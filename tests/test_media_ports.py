"""RTP port plan: every stream owns its RTCP partner.

An RTP stream on port N also binds N+1 for RTCP, so two streams whose ports sit
one apart cannot both bind. Observed live as ffmpeg exiting at startup with
"bind failed: Address in use" on both ports, which killed audio and video.
"""
import pytest

const = pytest.importorskip("custom_components.vimar_intercom.const")

PORTS = {
    "RTP_AUDIO_PORT": const.RTP_AUDIO_PORT,
    "RTP_VIDEO_PORT": const.RTP_VIDEO_PORT,
    "FFMPEG_AV_VIDEO_PORT": const.FFMPEG_AV_VIDEO_PORT,
    "FFMPEG_AV_AUDIO_PORT": const.FFMPEG_AV_AUDIO_PORT,
}
_media = pytest.importorskip("custom_components.vimar_intercom.media_handler")
for _i, (_v, _a) in enumerate(_media._HOMEKIT_PORT_POOL):
    PORTS[f"HomeKit session {_i} video"] = _v
    PORTS[f"HomeKit session {_i} audio"] = _a


def test_no_rtp_stream_collides_with_another_streams_rtcp():
    claimed: dict[int, str] = {}
    for name, port in PORTS.items():
        for offset, kind in ((0, "RTP"), (1, "RTCP")):
            taken = claimed.get(port + offset)
            assert taken is None, (
                f"{name} {kind} port {port + offset} is already used by {taken}"
            )
            claimed[port + offset] = f"{name} {kind}"


def test_ports_are_even_so_the_rtcp_partner_is_conventional():
    for name, port in PORTS.items():
        assert port % 2 == 0, f"{name}={port} should be even (RTP convention)"


def test_the_av_pair_is_far_enough_apart_to_bind():
    assert abs(const.FFMPEG_AV_VIDEO_PORT - const.FFMPEG_AV_AUDIO_PORT) >= 2


class TestSpropParameterSets:
    """ffmpeg must not have to wait for an SPS/PPS pair to learn the geometry.

    The panel sends parameter sets roughly every three seconds, so probing for
    them cost seconds of startup, and every restart began the wait again.
    """

    @staticmethod
    def media():
        return pytest.importorskip("custom_components.vimar_intercom.media_handler")

    def test_nothing_is_declared_before_any_sps_is_seen(self, monkeypatch):
        media = self.media()
        monkeypatch.setattr(media, "video_proto", None)
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        assert media._sprop_parameter_sets() == ""

    def test_a_pair_seen_once_is_reused_on_the_next_call(self, monkeypatch):
        """È questo che toglie l'attesa all'apertura successiva.

        La targa manda sempre gli stessi parametri, e non sempre li rimanda
        nel primo gruppo: una chiamata è rimasta ferma 3,1 s dopo aver già
        ricevuto PPS e un keyframe intero, solo perché l'SPS non c'era.
        """
        media = self.media()
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        seen = type("P", (), {"_last_sps": b"\x67sps", "_last_pps": b"\x68pps",
                              "pkt_count": 5})()
        monkeypatch.setattr(media, "video_proto", seen)
        first = media._sprop_parameter_sets()
        assert "sprop-parameter-sets=" in first

        # Chiamata nuova: i parametri di questa non sono ancora arrivati.
        fresh = type("P", (), {"_last_sps": None, "_last_pps": None,
                               "pkt_count": 3})()
        monkeypatch.setattr(media, "video_proto", fresh)
        assert media._sprop_parameter_sets() == first, "vanno ricordati"
        monkeypatch.setattr(media, "has_video", True)
        assert media.video_ready(), "con i pacchetti e i parametri noti si parte"

    def test_remembered_sets_do_not_invent_a_video_that_is_not_arriving(self, monkeypatch):
        """Il guaio opposto: un INVITE che dichiara video e non ne manda.

        Dichiararlo lo stesso lascia ffmpeg ad aspettare un video che non
        arriva — e con lui resta fermo anche l'audio.
        """
        media = self.media()
        monkeypatch.setattr(media, "_known_sps", b"\x67sps")
        monkeypatch.setattr(media, "_known_pps", b"\x68pps")
        monkeypatch.setattr(media, "has_video", True)
        monkeypatch.setattr(media, "video_proto", type("P", (), {
            "_last_sps": None, "_last_pps": None, "pkt_count": 0})())
        assert not media.video_ready()

    def test_both_sets_are_required(self, monkeypatch):
        monkeypatch.setattr(self.media(), "_known_sps", None)
        monkeypatch.setattr(self.media(), "_known_pps", None)
        media = self.media()
        proto = type("P", (), {"_last_sps": b"\x67\x42\xc0\x1f", "_last_pps": None})()
        monkeypatch.setattr(media, "video_proto", proto)
        assert media._sprop_parameter_sets() == ""

    def test_the_observed_sets_are_base64_encoded_in_order(self, monkeypatch):
        import base64

        media = self.media()
        sps, pps = b"\x67\x42\xc0\x1f\x01\x02", b"\x68\xee\x01\x44"
        proto = type("P", (), {"_last_sps": sps, "_last_pps": pps})()
        monkeypatch.setattr(media, "video_proto", proto)
        value = media._sprop_parameter_sets()
        assert value == (
            f";sprop-parameter-sets={base64.b64encode(sps).decode()},"
            f"{base64.b64encode(pps).decode()}"
        )

    def test_the_sdp_carries_them_so_ffmpeg_need_not_probe(self, monkeypatch, tmp_path):
        media = self.media()
        sps, pps = b"\x67\x42\xc0\x1f", b"\x68\xee\x01\x44"
        proto = type("P", (), {"_last_sps": sps, "_last_pps": pps, "pkt_count": 5})()
        monkeypatch.setattr(media, "video_proto", proto)
        monkeypatch.setattr(media, "has_video", True)
        monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
        sdp = open(media._write_av_sdp()).read()
        assert "sprop-parameter-sets=" in sdp
        assert "packetization-mode=1" in sdp
        assert f"m=video {media.FFMPEG_AV_VIDEO_PORT}" in sdp
        assert f"m=audio {media.FFMPEG_AV_AUDIO_PORT}" in sdp
