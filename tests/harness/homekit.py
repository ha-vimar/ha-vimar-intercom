"""Fakes for the HomeKit tests: stand-ins for the video and audio bridges, the encoder,
ffmpeg, the hub and Home Assistant's own objects. Moved out of tests/test_homekit_*.py."""
import asyncio
import time

from custom_components.vimar_intercom.media_handler import RTPVideoProtocol

SPS = b"\x67\x42\xc0\x1f\x8d\x68\x14\x1f\x90"
PPS = b"\x68\xee\x01\x44\x44\x80"


class FakeVideo:
    instances: list = []

    def __init__(self, *_a):
        self.stopped = 0
        FakeVideo.instances.append(self)

    async def open(self):
        pass

    def begin(self, *_a):
        pass

    def on_live(self, *_a):
        pass

    def send_bye(self):
        pass

    async def stop(self):
        self.stopped += 1


class FakeBridge:
    instances: list = []
    encoder_port = 9

    def __init__(self, *_a, on_first_voice=None):
        self.stopped = 0
        self.on_first_voice = on_first_voice
        self.on_first_audio = None
        FakeBridge.instances.append(self)

    async def start(self):
        pass

    def send_bye(self):
        pass

    async def stop(self):
        self.stopped += 1


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.pid = 4242
        self._done = asyncio.Event()
        self.stderr = None

    def terminate(self):
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self._done.wait()
        return self.returncode


class Hub:
    """The parts of v1.0.9's hub the accessory uses."""

    def __init__(self, gate):
        self.gate = gate
        self.call_coming = True
        self.in_call = True
        self.calling = False
        self.video_active = True
        self._stream_viewers = 0
        self._auto_called = False
        self._busy_now = True
        self.log = []

    async def stream_opened(self, reflex_guard=True):
        assert reflex_guard is False, "a HomeKit view is a person, not a reflex reopen"
        # Like the real hub: the viewer counts on the first line, before any await.
        self._stream_viewers += 1
        self.log.append("opened")
        await self.gate["open"].wait()
        return getattr(self, "open_result", True)

    async def stream_closed(self):
        self._stream_viewers -= 1
        self.log.append("closed")

    async def async_answer(self):
        self.log.append("answer")
        self.in_call = True
        return True, "ok"

    def should_hang_up_for_viewers(self, answered_for_them=False):
        # The real hub's rule (hub.should_hang_up_for_viewers).
        if self._stream_viewers:
            return False
        return answered_for_them or (self._auto_called and self._busy_now)

    async def async_hangup(self):
        self.log.append("hangup")


class FakeTap:
    instances: list = []

    def __init__(self):
        self.sdp_path, self.attached, self.closed = "/tmp/x.sdp", False, False
        FakeTap.instances.append(self)

    def attach(self):
        self.attached = True

    def close(self):
        self.closed = True


class FakeTranscoder:
    instances: list = []
    gate: asyncio.Event | None = None

    def __init__(self, *_a):
        self.video_proto = None
        self.stopped = False
        self.gop = type("G", (), {"has_keyframe": True, "packets": []})()
        FakeTranscoder.instances.append(self)

    @property
    def running(self):
        return not self.stopped

    async def start(self, _backlog, on_ready=None, defer=False):
        if FakeTranscoder.gate:
            await FakeTranscoder.gate.wait()
        self._on_ready, self.begun, self.begins = on_ready, False, 0
        if not defer:
            self.begin()
        return True

    def begin(self):
        if not self.begun:
            self.begun = True
            self.begins += 1
            self._on_ready()

    def feed(self, _p):
        pass

    def add_sink(self, _s):
        pass

    def remove_sink(self, _s):
        pass

    async def stop(self):
        self.stopped = True


class FakeHass:
    def __init__(self, tmp_path):
        self.data = {}
        self.tasks = []
        self.config = type("C", (), {"language": "en",
                                     "path": lambda _self, *p: str(tmp_path.joinpath(*p))})()

    def async_create_task(self, coro):
        self.tasks.append(asyncio.ensure_future(coro))

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class Notes:
    """persistent_notification: what was shown and dismissed."""

    def __init__(self):
        self.shown, self.dismissed = {}, []

    def async_create(self, _hass, text, title=None, notification_id=None):
        self.shown[notification_id] = (title, text)

    def async_dismiss(self, _hass, notification_id):
        self.dismissed.append(notification_id)
        self.shown.pop(notification_id, None)


class QRAcc:
    def xhm_uri(self):
        return "X-HM://0023ISYWYABCD"


class Req(dict):
    def __init__(self, user, token):
        super().__init__(hass_user=user)
        self.query = {"t": token}


class Response:
    def __init__(self, status=200, body=b"", content_type=None, headers=None):
        self.status, self.body = status, body


class FakeState:
    def __init__(self, clients=1):
        self.clients = clients
        self.pincode = b"111-22-333"

    @property
    def paired(self):
        return self.clients > 0

    def remove_paired_client(self, _uuid):
        self.clients -= 1


class SetupHass(FakeHass):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.in_executor = False
        self.views = []
        self.http = type("H", (), {"register_view": lambda _s, v: self.views.append(v)})()
        self.loop = None

    async def async_add_executor_job(self, fn, *args):
        self.in_executor = True
        try:
            return fn(*args)
        finally:
            self.in_executor = False


class SetupHub:
    def __init__(self):
        self.rings, self.ends = [], []

    def register_ring_callback(self, cb):
        self.rings.append(cb)

    def unregister_ring_callback(self, cb):
        self.rings.remove(cb)

    def register_video_end_callback(self, cb):
        self.ends.append(cb)
        return lambda: self.ends.remove(cb)


class FakeVideoProto:
    gop_in_sequence_order = RTPVideoProtocol.gop_in_sequence_order

    def __init__(self, gop, ps=(SPS, PPS)):
        self._gop = gop
        self._ps = ps
        self.rtp_sinks = []
        self.pkt_count = len(gop)

    def sps_pps(self, own_only=False):
        return self._ps


class Recorder:
    """Stands in for the tap's socket: what left, and when."""

    def __init__(self):
        self.sent = []

    def sendto(self, data, _addr):
        self.sent.append((time.monotonic(), data))

    def close(self):
        pass


class _Char:
    def __init__(self):
        self.value = None

    def set_value(self, value):
        self.value = value


class _Mgmt:
    def __init__(self):
        self.char = _Char()

    def get_characteristic(self, _name):
        return self.char


class FakeVoiceFfmpeg:
    """A voice decoder that exits at once (a broken ffmpeg, a bad build)."""

    def __init__(self, returncode=1):
        self.returncode = returncode
        self.stdout = self.stderr = None

    async def wait(self):
        return self.returncode


class FakeSdpFfmpeg:
    returncode = None
    stderr = None
    stdout = None

    def terminate(self):
        self.returncode = -15

    kill = terminate

    async def wait(self):
        return self.returncode
