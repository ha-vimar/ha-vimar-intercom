"""La sessione di HomeKit: riconoscerla, e soprattutto chiuderla.

Da quando HomeKit legge un SDP invece del nostro MPEG-TS non esiste più una
richiesta HTTP la cui fine dica "ho chiuso la vista". Senza un sostituto la
chiamata restava in piedi fino al limite del citofono: una trentina di secondi
per l'autoaccensione, DUE MINUTI per una chiamata dalla strada — e in quei due
minuti la luce del posto esterno resta accesa e nessun altro può suonare.
"""
import pytest

media = pytest.importorskip("custom_components.vimar_intercom.media_handler")


class TestSessionDetection:
    def test_killing_nothing_is_harmless(self, monkeypatch):
        monkeypatch.setattr(media, "homekit_ffmpeg_pids", lambda: [])
        assert media.stop_homekit_ffmpeg() == 0

    def test_a_dead_process_does_not_raise(self, monkeypatch):
        """Il processo può essere già uscito: è la corsa normale, non un errore."""
        monkeypatch.setattr(media, "homekit_ffmpeg_pids", lambda: [999999])
        def boom(pid, sig):
            raise ProcessLookupError
        monkeypatch.setattr(media.os, "kill", boom)
        assert media.stop_homekit_ffmpeg() == 0


class TestAudioOnlyFallback:
    """Una chiamata senza immagini non può prendere la scorciatoia.

    Home Assistant mette sempre ``-map 0:v:0`` nella riga di comando. Con un
    SDP di solo audio ffmpeg esce subito — "Stream map '' matches no streams" —
    e non parte nemmeno la voce. È il caso del Tab che chiama Home Assistant.
    """

    @staticmethod
    def session(tmp_path):
        return {"sdp": str(tmp_path / "hk.sdp"), "audio_port": 19208, "video_port": 19206}

    def test_the_sdp_omits_video_when_none_is_arriving(self, monkeypatch, tmp_path):
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "video_proto", None)
        sdp = open(media._write_homekit_sdp_for(self.session(tmp_path))).read()
        assert "m=audio" in sdp
        assert "m=video" not in sdp, "senza video la scorciatoia va evitata a monte"

    def test_the_sdp_declares_video_when_it_is_arriving(self, monkeypatch, tmp_path):
        monkeypatch.setattr(media, "has_video", True)
        monkeypatch.setattr(media, "_known_sps", b"\x67sps")
        monkeypatch.setattr(media, "_known_pps", b"\x68pps")
        monkeypatch.setattr(media, "video_proto",
                            type("P", (), {"_last_sps": None, "_last_pps": None,
                                           "pkt_count": 7})())
        sdp = open(media._write_homekit_sdp_for(self.session(tmp_path))).read()
        assert "m=video" in sdp
        assert "sprop-parameter-sets=" in sdp, "la geometria va dichiarata, non cercata"


class TestTheRestartThatShouldNotHappen:
    """I parametri ricordati non sono la prova che il video stia arrivando.

    Da quando SPS e PPS sopravvivono alla chiamata, il controllo che decideva
    se "il video è arrivato" rispondeva di sì anche su una chiamata che video
    non ne manda affatto — quelle che partono dal Tab. Si riavviava ffmpeg nel
    mezzo dell'analisi di HomeKit per ricostruire lo stesso flusso nero: la
    rotella girava e l'audio non partiva.
    """

    def test_remembered_parameters_alone_do_not_mean_video(self, monkeypatch):
        monkeypatch.setattr(media, "has_video", True)          # dichiarato...
        monkeypatch.setattr(media, "_known_sps", b"\x67sps")   # ...e già noti...
        monkeypatch.setattr(media, "_known_pps", b"\x68pps")
        monkeypatch.setattr(media, "video_proto",
                            type("P", (), {"_last_sps": None, "_last_pps": None,
                                           "pkt_count": 0})())  # ...ma zero pacchetti
        assert media._sprop_parameter_sets() != "", (
            "i parametri ci sono: è proprio questo che traeva in inganno")
        assert not media.video_ready(), (
            "ma il video non sta arrivando, quindi non si riavvia nulla")


