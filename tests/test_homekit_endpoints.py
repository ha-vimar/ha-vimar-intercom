"""SetupEndpoints answers with ports we actually listen on.

pyhap's own answer echoes the phone's ports back as if they were the
accessory's and opens nothing. For video that happened to work; for audio it
meant the voice of whoever answered went to a socket nobody read. This drives
the real set_endpoints with a request shaped like the phone's.
"""
import os
import struct
import uuid

import pytest

hk = pytest.importorskip("custom_components.vimar_intercom.homekit_accessory")
from pyhap import tlv  # noqa: E402
from pyhap.camera import SETUP_ADDR_INFO, SETUP_SRTP_PARAM, SETUP_TYPES  # noqa: E402


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


def phone_request(session_id, v_port=50000, a_port=50002):
    addr = tlv.encode(
        SETUP_ADDR_INFO["ADDRESS_VER"], b"\x00",
        SETUP_ADDR_INFO["ADDRESS"], b"192.168.1.50",
        SETUP_ADDR_INFO["VIDEO_RTP_PORT"], struct.pack("<H", v_port),
        SETUP_ADDR_INFO["AUDIO_RTP_PORT"], struct.pack("<H", a_port))
    srtp = tlv.encode(
        SETUP_SRTP_PARAM["CRYPTO"], b"\x00",
        SETUP_SRTP_PARAM["MASTER_KEY"], os.urandom(16),
        SETUP_SRTP_PARAM["MASTER_SALT"], os.urandom(14))
    return tlv.encode(
        SETUP_TYPES["SESSION_ID"], session_id.bytes,
        SETUP_TYPES["ADDRESS"], addr,
        SETUP_TYPES["VIDEO_SRTP_PARAM"], srtp,
        SETUP_TYPES["AUDIO_SRTP_PARAM"], srtp,
        to_base64=True)


@pytest.fixture
def acc():
    a = hk.VimarDoorbell.__new__(hk.VimarDoorbell)
    a.sessions = {}
    a.stream_address = "127.0.0.1"
    a.stream_address_isv6 = b"\x00"
    a._management = [_Mgmt()]
    yield a
    for info in a.sessions.values():
        hk._close_quietly(info.get("a_sock"))
        hk._close_quietly(info.get("v_sock"))


def test_the_answer_names_our_own_bound_ports(acc):
    sid = uuid.uuid4()
    acc.set_endpoints(phone_request(sid))
    answer = tlv.decode(acc._management[0].char.value, from_base64=True)
    addr = tlv.decode(answer[SETUP_TYPES["ADDRESS"]])
    v_port = struct.unpack("<H", addr[SETUP_ADDR_INFO["VIDEO_RTP_PORT"]])[0]
    a_port = struct.unpack("<H", addr[SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[0]

    info = acc.sessions[sid]
    assert (v_port, a_port) != (50000, 50002), "not the phone's ports echoed back"
    assert info["v_sock"].getsockname()[1] == v_port
    assert info["a_sock"].getsockname()[1] == a_port, "the talk socket is really open"
    assert (info["v_port"], info["a_port"]) == (50000, 50002), "the phone's ports are kept"
    assert info["address"] == "192.168.1.50"


def test_the_keys_are_the_phones(acc):
    sid = uuid.uuid4()
    acc.set_endpoints(phone_request(sid))
    answer = tlv.decode(acc._management[0].char.value, from_base64=True)
    ours = tlv.decode(answer[SETUP_TYPES["AUDIO_SRTP_PARAM"]])
    key = ours[SETUP_SRTP_PARAM["MASTER_KEY"]] + ours[SETUP_SRTP_PARAM["MASTER_SALT"]]
    import base64
    assert base64.b64decode(acc.sessions[sid]["a_srtp_key"]) == key
