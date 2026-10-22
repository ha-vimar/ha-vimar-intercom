"""Recupero della registrazione SIP (v1.0.6).

Fino alla 1.0.5 il keepalive era racchiuso in `if sip.registered:`. Persa la
registrazione, il loop girava a vuoto per sempre: in UDP locale — il default —
non esisteva nessun altro percorso di recupero, e il citofono restava
scollegato fino al riavvio di Home Assistant. Peggio, `do_register()` non
azzerava `registered` quando falliva, quindi spesso quel flag restava `True` e
Home Assistant mostrava il citofono raggiungibile mentre non lo era.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod

sip = hub_mod.sip


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    return h


@pytest.fixture
def chiamate(monkeypatch):
    """Registra cosa il tick ha provato a fare, senza toccare la rete."""
    fatte = {"register": 0, "reconnect": 0, "init_status": 0}

    async def _register():
        fatte["register"] += 1
        return fatte.get("register_ok", True)

    async def _reconnect():
        fatte["reconnect"] += 1
        return fatte.get("reconnect_ok", True)

    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(sip, "reconnect", _reconnect)
    return fatte


def _tick(hub, fatte, monkeypatch):
    async def _init():
        fatte["init_status"] += 1
        hub._init_status_sent = True

    monkeypatch.setattr(hub, "_request_init_status", _init)
    asyncio.run(hub._keepalive_tick())


# ─── registrato: keepalive normale ───────────────────────────────────────────

def test_se_registrati_si_rinnova_e_basta(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", True, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["register"] == 1
    assert chiamate["reconnect"] == 0
    assert chiamate["init_status"] == 0, "lo stato iniziale c'era già: non si richiede"


# ─── non registrato: il caso che prima non esisteva ──────────────────────────

def test_se_non_registrati_si_tenta_il_recupero(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", False, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 1, "il loop girava a vuoto invece di riconnettersi"


def test_dopo_il_recupero_si_richiede_lo_stato(hub, chiamate, monkeypatch):
    """Mentre eravamo scollegati segreteria, DND e versione rubrica possono
    essere cambiati sul Tab: ripartire con i valori di prima è sbagliato."""
    monkeypatch.setattr(sip, "registered", False, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["init_status"] == 1


def test_recupero_fallito_conta_come_fallimento(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", False, raising=False)
    chiamate["reconnect_ok"] = False
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert hub.stats["register_failures"] == prima + 1
    assert chiamate["init_status"] == 0


def test_un_errore_non_ferma_il_loop(hub, monkeypatch):
    """Il tick è dentro un try: un'eccezione qui ucciderebbe il keepalive e
    riporterebbe al bug originale, solo per un'altra strada."""
    async def _esplode():
        raise RuntimeError("rete sparita")

    monkeypatch.setattr(sip, "registered", True, raising=False)
    monkeypatch.setattr(sip, "do_register", _esplode)

    asyncio.run(hub._keepalive_tick())  # non deve sollevare


# ─── registered, renewal fails: reconnect at once ────────────────────────────

def test_a_failed_renewal_reconnects_at_once(hub, chiamate, monkeypatch):
    """Waiting for the next tick left the intercom unreachable for 120 s."""
    monkeypatch.setattr(sip, "registered", True, raising=False)
    chiamate["register_ok"] = False
    hub._init_status_sent = True
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["register"] == 1 and chiamate["reconnect"] == 1
    assert hub.stats["register_failures"] == prima + 1
    assert chiamate["init_status"] == 1, "back after an outage: ask for the state again"
    assert hub.stats["last_register_time"] is not None