class TestSessionScopedTeardown:
    """Chiudere una sessione non deve chiudere quella dopo.

    Riaprendo in fretta, la chiamata nuova parte mentre la vecchia si sta
    ancora spegnendo: chi chiude alla cieca uccide il processo appena nato.
    """

    def test_only_the_named_processes_are_stopped(self, monkeypatch):
        killed = []
        monkeypatch.setattr(media.os, "kill", lambda pid, sig: killed.append(pid))
        monkeypatch.setattr(media, "homekit_ffmpeg_pids", lambda: [111, 222, 333])
        media.stop_homekit_ffmpeg({111})
        assert killed == [111], "gli altri possono essere di una sessione nuova"

    def test_without_a_list_it_falls_back_to_whatever_is_running(self, monkeypatch):
        killed = []
        monkeypatch.setattr(media.os, "kill", lambda pid, sig: killed.append(pid))
        monkeypatch.setattr(media, "homekit_ffmpeg_pids", lambda: [777])
        media.stop_homekit_ffmpeg(None)
        assert killed == [777]


class TestReopeningDuringAHangup:
    """Riaprire mentre la chiamata precedente si sta chiudendo.

    Visto nei registri, con un secondo fra la chiusura e la riapertura:

      16:21:42  vista chiusa, parte il BYE
      16:21:43  nuova vista aperta  -> si attacca alla chiamata di prima
      16:21:45  quel BYE arriva     -> e le muore sotto

    È la differenza fra chiudere e basta (che funziona) e chiudere e
    riaprire subito (che non funzionava).
    """

    @staticmethod
    def hub():
        mod = pytest.importorskip("custom_components.vimar_intercom.hub")
        h = mod.VimarIntercomHub()
        return mod, h

    def test_a_new_view_waits_for_the_hangup_to_finish(self, monkeypatch):
        import asyncio
        mod, h = self.hub()
        monkeypatch.setattr(mod, "HANGUP_SETTLE", 0.5)
        h._hanging_up = True
        calls = []
        monkeypatch.setattr(mod.sip, "registered", True, raising=False)
        monkeypatch.setattr(mod.sip, "in_call", False, raising=False)
        monkeypatch.setattr(mod.sip, "calling", False, raising=False)
        monkeypatch.setattr(mod.sip, "pending_incoming", {"active": False}, raising=False)
        monkeypatch.setattr(h, "_spawn", lambda coro: (coro.close(), calls.append(1))[1])

        async def main():
            task = asyncio.ensure_future(h.stream_opened())
            await asyncio.sleep(0.15)
            assert not calls, "non deve chiamare finché il riaggancio è in corso"
            h._hanging_up = False          # il BYE si conclude
            await asyncio.wait_for(task, timeout=2)
            return calls

        assert asyncio.run(main()), "finita l'attesa, la chiamata nuova parte"

    def test_without_a_hangup_in_flight_nothing_is_delayed(self, monkeypatch):
        import asyncio, time
        mod, h = self.hub()
        h._hanging_up = False
        monkeypatch.setattr(mod.sip, "registered", False, raising=False)
        monkeypatch.setattr(mod.sip, "in_call", False, raising=False)
        monkeypatch.setattr(mod.sip, "calling", False, raising=False)
        monkeypatch.setattr(mod.sip, "pending_incoming", {"active": False}, raising=False)

        async def main():
            t0 = time.monotonic()
            await h.stream_opened()
            return time.monotonic() - t0

        assert asyncio.run(main()) < 0.2, "senza riaggancio in corso non si aspetta"


