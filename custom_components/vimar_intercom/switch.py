"""Switch platform for Vimar Intercom — Segreteria e Non disturbare.

Comandi SIP (MESSAGE, header Panda: blue, verso l'SGA = SYSTEM.MAGIC_APT_INTERCOM,
55001 sull'impianto di riferimento, ma configurabile via options — vedi
runtime.SGA_TARGET / const.SGA_TARGET) [VERIFICATO 19/08/2026]:
  Segreteria      → VOICEMAIL;ON / VOICEMAIL;OFF
  Non disturbare  → DND;ON / DND;OFF

Lo stato è REALE: il Tab annuncia i cambi via SIP MESSAGE (sia da UI locale
che da app), quindi lo switch riflette lo stato effettivo e non è ottimistico.
Il comando è confermato sul campo: `VOICEMAIL;ON` → 55001 accende la segreteria
sul Tab (i vecchi tentativi verso 55002 davano 200 senza effetto = target sbagliato).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    SEGRETERIA_ON, SEGRETERIA_OFF,
    SEGRETERIA_HEADER_NAME, SEGRETERIA_HEADER_VALUE,
    DND_ON, DND_OFF,
)
from .device import device_info
from . import runtime as R

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    # Target letto da runtime (SGA_TARGET): valore da options — manuale o
    # importato da rubrica.db — con fallback al default storico in const.py.
    # runtime.configure() è già stato chiamato da async_setup_entry() prima
    # di avviare le piattaforme, quindi il valore qui è già quello corrente.
    async_add_entities([
        VimarModeSwitch(
            hub, entry.entry_id, key="segreteria", name="Segreteria",
            icon="mdi:voicemail", target=R.SGA_TARGET,
            cmd_on=SEGRETERIA_ON, cmd_off=SEGRETERIA_OFF,
            state_attr="voicemail",
            hname=SEGRETERIA_HEADER_NAME, hvalue=SEGRETERIA_HEADER_VALUE),
        VimarModeSwitch(
            hub, entry.entry_id, key="dnd", name="Non Disturbare",
            icon="mdi:bell-off", target=R.SGA_TARGET,
            cmd_on=DND_ON, cmd_off=DND_OFF, state_attr="dnd",
            hname="Panda", hvalue="blue"),
    ])


# Quanto attendere l'annuncio spontaneo del Tab dopo un comando, e quanto
# attendere la risposta quando lo stato viene richiesto esplicitamente.
VERIFY_ANNOUNCE_WAIT = 2.0
VERIFY_REPLY_WAIT = 3.0


class VimarModeSwitch(SwitchEntity, RestoreEntity):
    """Switch per una modalità del Tab (segreteria / non disturbare)."""

    _attr_has_entity_name = False

    def __init__(self, hub, entry_id: str, *, key: str, name: str, icon: str,
                 target: str, cmd_on: str, cmd_off: str, state_attr: str,
                 hname: str | None, hvalue: str | None) -> None:
        self._hub = hub
        self._key = key
        self._target = target
        self._cmd_on = cmd_on
        self._cmd_off = cmd_off
        self._state_attr = state_attr
        self._hname = hname
        self._hvalue = hvalue
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = device_info(entry_id)
        self._is_on = False
        self._last_result: str | None = None
        self._verified: bool | None = None
        self._verify_task: asyncio.Task | None = None
        # Stato richiesto e non ancora confermato. Finché è valorizzato è lui a
        # comandare quello che si vede: senza, l'interruttore resta sullo stato
        # vecchio per i secondi della verifica e invita a premere di nuovo —
        # e ogni pressione è un comando in più che l'impianto esegue davvero.
        self._pending: bool | None = None

    def _real(self) -> bool | None:
        return self._hub.stats.get(self._state_attr)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            self._is_on = last.state == "on"
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()

    @callback
    def _on_state_change(self) -> None:
        real = self._real()
        if real is not None:
            self._is_on = real
            if self._pending is not None and real == self._pending:
                # Confermato dall'impianto: l'ottimismo ha esaurito il suo compito.
                self._pending = None
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        if self._pending is not None:
            return self._pending
        real = self._real()
        return real if real is not None else self._is_on

    @property
    def assumed_state(self) -> bool:
        """Vero finché il Tab non ha mai annunciato lo stato (issue #9).

        In quel caso lo stato mostrato è una supposizione — ripristinata dal
        riavvio precedente o dedotta dall'ultimo comando riuscito — e Home
        Assistant lo segnala mostrando i due pulsanti on/off al posto
        dell'interruttore, invece di presentarla come un fatto.
        """
        return self._real() is None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "target": self._target,
            "comando_on": self._cmd_on,
            "comando_off": self._cmd_off,
            "stato_reale": self._real(),
            "ultimo_esito": self._last_result,
            "ultimo_comando_verificato": self._verified,
            "nota": "Comando via SIP MESSAGE (Panda: blue) verso l'SGA; stato letto dagli annunci del Tab.",
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._send(self._cmd_on, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._send(self._cmd_off, False)

    async def _send(self, body: str, new_state: bool) -> None:
        ok, msg = await self._hub.async_send_command(
            body=body, target=self._target,
            header_name=self._hname or None, header_value=self._hvalue or None)
        self._last_result = msg
        if ok:
            self._is_on = new_state
        self._pending = new_state if ok else None
        _LOGGER.info("%s %s → ok=%s msg=%s", self._attr_name,
                     "ON" if new_state else "OFF", ok, msg)
        self.async_write_ha_state()
        if ok:
            if self._verify_task and not self._verify_task.done():
                self._verify_task.cancel()
            self._verify_task = asyncio.create_task(self._verify(new_state))

    async def _verify(self, expected: bool) -> None:
        """Controlla che il comando abbia avuto effetto, non solo che sia stato accettato.

        Un 200 OK dice che il messaggio è stato consegnato, non che qualcuno
        lo abbia eseguito: un target sbagliato risponde 200 e ignora tutto, ed
        è esattamente così che questi due comandi possono sembrare funzionanti
        mentre sul Tab non cambia nulla.
        """
        try:
            await asyncio.sleep(VERIFY_ANNOUNCE_WAIT)
            if self._real() == expected:
                self._set_verified(True)
                return
            # Nessun annuncio spontaneo: chiediamo esplicitamente lo stato.
            await self._hub.async_request_init_status()
            await asyncio.sleep(VERIFY_REPLY_WAIT)
            real = self._real()
            if real == expected:
                self._set_verified(True)
                return
            if real is None:
                self._pending = None
                self._last_result = (
                    f"{self._last_result} — stato non riportato dall'impianto, "
                    "impossibile verificare"
                )
                self._set_verified(None)
                _LOGGER.warning(
                    "%s: comando accettato ma l'impianto non riporta lo stato; "
                    "non è possibile confermare che abbia avuto effetto.",
                    self._attr_name,
                )
                return
            self._last_result = (
                f"{self._last_result} — nessun effetto: {self._target} ha risposto 200 "
                "ma lo stato non è cambiato"
            )
            self._set_verified(False)
            self._is_on = real
            _LOGGER.warning(
                "%s: %s ha accettato «%s» (200 OK) ma lo stato è rimasto %s. "
                "Di solito significa che il destinatario è sbagliato: questi comandi "
                "vanno all'SGA dell'appartamento, che non su tutti gli impianti "
                "coincide con la targa. Cambia «sga_target» nelle opzioni "
                "dell'integrazione.",
                self._attr_name, self._target,
                self._cmd_on if expected else self._cmd_off,
                "ON" if real else "OFF",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("%s: verifica non riuscita: %s", self._attr_name, e)

    def _set_verified(self, value: bool | None) -> None:
        self._verified = value
        # Verifica conclusa, in un senso o nell'altro: da qui in poi comanda
        # lo stato reale, non più quello che speravamo.
        self._pending = None
        self.async_write_ha_state()
