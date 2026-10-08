"""Vimar Intercom: SDP, the offer/answer we build and the parser for the peer's.

Also the local SRTP keys our last SDP advertised, which setup_media encrypts with.
"""

import logging
import os
import time

from . import const as C
from . import plant_state as S
from . import runtime as R

_LOGGER = logging.getLogger(__name__)


# ─── SDP ────────────────────────────────────────────────────────────

_local_crypto_key = None
_local_video_crypto_key = None


# H.264 (RFC 6184): packetization-mode e profile-level-id vanno rispettati.
# La targa 2F (40507, baresip) offre e accetta SOLO packetization-mode=0
# (profile-level-id 42800c); le 2FV2 accettano il mode 1. Rispondere mode 1 a
# un'offerta mode 0 è una risposta senza codec in comune: la targa non manda
# un pacchetto video (prova del 28/09: audio rx=3602, video rx=0 in 54 s), e
# a un'offerta con il solo mode 1 risponde m=video 0 (probe di agosto).
_H264_OFFER = (
    ("96", "profile-level-id=42801F;packetization-mode=1"),
    ("97", "profile-level-id=42800c;packetization-mode=0"),
)


def _h264_answer(offer: dict | None) -> tuple[tuple[str, str], ...]:
    """Le linee H.264 della risposta: il payload type e i parametri che la
    targa ha offerto (il primo H.264 dell'offerta). Senza offerta, o senza
    H.264 nell'offerta, l'offerta completa (mode 1 e mode 0)."""
    video = (offer or {}).get("video") or {}
    for line in video.get("rtpmap", []):
        head, _, codec = line.partition(" ")
        if not codec.upper().startswith("H264/"):
            continue
        pt = head.split(":", 1)[1]
        fmtp = ""
        for f in video.get("fmtp", []):
            fhead, _, params = f.partition(" ")
            if fhead.split(":", 1)[1] == pt:
                fmtp = params.strip()
        keep = [kv for kv in fmtp.split(";")
                if kv.split("=", 1)[0].strip().lower() in ("packetization-mode", "profile-level-id")]
        return ((pt, ";".join(k.strip() for k in keep) or "packetization-mode=0"),)
    return _H264_OFFER


_SRTP_SUITES = ("AES_CM_128_HMAC_SHA1_80", "AES_CM_128_HMAC_SHA1_32")
_DEFAULT_CRYPTO = {"tag": "1", "suite": "AES_CM_128_HMAC_SHA1_80"}


def _offered_line(offer: dict | None, kind: str) -> dict | None:
    """The offer's m=<kind> line, or None when the offer has no such line.

    parse_sdp leaves an empty dict for a section the offer lacks; only a line
    with a port (0 included) was really offered.
    """
    line = (offer or {}).get(kind)
    return line if line and "port" in line else None


def _offered_kinds(offer: dict | None) -> list[str]:
    """The m-lines of an offer, in its order.

    With no offer (we make it), both; audio only when the pairing QR says the
    entrance has no camera (video=0, R.VIDEO_ENABLED False): offering video
    asks for a stream nobody will send.
    """
    kinds = [k for k in ((offer or {}).get("order") or ("audio", "video"))
             if _offered_line(offer, k) is not None]
    if kinds:
        return kinds
    return ["audio", "video"] if getattr(R, "VIDEO_ENABLED", True) else ["audio"]


def _line_security(offer: dict | None, kind: str) -> tuple[bool, dict | None]:
    """(answer this m-line with a live port, the crypto to answer it with or None).

    When we answer, each m-line mirrors that line of the offer: plants differ
    (a 2F panel wants plain RTP and ignores SRTP, a 2FV2 relay offers
    RTP/SAVP with a=crypto), and answering with the other profile means
    claiming to accept something we will not use: we would receive encrypted
    and send in the clear. An RTP/SAVP line is answered with the tag and suite
    of the offer's first supported a=crypto line; one with no supported suite
    cannot be used and is refused (port 0, RFC 4568 section 7.1.2). A line the
    offer declined (port 0) stays declined, and a line the offer lacks is not
    answered at all (build_sdp leaves it out). Only when we make the offer does
    the plant setting (S.MEDIA_ENC) decide.
    """
    offered = [m for m in (_offered_line(offer, "audio"), _offered_line(offer, "video")) if m]
    if not offered:
        return True, (dict(_DEFAULT_CRYPTO) if getattr(S, "MEDIA_ENC", False) else None)
    line = _offered_line(offer, kind)
    if line is None or not line.get("port"):
        return False, None
    if not line.get("secure"):
        return True, None
    if line.get("crypto_key") and not line.get("refused"):
        return True, {"tag": line.get("crypto_tag") or "1",
                      "suite": line.get("crypto_suite") or _DEFAULT_CRYPTO["suite"]}
    _LOGGER.warning("SDP: m=%s offers RTP/SAVP without a supported crypto suite "
                    "(%s): line refused", kind,
                    ", ".join(c.get("suite", "?") for c in line.get("crypto", [])) or "none")
    return False, None