class TestPerSessionResources:
    """Ogni sessione ha porte e SDP propri, e questo chiude la questione.

    Finché le sessioni condividevano porte fisse e un unico file, quella che
    moriva si portava dietro la nuova, e nessun guardiano riusciva a
    distinguerle: tre tentativi di aggirare la corsa sono falliti. Con risorse
    separate la corsa non esiste.
    """

    def setup_method(self):
        media._homekit_sessions.clear()

    def teardown_method(self):
        for sid in list(media._homekit_sessions):
            media.release_homekit_session(sid)

    def test_two_sessions_never_share_ports(self, monkeypatch):
        monkeypatch.setattr(media, "_udp_port_free", lambda port: True)
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "video_proto", None)
        a = media.acquire_homekit_session()
        b = media.acquire_homekit_session()
        assert a and b
        assert (a["video_port"], a["audio_port"]) != (b["video_port"], b["audio_port"])
        assert a["sdp"] != b["sdp"], "nemmeno il file può essere in comune"

    def test_a_released_pair_can_be_taken_again(self, monkeypatch):
        monkeypatch.setattr(media, "_udp_port_free", lambda port: True)
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "video_proto", None)
        first = media.acquire_homekit_session()
        media.release_homekit_session(first["id"])
        again = media.acquire_homekit_session()
        assert again["video_port"] == first["video_port"]

    def test_ports_still_held_by_a_dying_session_are_skipped(self, monkeypatch):
        """Il punto dell'esercizio: la sessione precedente che non si è spenta."""
        busy = {media._HOMEKIT_PORT_POOL[0][0]}
        monkeypatch.setattr(media, "_udp_port_free", lambda port: port not in busy)
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "video_proto", None)
        s = media.acquire_homekit_session()
        assert s["video_port"] != media._HOMEKIT_PORT_POOL[0][0]

    def test_rtp_goes_to_every_live_session(self, monkeypatch):
        monkeypatch.setattr(media, "_udp_port_free", lambda port: True)
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "video_proto", None)
        a = media.acquire_homekit_session()
        b = media.acquire_homekit_session()
        # Il video parte solo dopo il keyframe: qui si simula che sia arrivato.
        a["ready"] = b["ready"] = True
        assert sorted(media.homekit_session_ports("video")) == sorted(
            [a["video_port"], b["video_port"]])
        media.release_homekit_session(a["id"])
        assert media.homekit_session_ports("video") == [b["video_port"]]

    def test_the_pid_lookup_is_scoped_to_one_sdp(self, monkeypatch, tmp_path):
        """Un ffmpeg che legge l'SDP di un'altra sessione non è il nostro."""
        mine = str(tmp_path / "mine.sdp")
        theirs = str(tmp_path / "theirs.sdp")
        assert mine != theirs
        # homekit_ffmpeg_pids filtra sul percorso: basta che i due differiscano
        # perché un processo non possa essere scambiato per l'altro.
        assert media.homekit_ffmpeg_pids(mine) == []


class TestTheOpeningKeyframe:
    """La targa manda SPS, PPS e IDR appena risponde — e noi arriviamo tardi.

    Misurato, sempre uguale:

      16:51:52,744  IDR di apertura
      16:51:52,812  inoltro acceso   <- 68 ms troppo tardi
      16:51:55,737  keyframe successivo, tre secondi dopo

    Chi si collega non ha quindi niente di decodificabile per tre secondi, e
    se chiude prima non vede mai nulla: nelle catture due sessioni su quattro
    hanno ricevuto ZERO pacchetti video.
    """

    @staticmethod
    def proto():
        cls = media.RTPVideoProtocol
        p = cls.__new__(cls)
        p._gop = []
        p.GOP_BUFFER_MAX = 200
        sent = []
        p.ffmpeg_av_sock = type("S", (), {"sendto": lambda s, d, a: sent.append((d, a))})()
        return p, sent

    def test_the_buffer_restarts_at_each_sps(self):
        p, _ = self.proto()
        p._gop = [b"old", b"older"]
        # un pacchetto il cui payload comincia con un NAL di tipo 7
        assert (0x67 & 0x1F) == 7
        p._gop.clear()
        p._gop.append(b"sps-packet")
        assert p._gop == [b"sps-packet"], "il keyframe nuovo sostituisce il vecchio"

    def test_replay_goes_only_to_the_port_asked_for(self, monkeypatch):
        # Senza parametri ricordati non si antepone niente: qui si guarda solo
        # dove finiscono i pacchetti.
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        p, sent = self.proto()
        # Pacchetti RTP veri: il rigioco li rimette in ordine di sequenza e
        # quindi ha bisogno di un'intestazione da cui leggerla.
        p._gop = [bytes([0x80, 0x60]) + n.to_bytes(2, "big") + bytes(10)
                  for n in (1, 2, 3)]
        n = p.replay_gop_to(19206)
        assert n == 3
        assert [a for _, a in sent] == [("127.0.0.1", 19206)] * 3, (
            "solo la sessione HomeKit: nel percorso MPEG-TS sfasava l'audio")

    def test_replaying_nothing_is_harmless(self):
        p, sent = self.proto()
        assert p.replay_gop_to(19206) == 0
        assert sent == []


