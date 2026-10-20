"""Switch platform for Vimar Intercom — Segreteria e Non disturbare.

Comandi SIP (MESSAGE, header Panda: blue, verso l'SGA = SYSTEM.MAGIC_APT_INTERCOM,
55001 sull'impianto di riferimento, ma configurabile via options — vedi
runtime.SGA_TARGET / const.SGA_TARGET) [VERIFICATO 19/08/2026]:
  Segreteria      → VOICEMAIL;ON / VOICEMAIL;OFF
  Non disturbare  → DND;ON / DND;OFF

Lo stato è REALE: il Tab annuncia i cambi via SIP MESSAGE (sia da UI locale
che da app), quindi lo switch riflette lo stato effettivo e non è ottimistico.

Issue #9: su un impianto che non annuncia nulla (risponde 200 al comando senza
agire, o non manda mai VOICEMAIL;/DND;) lo switch mostrava per sempre l'ultimo
comando come se fosse lo stato. Ora il valore supposto dura CONFIRM_S dopo
l'invio, e intanto si rilegge lo stato (via HTTP in UDP locale; via SIP con
GET_INIT_STATUS solo se l'annuncio non arriva entro pochi secondi, #59); se nessuno
conferma, lo switch torna "sconosciuto". Al riavvio si riprende solo uno stato
che il Tab aveva confermato, non una supposizione.
Il comando è confermato sul campo: `VOICEMAIL;ON` → 55001 accende la segreteria
sul Tab (i vecchi tentativi verso 55002 davano 200 senza effetto = target sbagliato).
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import runtime as R
from .const import (
    DND_OFF,
    DND_ON,
    DOMAIN,
    SEGRETERIA_HEADER_NAME,
    SEGRETERIA_HEADER_VALUE,
    SEGRETERIA_OFF,
    SEGRETERIA_ON,
)
from .device import device_info, legacy_entity_id
from .hub import QUEUED

_LOGGER = logging.getLogger(__name__)

# Quanto resta visibile lo stato del comando appena inviato, in attesa che il Tab
# lo annunci o che la risposta a GET_INIT_STATUS lo confermi (sul 40507 l'annuncio
# arriva in meno di un secondo).
CONFIRM_S = 10.0


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
        VimarVoicemailSwitch(
            hub, entry.entry_id, key="segreteria", object_id="segreteria",
            icon="mdi:voicemail", target=R.SGA_TARGET,
            cmd_on=SEGRETERIA_ON, cmd_off=SEGRETERIA_OFF,
            state_attr="voicemail",
            hname=SEGRETERIA_HEADER_NAME, hvalue=SEGRETERIA_HEADER_VALUE),
        VimarModeSwitch(
            hub, entry.entry_id, key="dnd", object_id="non_disturbare",
            icon="mdi:bell-off", target=R.SGA_TARGET,
            cmd_on=DND_ON, cmd_off=DND_OFF, state_attr="dnd",
            hname="Panda", hvalue="blue"),
    ])


class VimarModeSwitch(SwitchEntity, RestoreEntity):
    """Switch per una modalità del Tab (segreteria / non disturbare)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, hub, entry_id: str, *, key: str, object_id: str, icon: str,
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
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}"
        self.entity_id = legacy_entity_id("switch", object_id)
        self._attr_device_info = device_info(entry_id)
        self._last_result: str | None = None
        # Stato confermato dal Tab prima del riavvio (attributo stato_reale salvato).
        self._restored: bool | None = None
        # Comando appena inviato: (valore, scadenza monotonic, mode_seq all'invio).
        # mode_seq cresce a ogni annuncio VOICEMAIL;/DND; e a ogni GET_INIT_STATUS_REPLY
        # che porta dnd/voicemail: una notizia arrivata dopo l'invio è la verità.
        self._pending: tuple[bool, float, int] | None = None
        self._expire_handle: asyncio.TimerHandle | None = None

    def _real(self) -> bool | None:
        return self._hub.stats.get(self._state_attr)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        # Solo uno stato confermato: fino alla 1.0.10 si riprendeva anche l'ultimo
        # comando supposto, che così sopravviveva ai riavvii (issue #9).
        if last is not None and isinstance(last.attributes.get("stato_reale"), bool):
            self._restored = last.attributes["stato_reale"]
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)
        self._cancel_expire()

    def _cancel_expire(self) -> None:
        if self._expire_handle:
            self._expire_handle.cancel()
            self._expire_handle = None

    @callback
    def _on_state_change(self) -> None:
        if self._pending is not None:
            wanted, _, seq_at_send = self._pending
            # Il Tab ha detto come stanno le cose dopo il comando (conferma o smentita).
            real = self._real()
            if real is not None and self._hub.stats.get("mode_seq", 0) != seq_at_send:
                self._pending = None
                self._cancel_expire()
                if real != wanted:
                    # A 200 OK says the MESSAGE was delivered, not that anyone
                    # acted on it: a wrong recipient answers 200 and ignores it.
                    _LOGGER.warning(
                        "%s: %s accepted %r (200 OK) but the state stayed %s. This "
                        "usually means the wrong recipient: these commands go to the "
                        "apartment's SGA, which is not the entrance panel on every "
                        "plant. Check \"sga_target\" in the integration options.",
                        self.entity_id, self._target,
                        self._cmd_on if wanted else self._cmd_off,
                        "ON" if real else "OFF")
        self.async_write_ha_state()

    @callback
    def _expire(self) -> None:
        self._expire_handle = None
        if self._pending is not None:
            if self._real() is None:
                _LOGGER.warning("%s: il citofono non ha confermato il comando in %.0f s, "
                                "stato sconosciuto", self.entity_id, CONFIRM_S)
            self._pending = None
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Disponibile appena registrati: HA non lascia comandare un'entità non
        disponibile, e sugli impianti che non annunciano mai lo stato (issue #9) lo
        switch resterebbe inutilizzabile. Stato ignoto = is_on None."""
        return bool(self._hub.registered)

    @property
    def is_on(self) -> bool | None:
        if self._pending is not None and time.monotonic() < self._pending[1]:
            return self._pending[0]
        real = self._real()
        if real is not None:
            return real
        return self._restored  # None = sconosciuto

    @property
    def assumed_state(self) -> bool:
        """Vero mentre lo stato mostrato non viene dal Tab: il comando appena inviato
        in attesa di conferma, o lo stato confermato prima del riavvio. Home Assistant
        lo segnala con i due pulsanti on/off al posto dell'interruttore."""
        return self._pending is not None or self._real() is None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "target": self._target,
            "comando_on": self._cmd_on,
            "comando_off": self._cmd_off,
            # L'ultimo stato confermato dal Tab (anche prima del riavvio): è quello
            # che il riavvio successivo riprende.
            "stato_reale": self._real() if self._real() is not None else self._restored,
            "ultimo_esito": self._last_result,
            "nota": "sip_command_to_sga",
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._send(self._cmd_on, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._send(self._cmd_off, False)

    async def _send(self, body: str, new_state: bool) -> None:
        # Letto PRIMA dell'invio: sul 40507 l'annuncio DND;OFF arriva mentre si aspetta
        # ancora il 200 del MESSAGE, e contato dopo sarebbe già "vecchio".
        seq = self._hub.stats.get("mode_seq", 0)
        ok, msg = await self._hub.async_send_command(
            body=body, target=self._target,
            header_name=self._hname or None, header_value=self._hvalue or None)
        self._last_result = msg
        # Fino alla 1.0.6 lo stato cambiava anche a comando fallito (404, timeout,
        # «Non registrato»): lo switch mostrava ON con la segreteria spenta, e
        # senza un annuncio del Tab a smentirlo lo stato falso sopravviveva anche
        # al riavvio (RestoreEntity). Ora solo un invio riuscito sposta lo stato
        # supposto; quello reale arriva comunque dall'annuncio VOICEMAIL;/DND;.
        if ok:
            self._pending = (new_state, time.monotonic() + CONFIRM_S, seq)
            self._cancel_expire()
            self._expire_handle = asyncio.get_running_loop().call_later(CONFIRM_S, self._expire)
            self._on_state_change()  # annuncio già arrivato durante l'invio: niente attesa
        _LOGGER.info("%s %s → ok=%s msg=%s", self.entity_id,
                     "ON" if new_state else "OFF", ok, msg)
        self.async_write_ha_state()
        if msg == QUEUED:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_queued")
        if not ok:
            # The entity's own translated name ("Do not disturb"); its entity_id only if it has none.
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="switch_failed", translation_placeholders={"entity": str(getattr(self, "name", None) or self.entity_id), "error": str(msg)})
        # Il Tab di solito annuncia il cambio da solo; chi non lo fa (#9) risponde a
        # GET_INIT_STATUS, che l'hub manda solo se l'annuncio non arriva (#59): ogni
        # GET_INIT_STATUS fa comparire «Configurazione modificata» sui telefoni.
        await self._hub.async_request_status(after_command=True)


class VimarVoicemailSwitch(VimarModeSwitch):
    """Segreteria: risponde il messaggio di HA (se configurato e attivo) o quella del Tab."""

    def _uses_ha(self) -> bool:
        return self._hub.away_enabled and R.away_message_configured()

    @property
    def is_on(self) -> bool | None:
        return True if self._uses_ha() else super().is_on

    @property
    def assumed_state(self) -> bool:
        return not self._uses_ha() and super().assumed_state

    @property
    def extra_state_attributes(self) -> dict:
        return {"modo": "Home Assistant" if self._uses_ha() else "Tab", **super().extra_state_attributes}

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and "modo" in last.attributes:
            # Nessun comando all'avvio: si riprende solo se il messaggio di HA era quello in uso.
            self._hub.set_away_enabled(last.attributes["modo"] == "Home Assistant"
                                       and not self._hub.stats.get("voicemail"))
        else:
            # Prima volta (o aggiornamento da prima che il messaggio di assenza fosse unito a
            # Segreteria, quando bastava averlo configurato): il messaggio di HA parte solo
            # quando l'utente accende Segreteria, non a ogni squillo.
            self._hub.set_away_enabled(False)
        await super().async_added_to_hass()

    async def async_turn_on(self, **kwargs) -> None:
        if R.away_message_configured():
            # Messaggio di HA: la segreteria del Tab va spenta, poi si attiva il nostro.
            await self._send(self._cmd_off, False)
            self._hub.set_away_enabled(True)
            self.async_write_ha_state()
            return
        await super().async_turn_on(**kwargs)
        self._hub.on_voicemail_on()

    async def async_turn_off(self, **kwargs) -> None:
        self._hub.set_away_enabled(False)
        await super().async_turn_off(**kwargs)
