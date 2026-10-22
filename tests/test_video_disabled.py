"""Entrances without a camera: the pairing QR says video=0.

Our own INVITE offer used to carry m=video anyway, asking an audio-only panel
for a stream it cannot send. Answers already mirror the offer; only the offer
we make depends on R.VIDEO_ENABLED.
"""
from __future__ import annotations

import pytest

from custom_components.vimar_intercom import qr_decoder as qr
from custom_components.vimar_intercom import sip_client as sip

R = sip.R

FIELDS = {"id": "12345", "pwd": "secret", "domain": "192.168.1.50",
          "cdomain": "abc.ipvdes.vimar.cloud", "gid": "101"}
VIDEO_OFFER = (
    "v=0\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=audio 55730 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    "m=video 57100 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


def _kinds(sdp: str) -> list[str]:
    return [line.split()[0][2:] for line in sdp.split("\r\n") if line.startswith("m=")]


@pytest.mark.parametrize("value, expected", [("0", False), ("1", True), (None, True)])
def test_the_qr_video_field_is_extracted(value, expected):
    fields = dict(FIELDS)
    if value is not None:
        fields[qr.QR_VIDEO] = value
    creds = qr.extract_sip_credentials(fields)
    # Stored as qr_video: for the Vimar apps it only allows the preview while it rings.
    assert creds["qr_video"] is expected and "video_enabled" not in creds


def test_an_entry_without_the_field_keeps_video():
    R.configure({"sip_user": "1", "sip_domain": "d"})
    assert R.VIDEO_ENABLED is True


def test_a_video_0_qr_does_not_take_video_out_of_our_calls():
    """For the apps VIDEO=0 only rules the preview while it rings; it took video out of
    every call we made (08-collab, #175 only added a manual override)."""
    R.configure({"sip_user": "1", "sip_domain": "d", "qr_video": False})
    assert R.VIDEO_ENABLED is True


def test_the_option_still_turns_video_off_in_our_calls():
    R.configure({"sip_user": "1", "sip_domain": "d", "qr_video": True, "video_enabled": False})
    assert R.VIDEO_ENABLED is False


def test_our_offer_has_no_video_line_on_an_audio_only_plant(monkeypatch):
    monkeypatch.setattr(R, "VIDEO_ENABLED", False)
    assert _kinds(sip.build_sdp()) == ["audio"]


def test_our_offer_keeps_video_by_default(monkeypatch):
    monkeypatch.setattr(R, "VIDEO_ENABLED", True)
    assert _kinds(sip.build_sdp()) == ["audio", "video"]


def test_an_answer_still_mirrors_the_offer(monkeypatch):
    """The panel offers video: the answer keeps the offer's lines (RFC 3264)."""
    monkeypatch.setattr(R, "VIDEO_ENABLED", False)
    assert _kinds(sip.build_sdp(sip.parse_sdp(VIDEO_OFFER))) == ["audio", "video"]
