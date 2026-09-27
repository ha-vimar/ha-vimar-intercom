"""La strada del dialogo: senza, un BYE non arriva da nessuna parte.

Il relay della Vimar mette CINQUE Record-Route in risposta a un INVITE. Chi
non li raccoglie manda le richieste in-dialogo al proxy della registrazione, e
quelle spariscono: nessuna risposta, e la chiamata resta aperta fino al
timeout del citofono — due minuti, per una chiamata dalla strada, con la luce
del posto esterno accesa e nessuno che possa più suonare.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")

# Presa dal traffico vero di questo impianto.
OK_200 = (
    "SIP/2.0 200 Ok\r\n"
    "Via: SIP/2.0/TLS 192.0.2.225:5070;received=198.51.100.65;branch=z9hG4bK8c\r\n"
    "Record-Route: <sip:127.0.0.1;r2=on;lr=on;ftag=2f3b56>\r\n"
    "Record-Route: <sip:192.0.2.17;r2=on;lr=on;ftag=2f3b56>\r\n"
    "Record-Route: <sip:192.0.2.17:5092;lr>\r\n"
    "Record-Route: <sips:198.51.100.65:39063;lr>\r\n"
    "Record-Route: <sips:63.34.36.117:7042;lr;fs-rport=7042>\r\n"
    "From: <sip:60999@127.0.0.1>;tag=2f3b56\r\n"
    "To: <sip:55001@127.0.0.1>;tag=ISKFxCC\r\n"
    "Call-ID: call-1b8fe3\r\n"
    "CSeq: 12 INVITE\r\n"
    "Contact: <sip:60002@127.0.0.1:6095>\r\n"
    "Content-Length: 0\r\n\r\n"
)


class TestParsing:
    def test_every_record_route_is_kept_in_order(self):
        _, hdrs, _, _ = sip._parse(OK_200)
        rr = hdrs["_record_route_all"]
        assert len(rr) == 5, "ce ne sono cinque, e contano tutte"
        assert rr[0].startswith("<sip:127.0.0.1"), "l'ordine di arrivo va preservato"
        assert rr[-1].startswith("<sips:63.34.36.117"), "il relay è l'ultimo"

    def test_several_on_one_line_are_split(self):
        """Possono arrivare anche separate da virgola sulla stessa riga."""
        msg = ("SIP/2.0 200 Ok\r\n"
               "Record-Route: <sip:a;lr>, <sip:b;lr>\r\n"
               "Content-Length: 0\r\n\r\n")
        _, hdrs, _, _ = sip._parse(msg)
        assert hdrs["_record_route_all"] == ["<sip:a;lr>", "<sip:b;lr>"]

    def test_absence_is_not_an_empty_string(self):
        msg = "SIP/2.0 200 Ok\r\nContent-Length: 0\r\n\r\n"
        _, hdrs, _, _ = sip._parse(msg)
        assert "_record_route_all" not in hdrs


class TestRouteHeader:
    def test_the_dialog_route_wins_over_the_registration_one(self):
        """Dentro un dialogo vale la sua strada, non quella della registrazione."""
        route_set = ["<sips:63.34.36.117:7042;lr>", "<sip:192.0.2.17:5092;lr>"]
        line = sip._route_line(route_set)
        assert line == ("Route: <sips:63.34.36.117:7042;lr>\r\n"
                        "Route: <sip:192.0.2.17:5092;lr>\r\n")
        assert line.count("Route:") == 2, "una riga per ogni salto, in ordine"

    def test_without_a_dialog_the_registration_route_is_used(self, monkeypatch):
        monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", False)
        line = sip._route_line(None)
        assert line.startswith("Route: <sip:") and line.endswith(";lr>\r\n")
        assert line.count("Route:") == 1

    def test_the_caller_reverses_the_list(self):
        """RFC 3261 §12.1.2: da chiamanti la strada si percorre al contrario."""
        _, hdrs, _, _ = sip._parse(OK_200)
        route_set = list(reversed(hdrs["_record_route_all"]))
        assert route_set[0].startswith("<sips:63.34.36.117"), (
            "il primo salto è il relay, che è l'ultimo Record-Route ricevuto")
        assert route_set[-1].startswith("<sip:127.0.0.1")


class TestInboundDialogTarget:
    """Il BYE di una chiamata RICEVUTA va al Contact, non al From.

    Mandandolo al From la targa risponde "481 Call/transaction does not
    exist" e la chiamata resta aperta — con il posto esterno occupato.
    """

    INVITE = (
        "INVITE sip:60999@198.51.100.65:51530;transport=tls SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 63.34.36.117:7042;branch=z9hG4bK.aa\r\n"
        "Record-Route: <sips:63.34.36.117:7042;lr>\r\n"
        "From: <sip:60001@127.0.0.1>;tag=cVdrL\r\n"
        "To: <sip:60999@127.0.0.1>\r\n"
        "Call-ID: abc-123\r\n"
        "CSeq: 20 INVITE\r\n"
        "Contact: <sip:60001@127.0.0.1:6100>\r\n"
        "Content-Length: 0\r\n\r\n"
    )

    def test_the_contact_is_extracted_not_the_from(self):
        _, hdrs, _, _ = sip._parse(self.INVITE)
        assert sip._angle(hdrs["contact"]) == "sip:60001@127.0.0.1:6100"
        assert sip._angle(hdrs["from"]) == "sip:60001@127.0.0.1", (
            "il From non porta la porta: è per questo che come bersaglio non vale")

    def test_angle_brackets_are_optional(self):
        assert sip._angle("sip:x@y") == "sip:x@y"
        assert sip._angle("  <sip:x@y>  ;tag=z") == "sip:x@y"

    def test_the_inbound_route_set_is_kept_in_arrival_order(self):
        _, hdrs, _, _ = sip._parse(self.INVITE)
        assert hdrs["_record_route_all"] == ["<sips:63.34.36.117:7042;lr>"]