def test_a_failed_renewal_and_reconnect_count_once(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", True, raising=False)
    chiamate["register_ok"] = False
    chiamate["reconnect_ok"] = False
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 1
    assert hub.stats["register_failures"] == prima + 1, "one failed tick, one failure"
    assert chiamate["init_status"] == 0


@pytest.mark.parametrize("flag", ["in_call", "calling", "ringing"])
@pytest.mark.parametrize("retry_ok", [True, False])
def test_a_failed_renewal_during_a_call_never_reconnects(hub, chiamate, monkeypatch, flag, retry_ok):
    """A reconnect tears down the connection the live call (or the ring) runs on."""
    monkeypatch.setattr(sip, "registered", True, raising=False)
    if flag == "ringing":
        monkeypatch.setitem(sip.pending_incoming, "active", True)
    else:
        monkeypatch.setattr(sip, flag, True, raising=False)
    hub._init_status_sent = True
    answers = iter([False, retry_ok])

    async def _register():
        chiamate["register"] += 1
        return next(answers)

    monkeypatch.setattr(sip, "do_register", _register)
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 0
    assert chiamate["register"] == 2, "the REGISTER is retried once"
    assert hub.stats["register_failures"] == prima + 1, "one failed tick, one failure"


@pytest.mark.parametrize("flag", ["in_call", "calling", "ringing"])
def test_a_lapsed_registration_during_a_call_does_not_reconnect(hub, chiamate, monkeypatch, flag):
    """The next tick after a failed renewal (registered is False) must not drop the call or ring."""
    monkeypatch.setattr(sip, "registered", False, raising=False)
    if flag == "ringing":
        monkeypatch.setitem(sip.pending_incoming, "active", True)
    else:
        monkeypatch.setattr(sip, flag, True, raising=False)
    chiamate["register_ok"] = False
    hub._init_status_sent = True
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 0 and chiamate["register"] == 1
    assert hub.stats["register_failures"] == prima + 1, "one failed tick, one failure"


@pytest.mark.parametrize("registered", [False, True])
def test_a_register_during_a_call_joins_the_readers_reconnect(hub, chiamate, monkeypatch, registered):
    """The reader is reconnecting (the cloud dropped mid-call): a REGISTER from the
    keepalive would open a second connection and later cancel the good one.
    registered=True: the renewal fails while the reader starts over, the retry joins."""
    renewals = int(registered)  # REGISTERs sent before the reader's reconnect starts
    monkeypatch.setattr(sip, "registered", registered, raising=False)
    monkeypatch.setattr(sip, "in_call", True, raising=False)
    # the reader starts reconnecting once the renewals have gone out
    monkeypatch.setattr(sip, "reconnecting", lambda: chiamate["register"] >= renewals)
    chiamate["register_ok"] = False
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["register"] == renewals and chiamate["reconnect"] == 1
    assert chiamate["init_status"] == 1, "back after a drop: the Tab's state is asked again"


def test_the_fast_reconnect_joins_the_running_attempt(monkeypatch):
    """The keepalive and the reader share one reconnect: never two in parallel."""
    started = []

    async def _slow():
        started.append(True)
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(sip, "_reconnect", _slow)
    monkeypatch.setattr(sip, "_reconnect_task", None)

    async def _run():
        return await asyncio.gather(sip.reconnect(), sip.reconnect())

    assert asyncio.run(_run()) == [True, True]
    assert started == [True]


# ─── the lifetime the registrar grants ───────────────────────────────────────

def _ok200(extra: str) -> str:
    return ("SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP 192.0.2.5:5070;branch=z9hG4bKx\r\n"
            "From: <sip:12345@example.test>;tag=a\r\nTo: <sip:12345@example.test>;tag=b\r\n"
            "Call-ID: reg-1\r\nCSeq: 1 REGISTER\r\n" + extra + "Content-Length: 0\r\n\r\n")


@pytest.mark.parametrize("extra, expected", [
    ('Contact: <sip:12345@192.0.2.9:5070>;+sip.instance="<urn:uuid:other>";expires=30\r\n'
     'Contact: <sip:12345@192.0.2.5:5070>;+sip.instance="<urn:uuid:ours>";expires=90\r\n', 90),
    ("Expires: 60\r\n", 60),
    ("", None),
])
def test_the_granted_expiry_prefers_our_own_binding(monkeypatch, extra, expected):
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "ours")
    monkeypatch.setattr(sip, "MY_IP", None)
    _, hdrs, *_ = sip._parse(_ok200(extra))
    assert sip._granted_expires(hdrs) == expected


@pytest.mark.parametrize("expires, warned", [(60, True), (3600, False)])
def test_a_short_granted_expiry_is_a_warning(monkeypatch, caplog, expires, warned):
    async def _send_request(_msg, _cid):
        return [_ok200(f"Expires: {expires}\r\n")]

    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "_udp_sock", object())
    monkeypatch.setattr(sip, "_my_port", lambda: 5070)
    monkeypatch.setattr(sip, "_send_request", _send_request)
    monkeypatch.setattr(sip, "_record_bindings", lambda hdrs: None)
    monkeypatch.setattr(sip, "_set_registered", lambda v: None)
    with caplog.at_level("WARNING"):
        assert asyncio.run(sip.do_register())
    assert ("granted only" in caplog.text) is warned


# ─── renewing at the lifetime the registrar grants ───────────────────────────