def build_sdp(offer: dict | None = None, reuse_keys: bool = False):
    """Costruisce l'offerta/risposta SDP.

    Su questo impianto (verificato sul campo 20/08/2026 verso la targa 55100)
    il media viaggia in RTP IN CHIARO: se si offre SRTP (RTP/SAVP + a=crypto)
    la targa baresip non risponde e tutto il media fallisce. Perciò il default
    è RTP/AVP senza a=crypto. SRTP resta disponibile via S.MEDIA_ENC=True per
    impianti che negoziano media_enc. As an answer, each m-line mirrors the
    offer's profile and crypto suite for that line (see _line_security).

    reuse_keys: a new answer inside a dialog (re-INVITE) keeps the local SRTP
    keys the running media already encrypts with, so what we advertise and
    what srtp_tx sends stay the same key.
    """
    from .sip_client import MY_IP
    global _local_crypto_key, _local_video_crypto_key
    sid = str(int(time.time()))
    import base64 as _b64

    audio_ok, audio_sec = _line_security(offer, "audio")
    video_ok, video_sec = _line_security(offer, "video")

    # RTP in chiaro su una linea: nessuna chiave locale → setup_media non crea
    # srtp_tx/rx per quella linea.
    def _key(sec, current):
        if not sec:
            return None
        if reuse_keys and current:
            return current
        return _b64.b64encode(os.urandom(30)).decode()

    _local_crypto_key = _key(audio_sec, _local_crypto_key)
    _local_video_crypto_key = _key(video_sec, _local_video_crypto_key)
    audio_proto = "RTP/SAVP" if audio_sec else "RTP/AVP"
    video_proto = "RTP/SAVP" if video_sec else "RTP/AVP"
    audio_crypto = (f"a=crypto:{audio_sec['tag']} {audio_sec['suite']} "
                    f"inline:{_local_crypto_key}\r\n") if audio_sec else ""
    video_crypto = (f"a=crypto:{video_sec['tag']} {video_sec['suite']} "
                    f"inline:{_local_video_crypto_key}\r\n") if video_sec else ""
    audio_port = C.RTP_AUDIO_PORT if audio_ok else 0
    video_port = C.RTP_VIDEO_PORT if video_ok else 0

    h264 = _h264_answer(offer)
    h264_lines = "".join(
        f"a=rtpmap:{pt} H264/90000\r\n"
        f"a=fmtp:{pt} {fmtp}\r\n"
        f"a=rtcp-fb:{pt} ccm fir\r\n"
        f"a=rtcp-fb:{pt} nack\r\n"
        f"a=rtcp-fb:{pt} nack pli\r\n"
        for pt, fmtp in h264
    )

    blocks = {
        "audio": (
            f"m=audio {audio_port} {audio_proto} 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=fmtp:101 0-15\r\n"
            f"a=ptime:20\r\n"
            f"a=sendrecv\r\n"
            f"{audio_crypto}"
        ),
        "video": (
            f"m=video {video_port} {video_proto} {' '.join(pt for pt, _ in h264)}\r\n"
            f"b=AS:{R.VIDEO_BANDWIDTH}\r\n"
            f"{h264_lines}"
            f"a=sendrecv\r\n"
            f"{video_crypto}"
        ),
    }
    # RFC 3264 section 6: the answer has exactly the offer's m-lines, in the
    # offer's order; a line is refused with port 0, never dropped, and a line
    # the offer lacks is never added (an audio-only panel got a live m=video).
    return (
        f"v=0\r\n"
        f"o=- {sid} {sid} IN IP4 {MY_IP}\r\n"
        f"s=Talk\r\n"
        f"c=IN IP4 {MY_IP}\r\n"
        f"b=AS:{R.SESSION_BANDWIDTH}\r\n"
        f"t=0 0\r\n"
        f"a=rtcp-xr:rcvr-rtt=all:10000 stat-summary=loss,dup,jitt,TTL voip-metrics\r\n"
        + "".join(blocks.get(kind) or _refused_block(offer[kind])
                  for kind in _offered_kinds(offer))
    )


