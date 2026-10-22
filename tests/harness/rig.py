"""L'impianto di prova: hub + sip_client + media_handler + av_stream veri contro la
targa finta (peer.py), solo 127.0.0.1.

    async with Rig(monkeypatch, "tls", real_av=True, http=True, srtp=True) as rig:

transport  "udp" (locale) o "tls" (cloud)
real_av    ffmpeg vero per /av e frame grabber vero (altrimenti una pipe e niente foto)
http       aiohttp vero con le view e la pagina della card (web.start): rig.base
srtp       media cifrato: offriamo SRTP (MEDIA_ENC) e la targa risponde con a=crypto
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import tempfile
import time

from custom_components.vimar_intercom import av_passive, av_stream, away_tts, frame_grabber
from custom_components.vimar_intercom import const as C
from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import plant_state as S
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip

from . import web
from .peer import DOMAIN, PANEL, PASSWORD, USER, FakePeer


def run(coro, timeout=90):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def wait_until(cond, timeout=5.0, what="condizione"):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"{what}: mai verificata")
        await asyncio.sleep(0.02)


def free_udp_ports(n: int) -> list[int]:
    """n porte UDP libere e tutte diverse: i socket restano aperti finché non
    sono state scelte tutte (presa una alla volta, la seconda può essere la prima)."""
    socks = []
    try:
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            socks.append(s)
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def udp_port_is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def our_media_addrs(sdp_text: str):
    """(video, audio) dove la targa deve mandare il media, dal NOSTRO SDP."""
    p = sip.parse_sdp(sdp_text)
    return ((p["video"].get("ip") or "127.0.0.1", p["video"]["port"]),
            (p["audio"].get("ip") or "127.0.0.1", p["audio"]["port"]))


class Rig:
    def __init__(self, monkeypatch, transport: str = "udp", *, real_av=False, http=False,
                 srtp=False):
        self.mp, self.transport = monkeypatch, transport
        self.real_av, self.with_http, self.srtp = real_av, http, srtp
        self.ws_events: list[dict] = []
        self.rings = 0
        self.services: list[str] = []     # servizi chiamati dalla card (web.start)
        self.state_override = None        # stato imposto alla card al posto di hub.status
        self.panel_media = None           # harness.media.PanelMedia della targa, se c'è
        self.http = None

    def events(self, kind):
        return [e for e in self.ws_events if e.get("type") == kind]

    async def __aenter__(self):
        mp = self.mp
        # The fake panel answers any INVITE at once: it has none of a 2-wire
        # panel's busy seconds after a dialog (#44), which the settle before a
        # local UDP call waits out. Those are tested in test_view_after_hangup.
        mp.setattr(hub_mod, "LOCAL_UDP_SETTLE", 0.0)
        self.peer = peer = await FakePeer(self.transport).start()
        if self.srtp:
            peer.key = base64.b64encode(os.urandom(30)).decode()
        ha1 = hashlib.md5(f"{USER}:{DOMAIN}:{PASSWORD}".encode()).hexdigest()
        for k, v in dict(SIP_USER=USER, SIP_DOMAIN=DOMAIN, SIP_PASSWORD=PASSWORD, SIP_HA1=ha1,
                         LOCAL_PROXY="127.0.0.1", SIP_PROXY="localhost",
                         USE_LOCAL_UDP=self.transport == "udp", LOCAL_UDP_PORT=0,
                         INTERCOM=f"sip:{PANEL}@{DOMAIN}", DEVICE_UUID="uuid-test",
                         DEVICE_IMEI="000", AWAY_MESSAGE_FILE="", AWAY_MESSAGE_TEXT="",
                         AWAY_MESSAGE_TTS="", AWAY_MESSAGE_DELAY=0,
                         SNAPSHOT_DIR="").items():
            mp.setattr(R, k, v, raising=False)
        mp.setattr(S, "MEDIA_ENC", self.srtp)
        mp.setattr(away_tts, "_hass", None)
        mp.setattr(away_tts, "_cache", None)
        mp.setattr(C, "LOCAL_SIP_PORT", peer.port)
        a, v = free_udp_ports(2)
        for mod in (C, media):
            mp.setattr(mod, "RTP_AUDIO_PORT", a)
            mp.setattr(mod, "RTP_VIDEO_PORT", v)
        for k, val in dict(registered=False, in_call=False, calling=False, reader=None,
                           writer=None, _udp_sock=None, lock=None, cseq_counter=0,
                           pending_responses={}, _suppress_broadcast=False, _broadcast=None,
                           _state_change_callback=None, _model_callback=None,
                           incoming_requests=None, MY_IP=None, _reconnect_task=None,
                           _proxy_challenge=None,
                           call_state={k: None for k in sip.call_state},
                           pending_incoming={**{k: None for k in sip.pending_incoming},
                                             "active": False, "early": False}).items():
            mp.setattr(sip, k, val, raising=False)
        for k, val in dict(audio_proto=None, video_proto=None, _stun_task=None,
                           _audio_task=None, _tx_task=None, ws_send_bytes=None, _broadcast=None).items():
            mp.setattr(media, k, val)
        for k, val in dict(_av_lock=asyncio.Lock(), _av_clients=set(), _av_pump=None,
                           av_ffmpeg_proc=None).items():
            mp.setattr(av_stream, k, val)
        for k in ("_grabber", "_clip_q", "_clip_req"):  # niente foto o clip di un test prima
            mp.setattr(frame_grabber, k, None)
        for k, val in dict(_lock=asyncio.Lock(), _clients=set(), _task=None, _live=None).items():
            mp.setattr(av_passive, k, val)
        if self.real_av:
            from .media import free_even_port_pair
            vp = free_even_port_pair()
            ap = free_even_port_pair()
            while abs(ap - vp) < 2:
                ap = free_even_port_pair()
            mp.setattr(av_stream, "FFMPEG_AV_VIDEO_PORT", vp)
            mp.setattr(av_stream, "FFMPEG_AV_AUDIO_PORT", ap)
        else:
            mp.setattr(frame_grabber, "start", lambda vp: None)
            mp.setattr(frame_grabber, "stop", lambda vp, clip=True: None)
            mp.setattr(frame_grabber, "record", lambda *a: None)
            self._fake_av_ffmpeg()
        if self.transport == "tls":
            mp.setattr(sip, "_resolve_sip_targets", lambda proxy, port: [("127.0.0.1", peer.port)])
            mp.setattr(C, "CA_PATH", os.path.join(tempfile.gettempdir(), "nessun-ca.pem"))

        self.hub = hub = hub_mod.VimarIntercomHub()
        if not self.with_http:
            mp.setattr(hub, "_touch", lambda: None)
        hub.set_ws_broadcast(self._record)
        hub.register_ring_callback(self._ring)
        sip.init(hub._handle_broadcast)
        sip.set_state_callback(hub._on_sip_state_change)
        sip.set_model_callback(hub._on_model_detected)
        media.init(hub._handle_broadcast)
        sip.MY_IP = sip.get_local_ip()
        sip.incoming_requests = asyncio.Queue()
        await media.setup_transports()
        await sip.connect()
        hub._tasks += [asyncio.create_task(sip.reader_task()),
                       asyncio.create_task(sip.request_processor())]
        hub._running = True
        if self.with_http:
            await web.start(self)
        return self

    async def __aexit__(self, *exc):
        if self.panel_media:
            self.panel_media.stop()
        try:
            await self.hub.async_stop()
            await self.peer.stop()
        finally:
            if self.http:
                await self.http.cleanup()
        await asyncio.sleep(0)

    async def _record(self, payload):
        self.ws_events.append(payload)

    def _ring(self):
        self.rings += 1

    def _fake_av_ffmpeg(self):
        """ffmpeg dell'/av: una pipe al posto del processo (niente porte 19210)."""
        rig = self

        class FakeProc:
            def __init__(self):
                r, self.w = os.pipe()
                self.stdout = os.fdopen(r, "rb")
                self.alive = True

            def poll(self):
                return None if self.alive else 0

        async def start():
            av_stream.av_ffmpeg_proc = rig.av_proc = FakeProc()
            os.write(rig.av_proc.w, b"TS" * 100)

        async def stop():
            proc, av_stream.av_ffmpeg_proc = av_stream.av_ffmpeg_proc, None
            if proc:
                proc.alive = False
                os.close(proc.w)

        self.mp.setattr(av_stream, "_start_av_ffmpeg_locked", start)
        self.mp.setattr(av_stream, "_stop_av_ffmpeg_locked", stop)

    # ─── la targa ──────────────────────────────────────────────────────────────
    async def register(self):
        assert await sip.do_register()

    def answer(self, media_on=False, delay=0.05, bye_after=None, seq0=None):
        """La targa risponde agli INVITE (100, 180, 200 con SDP); `media_on`: manda
        H.264 vero; `bye_after`: chiude lei la vista dopo N s (Tab 5S Up 40515: ~10)."""
        async def on_invite(peer, inv):
            peer.reply(inv, 100, "Trying")
            peer.reply(inv, 180, "Ringing")
            await asyncio.sleep(delay)
            if peer.pending_invite is not inv:
                return  # annullato (CANCEL)
            peer.pending_invite = None
            peer.reply(inv, 200, "OK", body=peer.sdp())
            if media_on:
                self.start_media(inv.body, seq0)
            if bye_after:
                asyncio.get_running_loop().call_later(
                    bye_after, lambda: asyncio.ensure_future(self._bye_if_current(inv)))
        self.peer.on_invite = on_invite

    def start_media(self, our_sdp: str, seq0=None):
        """Video e voce della targa verso di noi (all'SDP nostro: 200 OK o 183)."""
        from .media import PanelMedia
        if self.panel_media is None or seq0 is not None:
            if self.panel_media:
                self.panel_media.stop()
            self.panel_media = PanelMedia(self.peer.key, seq0, self.peer.pt)
        self.panel_media.start(*our_media_addrs(our_sdp))

    def bye(self, inv, cseq=2):
        """La targa chiude la chiamata `inv` (l'INVITE nostro che aveva ricevuto)."""
        if self.panel_media:
            self.panel_media.stop()
        self.peer.request("BYE", inv.cid, cseq, self.peer.to_tag,
                          to_tag=inv.h("from").split("tag=")[1])

    async def _bye_if_current(self, inv):
        if sip.call_state.get("call_id") == inv.cid:
            self.bye(inv)

    def ring(self, cid="ring-1", tag="pnl", sdp=True, body=None):
        """Qualcuno suona: INVITE della targa verso di noi, con l'SDP della targa."""
        return self.peer.request("INVITE", cid, 1, tag, body=body if body is not None
                                 else (self.peer.sdp() if sdp else ""))