@pytest.mark.parametrize("granted, delay", [
    (None, 120),   # the registrar did not say: upstream's cadence
    (3600, 120),   # a long grant never slows the renewal down
    (300, 120),    # 300 - 60 = 240, capped at 120
    (150, 120),    # 150 - min(60, 30) = 120
    (120, 96),     # 120 - min(60, 24): a fixed 60 s margin would renew at 60
    (60, 48),      # 60 - 12; a fixed margin would put it on the expiry (60 - 60 = 0)
    (10, 8),
    (5, 5),        # 5 - 1 = 4, raised to the 5 s floor
    (1, 5),
])
def test_the_renewal_delay_follows_the_grant(monkeypatch, granted, delay):
    monkeypatch.setattr(sip, "granted_expiry", granted)
    assert sip.renew_delay() == delay


def test_a_successful_register_remembers_the_grant(monkeypatch):
    async def _send_request(_msg, _cid):
        return [_ok200("Expires: 60\r\n")]

    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "_udp_sock", object())
    monkeypatch.setattr(sip, "_my_port", lambda: 5070)
    monkeypatch.setattr(sip, "_send_request", _send_request)
    monkeypatch.setattr(sip, "_record_bindings", lambda hdrs: None)
    monkeypatch.setattr(sip, "_set_registered", lambda v: None)
    monkeypatch.setattr(sip, "granted_expiry", None)
    monkeypatch.setattr(sip, "registered_at", 0.0)

    assert asyncio.run(sip.do_register())
    assert sip.granted_expiry == 60
    assert sip.registered_at > 0


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def _run_loop(hub, monkeypatch, *, grant, until, register_at=None):
    """Run the keepalive loop on a fake clock; return when each REGISTER went out
    (seconds from the start). `register_at`: a REGISTER done elsewhere (startup)
    while the loop is already waiting."""
    clock = _Clock()
    start = clock.now
    sent = []
    monkeypatch.setattr(hub_mod, "time", clock)

    def _granted():
        monkeypatch.setattr(sip, "registered", True, raising=False)
        monkeypatch.setattr(sip, "registered_at", clock.now)
        monkeypatch.setattr(sip, "granted_expiry", grant)

    async def _register():
        sent.append(round(clock.now - start, 3))
        _granted()
        return True

    async def _sleep(seconds):
        assert seconds > 0, "the loop must never spin"
        # A real sleep does not end early: the REGISTER happens during it.
        end = clock.now + seconds
        if register_at is not None and clock.now < start + register_at <= end:
            clock.now = start + register_at
            _granted()
        clock.now = end
        if clock.now - start >= until:
            hub._running = False

    async def _init():
        hub._init_status_sent = True

    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(hub_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(hub, "_request_init_status", _init)
    hub._init_status_sent = True
    hub._running = True
    if register_at is None:
        _granted()
    else:
        monkeypatch.setattr(sip, "registered", False, raising=False)
        monkeypatch.setattr(sip, "registered_at", 0.0)
        monkeypatch.setattr(sip, "granted_expiry", None)
    asyncio.run(hub._keepalive_loop())
    return sent


def test_a_60_s_grant_is_renewed_before_it_lapses(hub, monkeypatch):
    sent = _run_loop(hub, monkeypatch, grant=60, until=300)
    assert sent[0] == 48
    gaps = [b - a for a, b in zip([0, *sent], sent, strict=False)]
    assert all(g < 60 for g in gaps), sent


def test_a_60_s_grant_at_startup_is_honoured_by_the_waiting_loop(hub, monkeypatch):
    """The first REGISTER (the hub's startup) lands while the loop already waits:
    it must not keep its 120 s plan and let the 60 s binding lapse."""
    sent = _run_loop(hub, monkeypatch, grant=60, until=200, register_at=1)
    assert sent[0] == 49, sent


def test_a_3600_s_grant_keeps_the_120_s_cadence(hub, monkeypatch):
    sent = _run_loop(hub, monkeypatch, grant=3600, until=500)
    assert sent == [120, 240, 360, 480]


def test_without_a_registration_the_loop_retries_every_120_s(hub, chiamate, monkeypatch):
    clock = _Clock()
    start = clock.now
    ticks = []
    monkeypatch.setattr(hub_mod, "time", clock)
    monkeypatch.setattr(sip, "registered", False, raising=False)
    monkeypatch.setattr(sip, "registered_at", 0.0)

    async def _tick():
        ticks.append(clock.now - start)

    async def _sleep(seconds):
        clock.now += seconds
        if clock.now - start >= 250:
            hub._running = False

    monkeypatch.setattr(hub, "_keepalive_tick", _tick)
    monkeypatch.setattr(hub_mod.asyncio, "sleep", _sleep)
    hub._running = True
    asyncio.run(hub._keepalive_loop())
    assert ticks == [120, 240]