def _refused_block(line: dict) -> str:
    """An m-line we do not handle (m=text, m=application, ...), refused.

    RFC 3264 section 6: the answer keeps it, with port 0 and a format from the
    offer; dropping it shifts every later line of the answer.
    """
    fmts = " ".join(line.get("fmts") or ["0"])
    return f"m={line.get('media', 'application')} 0 {line.get('proto') or 'RTP/AVP'} {fmts}\r\n"


def _answer_fits(answer: str, offer: dict | None) -> bool:
    """Whether a previous answer still answers `offer`: the same m-lines in
    the same order, and no live line where the offer has port 0."""
    if not offer or not offer.get("order"):
        return True  # no SDP in the re-INVITE: the old session stands
    ours = _sdp_lines(answer)
    kinds = _offered_kinds(offer)
    if [media for media, _ in ours] != [offer[k].get("media", k) for k in kinds]:
        return False
    return all(offer[k]["port"] or not live for k, (_, live) in zip(kinds, ours, strict=False))


def _sdp_lines(sdp_text: str) -> list[tuple[str, bool]]:
    """(media, live) for each m-line of an SDP, in order."""
    lines = []
    for line in sdp_text.split("\n"):
        parts = line.strip().split()
        if parts and parts[0].startswith("m=") and len(parts) > 1:
            lines.append((parts[0][2:], parts[1] != "0"))
    return lines


def _loggable(section: dict | None) -> dict:
    """A parsed SDP section without its SRTP master key, for the log."""
    return {k: v for k, v in (section or {}).items() if k != "crypto_key"}


def parse_sdp(sdp_text):
    # "order" lists the m-lines as offered. A section the SDP lacks stays an
    # empty dict (no "port"): it is not a line of this session. Any other
    # m-line (m=text, m=application, a second m=audio) gets its own section
    # "m<n>" with "unknown": we answer it with port 0, and its c= and a=
    # lines stay in it instead of changing the line before.
    result = {"audio": {}, "video": {}, "conn": "", "order": []}
    m = None
    for line in sdp_text.split("\n"):
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
            if m:
                result[m]["ip"] = ip
            else:
                result["conn"] = ip
        elif line.startswith("m="):
            parts = line.split()
            media = parts[0][2:]
            try:
                port = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                port = 0
            if media in ("audio", "video") and "port" not in result[media]:
                m = media
            else:
                m = f"m{len(result['order'])}"
                result[m] = {"media": media, "unknown": True}
            result["order"].append(m)
            result[m]["port"] = port
            result[m]["proto"] = parts[2] if len(parts) > 2 else ""
            result[m]["secure"] = (not result[m].get("unknown")
                                   and "SAVP" in result[m]["proto"].upper())
            result[m]["fmts"] = parts[3:]
        elif line[2:] in ("sendrecv", "sendonly", "recvonly", "inactive") and line.startswith("a="):
            result.setdefault(m or "session", {})["dir"] = line[2:]
        elif line.startswith("a=rtpmap:") and m:
            result[m].setdefault("rtpmap", []).append(line)
        elif line.startswith("a=fmtp:") and m:
            result[m].setdefault("fmtp", []).append(line)
        elif line.startswith("a=crypto:") and m:
            # a=crypto:<tag> <suite> inline:<key>[|lifetime][|MKI] (RFC 4568)
            parts = line[9:].split()
            if len(parts) >= 3 and parts[2].startswith("inline:"):
                sec = result[m]
                tag, suite = parts[0], parts[1].upper()
                # The list keeps tag and suite only: the parsed SDP is logged,
                # and a master key in a log decrypts the call.
                sec.setdefault("crypto", []).append({"tag": tag, "suite": suite})
                # The key used for this line: the first a=crypto with a suite
                # we support, only on an RTP/SAVP line (on RTP/AVP it means
                # nothing).
                if sec.get("secure") and "crypto_key" not in sec and suite in _SRTP_SUITES:
                    sec.update(crypto_key=parts[2][7:].split("|", 1)[0],
                               crypto_tag=tag, crypto_suite=suite)
    session_dir = result.pop("session", {}).get("dir")
    for section in ("audio", "video"):
        line = result[section]
        if line.get("port") and line.get("secure") and "crypto_key" not in line:
            # RTP/SAVP with no crypto suite we support: our answer refuses it
            # with port 0 (_line_security), so no media may be set up on it.
            line["refused"] = True
    for section in ("audio", "video"):
        if "port" not in result[section]:
            continue
        if "ip" not in result[section]:
            result[section]["ip"] = result["conn"]
        if session_dir and "dir" not in result[section]:
            result[section]["dir"] = session_dir
    return result
