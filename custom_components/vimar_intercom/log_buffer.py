"""Logging del componente: buffer interno e inoltro al log di Home Assistant.

Modulo **puro** (solo `logging`), così i test lo esercitano senza HA. Viene
installato una volta da `__init__.py` con `install()`.

Due destinazioni, due regole:

* il **buffer circolare** (`debug_log`, servito da `/api/vimar_intercom/debug`)
  riceve tutto, DEBUG compreso;
* il **log di Home Assistant** riceve i record dal livello scelto dall'utente
  in su — WARNING se nessuno l'ha scelto.

Per distinguere i due casi il logger del pacchetto parte da un livello
«sentinella» (1, sotto DEBUG): finché resta lì nessuno l'ha toccato; se
`logger:` in configuration.yaml o il servizio `logger.set_level` lo cambiano,
quel livello diventa la soglia dell'inoltro. Fino alla 1.0.6 il logger era
fisso a DEBUG e l'inoltro fisso a WARNING: attivare il debug dall'interfaccia
non portava nel log di HA nessuna riga in più.

Entrambe le destinazioni passano da `redact()`: il log di HA finisce nei
report allegati alle issue, esattamente come il buffer.
"""

from __future__ import annotations

import logging

from .log_redact import redact

LOGGER_NAME = "custom_components.vimar_intercom"
LEVEL_PIN = 1
# Deve contenere una chiamata intera: con 200 righe il traffico per-pacchetto
# del video espelleva dal buffer proprio ciò che serve per capire un problema.
MAX_LINES = 3000

debug_log: list[str] = []


class DebugBufferHandler(logging.Handler):
    """Cattura i log del componente nel buffer circolare."""

    def emit(self, record):
        try:
            debug_log.append(redact(self.format(record)))
            if len(debug_log) > MAX_LINES:
                del debug_log[: len(debug_log) - MAX_LINES]
        except Exception:  # noqa: BLE001 - mai far fallire il logging
            pass


def forward_threshold(logger: logging.Logger) -> int:
    """Livello minimo inoltrato: WARNING se il livello non è stato scelto."""
    lvl = logger.level
    return logging.WARNING if lvl in (logging.NOTSET, LEVEL_PIN) else lvl


class ForwardToRootHandler(logging.Handler):
    """Inoltra al logger root (quello di HA) i record dalla soglia in su, oscurati."""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__()
        self._owner = logger

    def emit(self, record):
        if record.levelno < forward_threshold(self._owner):
            return
        try:
            clean = logging.makeLogRecord(record.__dict__)
            clean.msg = redact(record.getMessage())
            clean.args = None
        except Exception:  # noqa: BLE001 - meglio perdere la riga che mostrarla in chiaro
            return
        logging.getLogger().handle(clean)


def install(name: str = LOGGER_NAME) -> logging.Logger:
    """Collega i due handler al logger del pacchetto. Idempotente."""
    log = logging.getLogger(name)
    for h in list(log.handlers):
        if isinstance(h, (DebugBufferHandler, ForwardToRootHandler)):
            log.removeHandler(h)
    buf = DebugBufferHandler()
    buf.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(buf)
    log.addHandler(ForwardToRootHandler(log))
    log.setLevel(LEVEL_PIN)
    log.propagate = False  # l'inoltro al log di HA lo fa ForwardToRootHandler
    return log
