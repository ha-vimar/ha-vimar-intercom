"""RTP e H.264 senza stato: riordino dei pacchetti e piccoli costruttori.

Niente socket e niente variabili di modulo: si prova tutto da solo.
"""


class ReorderBuffer:
    """Rimette in ordine i pacchetti RTP, tenendone da parte pochi.

    La versione di prima teneva anche i pacchetti arrivati DOPO che il loro
    posto era stato saltato: restavano nel buffer per sempre, lo riempivano,
    e lo svuotamento forzato avanzava di uno alla volta su tutto lo spazio a
    16 bit — decine di migliaia di giri sull'event loop — riemettendo roba
    vecchia e riportando indietro il numero atteso. In strada di pacchetti in
    ritardo ne arrivano, 73 e 83 in due chiamate.
    """

    # Oltre questo salto non è un pacchetto fuori posto: è un flusso nuovo
    # (altra chiamata, targa che riparte) e si riparte da lì.
    RESTART_GAP = 3000

    def __init__(self, size: int = 5) -> None:
        self.size = size
        self.pending: dict[int, bytes] = {}
        self.next: int | None = None
        self.late = 0

    def reset(self) -> None:
        self.pending.clear()
        self.next = None

    def push(self, seq: int, payload: bytes) -> list[tuple[int, bytes]]:
        """Aggiunge un pacchetto; restituisce quelli pronti, in ordine."""
        out: list[tuple[int, bytes]] = []
        if self.next is None:
            self.next = seq
        delta = (seq - self.next) & 0xFFFF
        if delta >= 0x8000:
            delta -= 0x10000          # negativo: dietro al punto atteso
        if abs(delta) > self.RESTART_GAP:
            out.extend(self._drain_all())
            self.next = seq
        elif delta < 0:
            # Dietro al punto atteso: il suo posto è già stato saltato, o è
            # un doppione. Emetterlo adesso sarebbe peggio che perderlo.
            self.late += 1
            return out
        self.pending[seq] = payload
        self._drain(out)
        if len(self.pending) > self.size:
            # Il buco non si riempirà: si salta al primo pacchetto che c'è.
            self.next = min(self.pending, key=lambda s: (s - self.next) & 0xFFFF)
            self._drain(out)
        return out

    def _drain(self, out: list) -> None:
        while self.next in self.pending:
            out.append((self.next, self.pending.pop(self.next)))
            self.next = (self.next + 1) & 0xFFFF

    def _drain_all(self) -> list[tuple[int, bytes]]:
        base = self.next or 0
        ordered = sorted(self.pending.items(), key=lambda kv: (kv[0] - base) & 0xFFFF)
        self.pending.clear()
        return ordered


def _packet_has_sps(rtp: bytes) -> bool:
    """Se questo pacchetto RTP porta un SPS, da solo o dentro uno STAP-A."""
    if len(rtp) < 13:
        return False
    hlen = 12 + (rtp[0] & 0x0F) * 4
    if len(rtp) <= hlen:
        return False
    nal = rtp[hlen] & 0x1F
    if nal == 7:
        return True
    if nal == 24:                              # STAP-A: guarda dentro
        i = hlen + 1
        while i + 2 <= len(rtp):
            size = int.from_bytes(rtp[i:i + 2], "big")
            if i + 2 < len(rtp) and (rtp[i + 2] & 0x1F) == 7:
                return True
            i += 2 + size
    return False


def _stap_a(sps: bytes, pps: bytes, template: bytes) -> bytes:
    """Un pacchetto RTP che porta SPS e PPS insieme, in STAP-A (RFC 6184 §5.7.1).

    Serve perché la targa non mette sempre l'SPS nello stesso gruppo del
    keyframe: misurato, due aperture di chiamata su sette avevano PPS e IDR ma
    nessun SPS. Con ``-c:v copy`` ffmpeg inoltra solo i NAL che riceve — quel
    che sta in ``sprop-parameter-sets`` diventa extradata e non finisce mai sul
    filo — quindi il telefono non ha di che decodificare e resta nero fino al
    gruppo successivo che ne porti uno: tre secondi, o sei se ne mancano due.

    Si riusa l'intestazione del pacchetto del PPS (stessi seq, marcatore
    temporale e SSRC), così la sequenza resta coerente.
    """
    hdr = bytearray(template[:12])
    hdr[0] = (hdr[0] & 0xF0)          # niente CSRC in quello che costruiamo
    nri = max(sps[0] & 0x60, pps[0] & 0x60)
    payload = bytes([nri | 24])       # STAP-A
    for nal in (sps, pps):
        payload += len(nal).to_bytes(2, "big") + nal
    return bytes(hdr) + payload
