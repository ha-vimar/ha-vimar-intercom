"""Not every entrance has a camera: some are only a microphone and a speaker.

Asking for video that does not exist leaves the pipeline waiting for a stream
nobody will send, and shows a camera in Home Assistant that can never produce
a picture.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")
qr = pytest.importorskip("custom_components.vimar_intercom.qr_decoder")
runtime = pytest.importorskip("custom_components.vimar_intercom.runtime")
media = pytest.importorskip("custom_components.vimar_intercom.media_handler")

AUDIO_ONLY_OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
    "m=audio 55730 RTP/AVP 0 8 101\r\na=rtpmap:0 PCMU/8000\r\n"
)


class TestSdp:
    def test_an_audio_only_answer_has_no_video_section(self):
        answer = sip.build_sdp(enc=False, video=False)
        assert "m=audio" in answer
        assert "m=video" not in answer

    def test_declining_an_offered_video_keeps_the_section_with_port_zero(self):
        """RFC 3264: an answer keeps the offer's sections, refusing with port 0."""
        answer = sip.build_sdp(enc=False, decline_video=True)
        assert "m=video 0 " in answer
        assert f"m=video {sip.C.RTP_VIDEO_PORT}" not in answer

    def test_video_is_still_offered_by_default(self):
        assert f"m=video {sip.C.RTP_VIDEO_PORT}" in sip.build_sdp(enc=False)

    def test_an_audio_only_offer_parses_without_a_video_port(self):
        offer = sip.parse_sdp(AUDIO_ONLY_OFFER)
        assert offer["audio"]["port"] == 55730
        assert not offer["video"].get("port")


class TestConfiguration:
    def test_the_qr_reports_whether_the_plant_has_video(self):
        fields = {"id": "1", "pwd": "p", "domain": "d", "video": "0"}
        assert qr.extract_sip_credentials(fields)["video_enabled"] is False
        fields["video"] = "1"
        assert qr.extract_sip_credentials(fields)["video_enabled"] is True

    def test_video_is_assumed_when_the_qr_does_not_say(self):
        assert qr.extract_sip_credentials({"id": "1", "pwd": "p", "domain": "d"})["video_enabled"]

    def test_runtime_carries_the_setting(self):
        runtime.configure({"sip_user": "1", "sip_domain": "d", "video_enabled": False})
        assert runtime.VIDEO_ENABLED is False
        runtime.configure({"sip_user": "1", "sip_domain": "d"})
        assert runtime.VIDEO_ENABLED is True


class TestFfmpegInput:
    def test_the_ffmpeg_sdp_omits_video_for_an_audio_only_call(self, monkeypatch, tmp_path):
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
        sdp = open(media._write_av_sdp()).read()
        assert "m=audio" in sdp
        assert "m=video" not in sdp, "ffmpeg would wait forever for a stream nobody sends"

    def test_the_ffmpeg_sdp_declares_video_once_its_parameters_are_known(self, monkeypatch, tmp_path):
        monkeypatch.setattr(media, "has_video", True)
        monkeypatch.setattr(media, "video_proto", type(
            "P", (), {"pkt_count": 12, "_last_sps": b"\x67\x42\xc0\x1f", "_last_pps": b"\x68\xee"},
        )())
        monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
        sdp = open(media._write_av_sdp()).read()
        assert f"m=video {media.FFMPEG_AV_VIDEO_PORT}" in sdp
        assert "sprop-parameter-sets=" in sdp

    def test_packets_without_parameter_sets_are_not_enough(self, monkeypatch, tmp_path):
        """Declaring video whose geometry ffmpeg must hunt for in the stream is
        how the video track silently disappears when analysis time is short."""
        monkeypatch.setattr(media, "has_video", True)
        monkeypatch.setattr(media, "video_proto", type("P", (), {"pkt_count": 12})())
        monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
        # Nessuna coppia ricordata da una chiamata precedente: è il caso di un
        # avvio a freddo, l'unico in cui i pacchetti da soli non bastano.
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        sdp = open(media._write_av_sdp()).read()
        assert "m=video" not in sdp

    def test_negotiated_but_absent_video_is_left_out_until_it_appears(self, monkeypatch, tmp_path):
        """The Tab offers video and then sends none; declaring it costs the audio too.

        ffmpeg cannot determine the geometry of a stream that never arrives and
        then emits nothing at all — the call ends with "frame size not set".
        """
        monkeypatch.setattr(media, "has_video", True)
        monkeypatch.setattr(media, "video_proto", type("P", (), {"pkt_count": 0})())
        monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
        sdp = open(media._write_av_sdp()).read()
        assert "m=audio" in sdp
        assert "m=video" not in sdp

    def test_known_parameter_sets_count_as_video_arriving(self, monkeypatch):
        proto = type("P", (), {"pkt_count": 0, "_last_sps": b"\x67", "_last_pps": b"\x68"})()
        monkeypatch.setattr(media, "video_proto", proto)
        assert media._video_is_flowing() is True

    def test_half_the_parameter_sets_is_not_enough(self, monkeypatch):
        proto = type("P", (), {"pkt_count": 99, "_last_sps": b"\x67", "_last_pps": None})()
        monkeypatch.setattr(media, "video_proto", proto)
        assert media._video_is_flowing() is False


def test_an_invite_without_sdp_is_answered_with_our_own_offer(monkeypatch):
    """With nothing offered, the answer is really an offer: video is our call."""
    monkeypatch.setattr(runtime, "VIDEO_ENABLED", True)
    monkeypatch.setattr(sip.R, "VIDEO_ENABLED", True)
    assert f"m=video {sip.C.RTP_VIDEO_PORT}" in sip.build_sdp(video=True)
    monkeypatch.setattr(sip.R, "VIDEO_ENABLED", False)
    assert "m=video" not in sip.build_sdp(video=False)


def test_parameter_sets_do_not_survive_a_call(monkeypatch):
    """Otherwise the next call looks like it has video when it has none, and
    ffmpeg waits for a stream that never arrives — taking the audio with it."""
    import asyncio

    proto = type("P", (), {
        "remote_addr": ("1.2.3.4", 1), "pkt_count": 9, "srtp_rx": object(),
        "_fua_buf": bytearray(b"x"), "_fua_started": True, "_fua_expected_seq": 5,
        "_last_sps": b"\x67\x42", "_last_pps": b"\x68\xee", "_sps_pps_sent": True,
        "forward_av": True,
    })()
    monkeypatch.setattr(media, "video_proto", proto)
    monkeypatch.setattr(media, "audio_proto", None)
    monkeypatch.setattr(media, "_stun_task", None)
    monkeypatch.setattr(media, "_audio_task", None)
    monkeypatch.setattr(media, "_audio_tx_task", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", None)

    asyncio.run(media.stop_media())

    assert proto._last_sps is None and proto._last_pps is None
    assert media._video_is_flowing() is False, "a finished call must not vouch for the next"