class TestKeyframeBeforeLiveFlow:
    """L'ordine conta: prima il keyframe, poi il flusso dal vivo.

    Mandando prima i pacchetti correnti, il keyframe che arriva dopo ha numeri
    di sequenza più vecchi e ffmpeg lo butta:

        [in#0/sdp] RTP: dropping old packet received too late

    ed è esattamente quello che si leggeva nei registri mentre la sessione
    restava senza immagine.
    """

    def setup_method(self):
        media._homekit_sessions.clear()

    def teardown_method(self):
        media._homekit_sessions.clear()

    def test_video_is_withheld_until_the_keyframe_has_been_fed(self, monkeypatch):
        monkeypatch.setattr(media, "_udp_port_free", lambda port: True)
        monkeypatch.setattr(media, "has_video", False)
        monkeypatch.setattr(media, "video_proto", None)
        s = media.acquire_homekit_session()
        assert s["ready"] is False
        assert media.homekit_session_ports("video") == [], (
            "niente video finché il keyframe non è arrivato")
        assert media.homekit_session_ports("audio") == [s["audio_port"]], (
            "l'audio invece parte subito: non ha keyframe da aspettare")
        s["ready"] = True
        assert media.homekit_session_ports("video") == [s["video_port"]]


class TestParameterSetsReachThePhone:
    """La targa non mette sempre l'SPS nel gruppo del keyframe.

    Misurato: due aperture di chiamata su sette avevano PPS e IDR ma nessun
    SPS. Con "-c:v copy" ffmpeg inoltra solo i NAL che riceve — quello che sta
    in sprop-parameter-sets resta extradata e non finisce mai sul filo — e il
    telefono non ha di che decodificare fino al gruppo successivo: tre secondi,
    sei se ne mancano due di fila.
    """

    @staticmethod
    def rtp(nal_byte: int) -> bytes:
        # intestazione RTP minima (12 byte) + un NAL
        return bytes([0x80, 0x60, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1]) + bytes([nal_byte, 0xAA])

    @staticmethod
    def proto(gop):
        cls = media.RTPVideoProtocol
        p = cls.__new__(cls)
        p._gop = list(gop)
        sent = []
        p.ffmpeg_av_sock = type("S", (), {"sendto": lambda s, d, a: sent.append(d)})()
        return p, sent

    def test_a_missing_sps_is_prepended_as_stap_a(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", b"\x67\x42\xc0\x1f")
        monkeypatch.setattr(media, "_known_pps", b"\x68\xee\x01\x44")
        p, sent = self.proto([self.rtp(0x68), self.rtp(0x65)])   # PPS + IDR, nessun SPS
        assert p.replay_gop_to(19206) == 3, "un pacchetto in più davanti"
        first = sent[0]
        assert (first[12] & 0x1F) == 24, "deve essere uno STAP-A"
        assert b"\x67\x42\xc0\x1f" in first and b"\x68\xee\x01\x44" in first
        assert first[:4] == self.rtp(0x68)[:4], "riusa l'intestazione del pacchetto esistente"

    def test_a_group_that_already_has_sps_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", b"\x67sps")
        monkeypatch.setattr(media, "_known_pps", b"\x68pps")
        gop = [self.rtp(0x67), self.rtp(0x68), self.rtp(0x65)]
        p, sent = self.proto(gop)
        assert p.replay_gop_to(19206) == 3, "niente da aggiungere"
        assert sent == gop

    def test_an_sps_inside_a_stap_a_counts(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", b"\x67sps")
        monkeypatch.setattr(media, "_known_pps", b"\x68pps")
        stap = media._stap_a(b"\x67sps", b"\x68pps", self.rtp(0x68))
        p, sent = self.proto([stap, self.rtp(0x65)])
        assert p.replay_gop_to(19206) == 2, "l'SPS c'è già, dentro lo STAP-A"

    def test_without_remembered_sets_nothing_is_invented(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        p, sent = self.proto([self.rtp(0x68), self.rtp(0x65)])
        assert p.replay_gop_to(19206) == 2


class TestReplayOrder:
    """Il keyframe va rigiocato in ordine di sequenza, non di arrivo.

    Il buffer si riempie PRIMA del riordino, quindi contiene i pacchetti
    nell'ordine in cui sono arrivati. Rigiocandoli così, ffmpeg prende il primo
    come riferimento e scarta quelli che lo precedono:

        [in#0/sdp] RTP: dropping old packet received too late

    Misurato: tre pacchetti persi su quindici, IDR incompleto, e 2,8 secondi di
    attesa fino al keyframe successivo prima che uscisse il primo fotogramma.
    """

    @staticmethod
    def pkt(seq: int, nal: int = 0x41) -> bytes:
        return (bytes([0x80, 0x60]) + seq.to_bytes(2, "big")
                + bytes(8) + bytes([nal, 0xAA]))

    @staticmethod
    def proto(gop):
        cls = media.RTPVideoProtocol
        p = cls.__new__(cls)
        p._gop = list(gop)
        sent = []
        p.ffmpeg_av_sock = type("S", (), {"sendto": lambda s, d, a: sent.append(d)})()
        return p, sent

    @staticmethod
    def seqs(packets):
        return [int.from_bytes(p[2:4], "big") for p in packets]

    def test_out_of_order_arrivals_are_replayed_in_order(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        arrival = [self.pkt(n) for n in (100, 103, 101, 102)]
        p, sent = self.proto(arrival)
        p.replay_gop_to(19206)
        assert self.seqs(sent) == [100, 101, 102, 103]

    def test_a_sequence_wrap_inside_the_group_is_handled(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        arrival = [self.pkt(n) for n in (65534, 1, 65535, 0)]
        p, sent = self.proto(arrival)
        p.replay_gop_to(19206)
        assert self.seqs(sent) == [65534, 65535, 0, 1], "il giro del contatore non scombina"

    def test_already_ordered_stays_ordered(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        arrival = [self.pkt(n) for n in (10, 11, 12)]
        p, sent = self.proto(arrival)
        p.replay_gop_to(19206)
        assert self.seqs(sent) == [10, 11, 12]


class TestWhatWeActuallyReplay:
    """Il rapporto sul gruppo rigiocato, che distingue un IDR intero da uno monco.

    Il telefono mostra l'immagine al primo IDR che riesce a decodificare, e
    butta via senza un fiato quelli a cui manca un pezzo: resta nero fino al
    keyframe naturale della targa, tre secondi esatti più tardi. Nei log le due
    cose sono indistinguibili — "keyframe rigiocato, 15 pacchetti" si scrive
    uguale nei due casi. Questo rapporto le separa.
    """

    @staticmethod
    def proto(gop):
        cls = media.RTPVideoProtocol
        p = cls.__new__(cls)
        p._gop = list(gop)
        return p

    @staticmethod
    def rtp(seq: int, payload: bytes, marker: bool = False) -> bytes:
        return (bytes([0x80, 0xE0 if marker else 0x60])
                + seq.to_bytes(2, "big") + bytes(8) + payload)

    def fua(self, seq, size, start=False, end=False, marker=False):
        fu = (0x80 if start else 0) | (0x40 if end else 0) | 5
        return self.rtp(seq, bytes([0x7C, fu]) + b"\xAA" * size, marker)

    def test_a_whole_idr_is_reported_whole(self):
        gop = [
            self.rtp(10, b"\x67" + b"\x01" * 23),      # SPS, 24B
            self.rtp(11, b"\x68" + b"\x01" * 4),       # PPS, 5B
            self.fua(12, 1200, start=True),
            self.fua(13, 1200),
            self.fua(14, 900, end=True, marker=True),
        ]
        report = self.proto(gop)._gop_report()
        assert "buchi=0" in report
        assert "unità chiusa=sì" in report
        assert "7:24" in report and "8:5" in report
        assert "5:3300" in report, report

    def test_a_lost_tail_shows_up_as_an_open_unit(self):
        """Il frammento finale perso: l'IDR non si chiude, il telefono lo scarta."""
        gop = [
            self.rtp(10, b"\x67" + b"\x01" * 23),
            self.rtp(11, b"\x68" + b"\x01" * 4),
            self.fua(12, 1200, start=True),
            self.fua(13, 1200),
        ]
        report = self.proto(gop)._gop_report()
        assert "5!aperto:2400" in report, report
        assert "unità chiusa=NO" in report

    def test_a_hole_in_the_sequence_is_counted(self):
        gop = [
            self.fua(12, 1200, start=True),
            self.fua(15, 900, end=True, marker=True),   # 13 e 14 mancano
        ]
        assert "buchi=2" in self.proto(gop)._gop_report()

    def test_an_empty_group_says_so(self):
        assert self.proto([])._gop_report() == "vuoto"


class TestALateSpsMustNotWipeTheKeyframe:
    """L'SPS che arriva in ritardo non deve portarsi via l'IDR già raccolto.

    Il buffer si riempiva in ordine di ARRIVO e veniva sforbiciato lì, sul
    primo SPS che passava. Quando l'SPS arriva dopo il PPS e l'IDR che gli
    vanno dietro — succede davvero, il riordino sotto esiste apposta — quella
    sforbiciata buttava via proprio il keyframe. Misurato in casa, 12:04:24:

        Video pkt #2: 1252B, nals=1 types={8: 1}      <- il PPS c'e'
        IDR buffered — waiting for SPS+PPS            <- l'IDR pure
        Keyframe rigiocato: buchi=6, nal=[7:9,1:2162,1:1937,1:2279,1:2138]

    Il depacchettizzatore, che sta DIETRO il riordino, l'IDR lo vedeva; al
    telefono rigiocavamo un SPS spaiato con quattro P-frame. Nero fino al
    keyframe naturale successivo: tre secondi.
    """

    @staticmethod
    def rtp(seq: int, payload: bytes, marker: bool = False) -> bytes:
        return (bytes([0x80, 0xE0 if marker else 0x60])
                + seq.to_bytes(2, "big") + bytes(8) + payload)

    def fua(self, seq, size, start=False, end=False, marker=False):
        fu = (0x80 if start else 0) | (0x40 if end else 0) | 5
        return self.rtp(seq, bytes([0x7C, fu]) + b"\xAA" * size, marker)

    def proto(self, arrival):
        """Il buffer riempito dalla VERA regola di raccolta, un pacchetto alla
        volta nell'ordine in cui arrivano — non assegnato a mano."""
        cls = media.RTPVideoProtocol
        p = cls.__new__(cls)
        p._gop = media.deque(maxlen=cls.GOP_BUFFER_MAX)
        for rtp in arrival:
            p._remember_for_replay(rtp)
        return p

    def keyframe_arriving_sps_last(self):
        """Sequenza 100..105 = SPS,PPS,IDR — ma l'SPS entra per ultimo."""
        sps = self.rtp(100, b"\x67" + b"\x01" * 23)
        pps = self.rtp(101, b"\x68" + b"\x01" * 4)
        idr = [self.fua(102, 1200, start=True),
               self.fua(103, 1200),
               self.fua(104, 900, end=True, marker=True)]
        return [pps, *idr, sps]          # ordine di ARRIVO

    def test_the_idr_survives(self):
        p = self.proto(self.keyframe_arriving_sps_last())
        assert p._gop_has_idr(), "l'IDR c'era: non deve sparire"
        report = p._gop_report()
        assert "buchi=0" in report, report
        assert "5:3300" in report, report
        assert "7:24" in report and "8:5" in report, report

    def test_it_is_replayed_in_sequence_order(self, monkeypatch):
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        p = self.proto(self.keyframe_arriving_sps_last())
        sent = []
        p.ffmpeg_av_sock = type("S", (), {"sendto": lambda s, d, a: sent.append(d)})()
        p.replay_gop_to(19206)
        assert [int.from_bytes(x[2:4], "big") for x in sent] == [100, 101, 102, 103, 104]

    def test_the_group_starts_at_the_last_sps(self):
        """Quello che precede il keyframe non va rigiocato: è roba vecchia."""
        vecchi = [self.rtp(90 + i, b"\x41" + b"\xAA" * 500) for i in range(5)]
        p = self.proto(vecchi + self.keyframe_arriving_sps_last())
        ordinati = p._gop_in_sequence_order()
        assert [int.from_bytes(x[2:4], "big") for x in ordinati] == [100, 101, 102, 103, 104]


class TestNoKeyframeMeansNoGreenLight:
    """Senza un IDR intero non si rigioca e non si apre il flusso dal vivo.

    Mandare al telefono un SPS spaiato e qualche P-frame non è "meglio di
    niente": è identico a niente, ma ci fa dichiarare la sessione pronta e
    aprire il flusso dal vivo su un telefono che non ha di che decodificarlo.
    """

    rtp = staticmethod(TestALateSpsMustNotWipeTheKeyframe.rtp)
    fua = TestALateSpsMustNotWipeTheKeyframe.fua
    proto = TestALateSpsMustNotWipeTheKeyframe.proto

    def test_sps_plus_p_frames_is_not_a_keyframe(self):
        gop = [self.rtp(100, b"\x67" + b"\x01" * 23)] + [
            self.rtp(107 + i, b"\x41" + b"\xAA" * 2000) for i in range(4)]
        assert not self.proto(gop)._gop_has_idr(), "l'IDR non c'è: non si rigioca"

    def test_a_half_idr_is_not_a_keyframe(self):
        """Solo la coda dell'IDR: il frammento che lo apre manca."""
        gop = [self.rtp(100, b"\x67" + b"\x01" * 23),
               self.fua(104, 900, end=True, marker=True)]
        assert not self.proto(gop)._gop_has_idr()

    def test_a_whole_fragmented_idr_is_one(self):
        gop = [self.fua(102, 1200, start=True),
               self.fua(103, 900, end=True, marker=True)]
        assert self.proto(gop)._gop_has_idr()

    def test_an_unfragmented_idr_is_one(self):
        assert self.proto([self.rtp(102, b"\x65" + b"\xAA" * 900)])._gop_has_idr()

    def test_an_idr_inside_a_stap_a_is_one(self):
        inner = b"\x65" + b"\xAA" * 40
        stap = (b"\x78" + len(inner).to_bytes(2, "big") + inner)
        assert self.proto([self.rtp(102, stap)])._gop_has_idr()


class TestTheSameAnswerTwice:
    """Il 183 e il 200 OK devono portare LA STESSA risposta SDP.

    build_sdp tira chiavi SRTP nuove a ogni chiamata (os.urandom), quindi
    ricostruire la risposta per il 200 OK significa offrire chiavi DIVERSE da
    quelle già mandate nel 183 dello stesso dialogo — che RFC 3261 §13.2.1 non
    permette. Provato in strada: la targa rinegoziava il media, la luce del
    posto esterno si accendeva e spegneva, il video andava a scatti e l'audio
    non arrivava per niente.
    """

    def test_build_sdp_makes_new_keys_every_time(self):
        """La premessa del guasto, messa nero su bianco."""
        sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")
        primo = sip.build_sdp(enc=True, video=True)
        secondo = sip.build_sdp(enc=True, video=True)
        assert primo != secondo, "se un giorno diventassero uguali, il resto non serve più"

    # Che il 200 OK rimandi davvero il 183 lo prova test_sip_dialogs.py,
    # passando per handle_incoming_invite e do_answer_incoming veri.


class TestParameterSetsSurviveARestart:
    """SPS/PPS salvati su disco, perché il riavvio non li cancelli.

    Solo in memoria, la prima apertura dopo ogni riavvio di Home Assistant — e
    ogni aggiornamento dell'integrazione ne fa uno — restava ferma tre secondi
    se la targa aveva lasciato fuori l'SPS dal gruppo d'apertura: 3,09 s
    misurati il 26 settembre, contro i 0,07 di tutte le altre aperture.
    """

    SPS, PPS = b"\x67\x42\x80\x1f\xda", b"\x68\xce\x3c\x80"

    def test_saved_then_loaded(self, monkeypatch, tmp_path):
        path = str(tmp_path / "params.json")
        media._save_parameter_sets(path, self.SPS, self.PPS)
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        assert media.load_parameter_sets(path)
        assert (media._known_sps, media._known_pps) == (self.SPS, self.PPS)

    def test_a_missing_or_corrupt_file_changes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        assert not media.load_parameter_sets(str(tmp_path / "nope.json"))
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert not media.load_parameter_sets(str(bad))
        assert media._known_sps is None

    def test_something_that_is_not_an_sps_is_refused(self, monkeypatch, tmp_path):
        import base64, json
        monkeypatch.setattr(media, "_known_sps", None)
        p = tmp_path / "swapped.json"
        p.write_text(json.dumps({"sps": base64.b64encode(self.PPS).decode(),
                                 "pps": base64.b64encode(self.SPS).decode()}))
        assert not media.load_parameter_sets(str(p))
        assert media._known_sps is None

    def test_written_only_when_they_change(self, monkeypatch, tmp_path):
        writes = []
        monkeypatch.setattr(media, "_save_parameter_sets", lambda *a: writes.append(a))
        monkeypatch.setattr(media, "_params_path", str(tmp_path / "p.json"))
        monkeypatch.setattr(media, "_known_sps", None)
        monkeypatch.setattr(media, "_known_pps", None)
        proto = type("P", (), {"_last_sps": self.SPS, "_last_pps": self.PPS})()
        monkeypatch.setattr(media, "video_proto", proto)

        async def twice():
            media._remember_parameter_sets()
            media._remember_parameter_sets()
            await asyncio.sleep(0.05)
        import asyncio
        asyncio.run(twice())
        assert len(writes) == 1


class TestThePreviewSurvivesABadStart:
    """Un ffmpeg che apre il file e poi non parte lo lascia a zero byte.

    Il 26 settembre l'anteprima di Casa è sparita così: il codificatore JPEG si
    rifiutava di aprirsi, il file restava vuoto, e sulla scheda non c'era più
    l'ultimo fotogramma. Si tiene l'ultimo JPEG intero, e si ridà quello.
    """

    JPEG = b"\xff\xd8" + b"\x10" * 200 + b"\xff\xd9"

    def test_the_last_good_frame_survives_an_empty_file(self, monkeypatch, tmp_path):
        snap = tmp_path / "snap.jpg"
        monkeypatch.setattr(media, "SNAPSHOT_PATH", str(snap))
        monkeypatch.setattr(media, "_last_good_snapshot", None)
        snap.write_bytes(self.JPEG)
        assert media.read_snapshot() == self.JPEG
        snap.write_bytes(b"")                       # ffmpeg l'ha aperto e basta
        assert media.read_snapshot() == self.JPEG

    def test_a_half_written_jpeg_is_not_taken(self, monkeypatch, tmp_path):
        snap = tmp_path / "snap.jpg"
        monkeypatch.setattr(media, "SNAPSHOT_PATH", str(snap))
        monkeypatch.setattr(media, "_last_good_snapshot", None)
        snap.write_bytes(self.JPEG[:150])           # manca la coda FFD9
        assert media.read_snapshot() is None

    def test_no_file_no_frame(self, monkeypatch, tmp_path):
        monkeypatch.setattr(media, "SNAPSHOT_PATH", str(tmp_path / "missing.jpg"))
        monkeypatch.setattr(media, "_last_good_snapshot", None)
        assert media.read_snapshot() is None


class TestTwoPeopleOnTheSameCall:
    """Due viste sulla stessa chiamata: chi chiude per primo non la chiude a tutti.

    Il 26 settembre: suona un corriere, si risponde dall'iPhone, la compagna
    apre la stessa chiamata e poi la chiude — e in strada la chiamata cade. Il
    guardiano era uno solo: la seconda vista annullava quello della prima (che
    perdeva anche l'audio della strada, perché la sua sessione veniva
    rilasciata), e chiudendo la seconda si riagganciava.
    """

    def test_the_call_stays_until_the_last_view_closes(self, monkeypatch, tmp_path):
        import asyncio
        mod = pytest.importorskip("custom_components.vimar_intercom.hub")
        h = mod.VimarIntercomHub()
        h._auto_called = True
        monkeypatch.setattr(mod, "HOMEKIT_WATCH_START", 1.0)
        hangups = []

        async def fake_hangup():
            hangups.append(1)
        monkeypatch.setattr(mod.sip, "in_call", True, raising=False)
        monkeypatch.setattr(mod.sip, "call_state", {"call_id": "c1"}, raising=False)
        monkeypatch.setattr(mod.sip, "do_hangup", fake_hangup, raising=False)

        alive = set()
        monkeypatch.setattr(media, "homekit_ffmpeg_pids",
                            lambda sdp=None: [1] if sdp in alive else [])
        monkeypatch.setattr(media, "_homekit_sessions", {})
        sessions = []
        for sid in ("19206", "19210"):
            s = {"id": sid, "sdp": str(tmp_path / f"{sid}.sdp")}
            media._homekit_sessions[sid] = s
            alive.add(s["sdp"])
            sessions.append(s)

        async def main():
            first = h.start_homekit_watch(sessions[0])
            second = h.start_homekit_watch(sessions[1])
            await asyncio.sleep(0.5)
            assert not first.done(), "aprire la seconda vista non ferma la prima"

            alive.discard(sessions[1]["sdp"])          # la compagna chiude
            await asyncio.wait_for(second, 2)
            assert hangups == [], "la chiamata resta: c'è ancora chi guarda"
            assert "19206" in media._homekit_sessions, "la prima vista tiene l'audio"

            alive.discard(sessions[0]["sdp"])          # chiude anche l'ultimo
            await asyncio.wait_for(first, 2)
            assert hangups == [1], "chiusa l'ultima vista, si riaggancia"

        asyncio.run(main())
