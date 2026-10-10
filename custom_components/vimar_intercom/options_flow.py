"""Options flow per Vimar Intercom: impostazioni, HomeKit, chiave /av e import della rubrica."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from . import away_config, cloud_phonebook, rest_client, rubrica_import, runtime, validate
from .config_flow import (
    DEFAULT_LOCAL_UDP_PORT,
    KEY_ACTUATORS,
    KEY_ALLOWED_USERS,
    KEY_AWAY_DELAY,
    KEY_AWAY_FILE,
    KEY_AWAY_TEXT,
    KEY_AWAY_TTS,
    KEY_CAMERA_TARGET,
    KEY_CLOUD_DOMAIN,
    KEY_CLOUD_PROXY,
    KEY_DOOR_TARGET,
    KEY_GID,
    KEY_INTERNAL_PANEL_TARGET,
    KEY_LOCAL_DOMAIN,
    KEY_LOCAL_PROXY,
    KEY_LOCAL_UDP_PORT,
    KEY_MEDIA_ENC,
    KEY_PICG_TARGET,
    KEY_RING_END_WEBHOOK_URL,
    KEY_RING_WEBHOOK_URL,
    KEY_SGA_TARGET,
    KEY_SIP_DOMAIN,
    KEY_SIP_PASSWORD,
    KEY_SIP_USER,
    KEY_SNAP_DELAY,
    KEY_SNAP_DIR,
    KEY_USE_LOCAL_UDP,
    KEY_VIEW_KA,
    KEY_VOICE_ANSWER,
    _test_sip_registration,
    _validate_ip,
)
from .const import (
    AWAY_TEXT_MAX,
    CAMERA_TARGET,
    CONF_HOMEKIT_ACCESSORY,
    CONF_HOMEKIT_ANSWER,
    CONF_HOMEKIT_RING_BUTTON,
    CONF_HOMEKIT_SMOOTH,
    CONF_VIDEO_BANDWIDTH,
    DEFAULT_HOMEKIT_ACCESSORY,
    DEFAULT_HOMEKIT_ANSWER,
    DEFAULT_HOMEKIT_RING_BUTTON,
    DEFAULT_HOMEKIT_SMOOTH,
    DEFAULT_SNAPSHOT_DELAY,
    DEFAULT_VIDEO_BANDWIDTH,
    DOMAIN,
    HOMEKIT_ANSWER_OPEN,
    HOMEKIT_ANSWER_TALK,
    HOMEKIT_DATA,
    HOMEKIT_QR_URL,
    INTERNAL_PANEL_TARGET,
    MY_NAME,
    PICG_TARGET,
    SGA_TARGET,
    VIDEO_BANDWIDTH_AUTO,
    VIDEO_BANDWIDTH_HIGH,
    VIDEO_BANDWIDTH_LOW,
)
from .runtime import (
    MEDIA_ENC_MODES,
    VOICE_ANSWER_MODES,
    media_enc_mode,
    view_keepalive_default,
    voice_answer_mode,
)

_LOGGER = logging.getLogger(__name__)

KEY_AWAY_UPLOAD = "away_message_upload"  # solo nel form: non si salva nelle opzioni


# Icone ammesse per gli attuatori dinamici (vedi tools/parse_rubrica.py)
ALLOWED_ACTUATOR_ICONS = ("door", "light", "switch")


class InvalidActuatorTarget(ValueError):
    """Target di un attuatore non numerico (né AUTO): errore «invalid_actuator_target»."""


def _parse_actuators(raw: str) -> list[dict]:
    """Valida la lista attuatori incollata come JSON nell'options flow.

    Solleva ValueError con un messaggio parlante se il JSON non è una lista di
    dict con le chiavi ``name``, ``msg``, ``target``, ``icon`` (icon nel set
    ammesso, target numerico o AUTO). Stringa vuota → lista vuota (nessun bottone).
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"JSON non valido: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("La radice deve essere una lista JSON")
    result: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Elemento #{i} non è un oggetto JSON")
        missing = [k for k in ("name", "msg", "target", "icon") if k not in item]
        if missing:
            raise ValueError(f"Elemento #{i}: chiavi mancanti {missing}")
        icon = item["icon"]
        if icon not in ALLOWED_ACTUATOR_ICONS:
            raise ValueError(
                f"Elemento #{i}: icon '{icon}' non valida "
                f"(ammesse: {', '.join(ALLOWED_ACTUATOR_ICONS)})"
            )
        # Stessa regola di hub.sip_uri: il target finisce nella request line del
        # MESSAGE. "AUTO" = la targa dell'apri-porta (button.py).
        target = str(item["target"]).strip()
        if target.upper() != "AUTO" and not validate.sip_target(target):
            raise InvalidActuatorTarget(f"elemento #{i}, target '{target}'")
        result.append({
            "name":   str(item["name"]),
            "msg":    str(item["msg"]),
            "target": target,
            "icon":   str(icon),
        })
    return result


def _inside(path: str, folder: str) -> bool:
    """True se path è folder o sta sotto (link risolti; /config/www2 non conta)."""
    return Path(path).resolve().is_relative_to(Path(folder).resolve())


def _homekit_pairing_text(hass, entry_id) -> str:
    """The setup code and QR of the HomeKit doorbell while it waits to be
    paired, for the HomeKit options page.

    Only here: the options are for administrators, and pairing gives live
    video, Talk and the gate, which ``allowed_users`` keeps from other users.
    The QR is an image behind an administrators-only view; the signed path
    authenticates the browser as whoever opened this page."""
    try:
        info = hass.data.get(HOMEKIT_DATA, {}).get("pairing", {}).get(entry_id)
    except AttributeError:
        return ""
    if not isinstance(info, dict) or not info.get("pin"):
        return ""
    italian = str(getattr(hass.config, "language", "") or "").startswith("it")
    if italian:
        text = ("Nell'app Casa: **Aggiungi accessorio**, poi inquadra il QR oppure scegli "
                "*Altre opzioni* e inserisci il codice:")
    else:
        text = ("In the Home app: **Add Accessory**, then scan the QR code or choose "
                "*More options* and enter the code:")
    text += f"\n\n## {info['pin']}"
    if info.get("svg") and info.get("token"):
        try:
            from homeassistant.components.http.auth import async_sign_path  # noqa: PLC0415

            path = async_sign_path(hass, f"{HOMEKIT_QR_URL}?t={info['token']}",
                                   timedelta(minutes=10))
            text += f"\n\n![QR]({path})"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit: pairing QR link not signed")
    return text


# ─── Settings form, one helper per section (same keys, order and defaults) ──


def _network_schema(form: dict) -> dict:
    return {
        vol.Required(
            "local_proxy",
            default=form.get(KEY_LOCAL_PROXY, "")
        ): str,
        vol.Optional(
            "use_local_udp",
            default=form.get(KEY_USE_LOCAL_UDP, True)
        ): bool,
        vol.Optional(
            "local_udp_port",
            default=form.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)
        ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
        vol.Optional(
            "video_enabled",
            default=form.get("video_enabled", True),
        ): bool,
        # auto (segue il media_enc dichiarato dall'impianto) / on / off (issue #4).
        vol.Optional(
            KEY_MEDIA_ENC,
            default=media_enc_mode(form.get(KEY_MEDIA_ENC)),
        ): selector.SelectSelector(selector.SelectSelectorConfig(
            options=list(MEDIA_ENC_MODES),
            translation_key="media_enc",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )),
        # Chi può rispondere a voce su /audio_ws: dichiarato / mai / chiunque.
        vol.Optional(
            KEY_VOICE_ANSWER,
            default=voice_answer_mode(form.get(KEY_VOICE_ANSWER)),
        ): selector.SelectSelector(selector.SelectSelectorConfig(
            options=list(VOICE_ANSWER_MODES),
            translation_key="voice_answer",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )),
    }


def _targets_schema(form: dict) -> dict:
    return {
        vol.Optional(
            KEY_SGA_TARGET,
            default=form.get(KEY_SGA_TARGET) or SGA_TARGET,
        ): str,
        vol.Optional(
            KEY_PICG_TARGET,
            default=form.get(KEY_PICG_TARGET) or PICG_TARGET,
        ): str,
        # Empty is a valid value: the panel learned from the last ring
        # with video (hub._camera_fallback), else 55100. Pre-filling 55100
        # saved it on the first save and the fallback never ran again.
        vol.Optional(
            KEY_CAMERA_TARGET,
            default=form.get(KEY_CAMERA_TARGET) or "",
        ): str,
        vol.Optional(
            KEY_INTERNAL_PANEL_TARGET,
            default=form.get(KEY_INTERNAL_PANEL_TARGET) or INTERNAL_PANEL_TARGET,
        ): str,
        # Nessun default fisso: vuoto è un valore valido (ripiego in
        # runtime.configure), non «55001».
        vol.Optional(
            KEY_DOOR_TARGET,
            default=form.get(KEY_DOOR_TARGET) or "",
        ): str,
    }


def _away_schema(form: dict) -> dict:
    return {
        vol.Optional(
            KEY_AWAY_FILE,
            default=form.get(KEY_AWAY_FILE, ""),
        ): str,
        # Caricato qui finisce in <media>/citofono/messaggi e diventa il file scelto.
        vol.Optional(KEY_AWAY_UPLOAD): selector.FileSelector(
            selector.FileSelectorConfig(accept=",".join(away_config.AUDIO_EXT))
        ),
        vol.Optional(
            KEY_AWAY_TEXT,
            default=form.get(KEY_AWAY_TEXT, ""),
        ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        # Niente default: un EntitySelector non accetta "" (vuoto = motore
        # predefinito di HA); il valore salvato torna come suggerimento.
        vol.Optional(
            KEY_AWAY_TTS,
            description={"suggested_value": form.get(KEY_AWAY_TTS) or None},
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="tts")),
        vol.Optional(
            KEY_AWAY_DELAY,
            default=form.get(KEY_AWAY_DELAY, 0),
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
    }


def _photos_schema(form: dict) -> dict:
    return {
        vol.Optional(
            KEY_SNAP_DIR,
            default=form.get(KEY_SNAP_DIR, ""),
        ): str,
        vol.Optional(
            KEY_SNAP_DELAY,
            default=form.get(KEY_SNAP_DELAY, DEFAULT_SNAPSHOT_DELAY),
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=30)),
        vol.Optional(
            KEY_VIEW_KA,
            default=form.get(KEY_VIEW_KA, view_keepalive_default(
                form.get(KEY_USE_LOCAL_UDP, True))),
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
        # Every video from the panel: card, camera, /av and HomeKit.
        vol.Optional(
            CONF_VIDEO_BANDWIDTH,
            default=form.get(CONF_VIDEO_BANDWIDTH, DEFAULT_VIDEO_BANDWIDTH),
        ): selector.SelectSelector(selector.SelectSelectorConfig(
            options=[VIDEO_BANDWIDTH_AUTO, VIDEO_BANDWIDTH_LOW, VIDEO_BANDWIDTH_HIGH],
            translation_key="video_bandwidth",
            mode=selector.SelectSelectorMode.LIST,
        )),
    }


def _access_schema(form: dict, users: list[dict]) -> dict:
    return {
        vol.Optional(
            KEY_ALLOWED_USERS,
            default=[u for u in form.get(KEY_ALLOWED_USERS) or [] if any(u == x["value"] for x in users)],
        ): selector.SelectSelector(selector.SelectSelectorConfig(
            options=users, multiple=True, mode="list")),  # caselle, non un menu
        # password: l'URL può portare un token segreto (es. Scrypted), non va
        # mostrato in chiaro nel form.
        vol.Optional(
            KEY_RING_WEBHOOK_URL,
            default=form.get(KEY_RING_WEBHOOK_URL, ""),
        ): selector.TextSelector(selector.TextSelectorConfig(type="password")),
        vol.Optional(
            KEY_RING_END_WEBHOOK_URL,
            default=form.get(KEY_RING_END_WEBHOOK_URL, ""),
        ): selector.TextSelector(selector.TextSelectorConfig(type="password")),
    }


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Modifica le impostazioni di rete senza re-inserire le credenziali."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._actuators_error: str | None = None
        self._rubrica_error: str | None = None
        self._imported: dict | None = None
        self._imported_gid: str = "101"
        # PICG dichiarato dal citofono stesso (get_info.php?action=nickname).
        # Resta None quando la rubrica arriva da un file caricato a mano.
        self._picg_from_rest: str | None = None

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Menu: impostazioni a mano, rubrica dal citofono, o file rubrica.db."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "homekit", "av_key", "fetch_rubrica", "fetch_rubrica_cloud",
                          "import_rubrica"],
        )

    async def async_step_settings(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        # Le options già salvate hanno precedenza sui dati iniziali dell'entry.
        current = {**self._entry.data, **self._entry.options}

        # Valore di default del campo attuatori: la lista già salvata, serializzata
        # in JSON leggibile così l'utente la ritrova e può modificarla.
        actuators_default = json.dumps(
            current.get(KEY_ACTUATORS, []), ensure_ascii=False, indent=2
        )

        if user_input is not None:
            local_proxy, use_local_udp, local_udp_port, media_enc, voice_answer = (
                self._settings_network(user_input))
            actuators_raw  = user_input.get(KEY_ACTUATORS, "")
            actuators_default = actuators_raw  # rimostra ciò che l'utente ha scritto
            targets = self._settings_targets(user_input, errors)
            away_file, away_text, away_tts, away_delay = await self._settings_away(user_input, errors)
            allowed_users, ring_webhook_url, ring_end_webhook_url = self._settings_access(
                user_input, errors)
            snap_dir, snap_delay, view_ka = await self._settings_photos(
                user_input, current, use_local_udp, errors)
            actuators = self._settings_actuators(actuators_raw, errors)
            await self._settings_sip_test(
                current, local_proxy, use_local_udp, local_udp_port, errors)

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        # Options this page does not show (HomeKit) keep their
                        # value; without this, saving the network settings
                        # turned the HomeKit doorbell off.
                        **self._entry.options,
                        KEY_LOCAL_PROXY:    local_proxy,
                        KEY_USE_LOCAL_UDP:  use_local_udp,
                        KEY_LOCAL_UDP_PORT: local_udp_port,
                        KEY_MEDIA_ENC:      media_enc,
                        "video_enabled": user_input.get(
                            "video_enabled", current.get("video_enabled", True)),
                        KEY_VOICE_ANSWER:   voice_answer,
                        KEY_ACTUATORS:      actuators,
                        **targets,
                        KEY_AWAY_FILE:      away_file,
                        KEY_AWAY_TEXT:      away_text,
                        KEY_AWAY_TTS:       away_tts,
                        KEY_AWAY_DELAY:     away_delay,
                        KEY_SNAP_DIR:       snap_dir,
                        KEY_SNAP_DELAY:     snap_delay,
                        KEY_VIEW_KA:        view_ka,
                        CONF_VIDEO_BANDWIDTH: user_input.get(
                            CONF_VIDEO_BANDWIDTH,
                            current.get(CONF_VIDEO_BANDWIDTH, DEFAULT_VIDEO_BANDWIDTH)),
                        KEY_ALLOWED_USERS:  allowed_users,
                        KEY_RING_WEBHOOK_URL:     ring_webhook_url,
                        KEY_RING_END_WEBHOOK_URL: ring_end_webhook_url,
                    },
                )

        # Il form si ricompone su ciò che l'utente ha appena inviato, non solo
        # sui valori salvati: ricostruirlo da `current` scarterebbe in silenzio
        # tutte le altre modifiche fatte insieme a quella che non ha passato la
        # validazione. `current` resta intatto perché serve ai confronti
        # (sip_changed in _settings_sip_test) per capire cosa è davvero cambiato.
        form = {**current, **(user_input or {})}
        # HA non ha un selettore di utenti: elenco a scelta multipla dagli utenti veri
        # (non quelli di sistema). Un utente cancellato sparisce dalla lista al salvataggio.
        users = [{"value": u.id, "label": u.name or u.id}
                 for u in await self.hass.auth.async_get_users() if not u.system_generated]

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({
                **_network_schema(form),
                vol.Optional(
                    KEY_ACTUATORS,
                    default=actuators_default,
                ): str,
                **_targets_schema(form),
                **_away_schema(form),
                **_photos_schema(form),
                **_access_schema(form, users),
            }),
            errors=errors,
            description_placeholders={
                "actuators_error": getattr(self, "_actuators_error", "") or "",
            },
        )

    # ─── Settings, one helper per section: read and validate user_input ─────

    @staticmethod
    def _settings_network(user_input: dict) -> tuple:
        local_proxy    = user_input.get("local_proxy", "").strip()
        use_local_udp  = user_input.get("use_local_udp", True)
        local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))
        media_enc      = media_enc_mode(user_input.get(KEY_MEDIA_ENC))
        voice_answer   = voice_answer_mode(user_input.get(KEY_VOICE_ANSWER))
        return local_proxy, use_local_udp, local_udp_port, media_enc, voice_answer

    @staticmethod
    def _settings_targets(user_input: dict, errors: dict[str, str]) -> dict:
        # SGA/PICG: id SIP numerici (es. "55001"). Campo vuoto → fallback al
        # default storico in const.py (gestito da runtime.configure()), quindi
        # qui basta validare il formato quando l'utente scrive qualcosa.
        targets = {}
        for key in (KEY_SGA_TARGET, KEY_PICG_TARGET, KEY_CAMERA_TARGET,
                    KEY_INTERNAL_PANEL_TARGET, KEY_DOOR_TARGET):
            targets[key] = str(user_input.get(key, "")).strip()
            if targets[key] and not validate.sip_target(targets[key]):
                errors[key] = "invalid_target"
        return targets

    async def _settings_away(self, user_input: dict, errors: dict[str, str]) -> tuple:
        away_file  = str(user_input.get(KEY_AWAY_FILE, "")).strip()
        away_text  = str(user_input.get(KEY_AWAY_TEXT, "")).strip()
        away_tts   = str(user_input.get(KEY_AWAY_TTS) or "").strip()
        away_delay = user_input.get(KEY_AWAY_DELAY, 0)
        if upload_id := user_input.get(KEY_AWAY_UPLOAD):
            def _save() -> str:
                with process_uploaded_file(self.hass, upload_id) as file_path:
                    return away_config.save_upload(file_path, away_config.messages_dir(self.hass))

            try:
                away_file = await self.hass.async_add_executor_job(_save)
            except (ValueError, OSError) as exc:
                if str(exc) in ("upload_bad_type", "upload_too_big"):
                    errors[KEY_AWAY_UPLOAD] = str(exc)
                else:  # e.g. an expired upload: ValueError("File does not exist")
                    _LOGGER.warning("Away message upload not saved: %s", exc, exc_info=isinstance(exc, OSError))
                    errors[KEY_AWAY_UPLOAD] = "upload_failed"
            else:
                # Il file è già nella cartella: se il form torna per un altro errore
                # mostra il nuovo percorso e non ricarica un upload già consumato.
                user_input[KEY_AWAY_FILE] = away_file
                user_input.pop(KEY_AWAY_UPLOAD)
        if len(away_text) > AWAY_TEXT_MAX:
            errors[KEY_AWAY_TEXT] = "text_too_long"
        # Come snapshot_dir: solo cartelle che HA può leggere (allowlist_external_dirs,
        # media). Il percorso va dritto a `ffmpeg -i`.
        if away_file and not self.hass.config.is_allowed_path(away_file):
            errors[KEY_AWAY_FILE] = "file_not_allowed"
        elif away_file and not await self.hass.async_add_executor_job(os.path.isfile, away_file):
            errors[KEY_AWAY_FILE] = "file_not_found"
        return away_file, away_text, away_tts, away_delay

    @staticmethod
    def _settings_access(user_input: dict, errors: dict[str, str]) -> tuple:
        allowed_users = [str(u) for u in user_input.get(KEY_ALLOWED_USERS) or []]
        ring_webhook_url     = str(user_input.get(KEY_RING_WEBHOOK_URL) or "").strip()
        ring_end_webhook_url = str(user_input.get(KEY_RING_END_WEBHOOK_URL) or "").strip()
        for key, url in ((KEY_RING_WEBHOOK_URL, ring_webhook_url),
                         (KEY_RING_END_WEBHOOK_URL, ring_end_webhook_url)):
            if not validate.http_url(url):
                errors[key] = "invalid_url"
        return allowed_users, ring_webhook_url, ring_end_webhook_url

    async def _settings_photos(
        self, user_input: dict, current: dict, use_local_udp: bool, errors: dict[str, str]
    ) -> tuple:
        snap_dir   = str(user_input.get(KEY_SNAP_DIR, "")).strip()
        snap_delay = user_input.get(KEY_SNAP_DELAY, DEFAULT_SNAPSHOT_DELAY)
        view_ka    = user_input.get(KEY_VIEW_KA, view_keepalive_default(use_local_udp))
        if (use_local_udp != current.get(KEY_USE_LOCAL_UDP, True)
                and view_ka == view_keepalive_default(current.get(KEY_USE_LOCAL_UDP, True))):
            view_ka = view_keepalive_default(use_local_udp)  # era il predefinito: segue la modalità
        if snap_dir and not self.hass.config.is_allowed_path(snap_dir):
            errors[KEY_SNAP_DIR] = "path_not_allowed"
        elif snap_dir and await self.hass.async_add_executor_job(
                _inside, snap_dir, self.hass.config.path("www")):
            # /config/www è servita su /local SENZA login: la foto della strada
            # finirebbe leggibile da internet.
            errors[KEY_SNAP_DIR] = "path_public"
        return snap_dir, snap_delay, view_ka

    def _settings_actuators(self, actuators_raw: str, errors: dict[str, str]) -> list[dict]:
        actuators: list[dict] = []
        try:
            actuators = _parse_actuators(actuators_raw)
        except ValueError as exc:
            errors[KEY_ACTUATORS] = ("invalid_actuator_target" if isinstance(exc, InvalidActuatorTarget)
                                     else "invalid_actuators")
            self._actuators_error = str(exc)
        return actuators

    async def _settings_sip_test(
        self, current: dict, local_proxy: str, use_local_udp: bool, local_udp_port: int,
        errors: dict[str, str],
    ) -> None:
        # Il test SIP live va fatto solo se cambiano davvero i parametri SIP:
        # rifarlo a ogni salvataggio (es. modifica solo attuatori) fallirebbe per
        # conflitto con l'integrazione già registrata e bloccherebbe il salvataggio.
        sip_changed = (
            local_proxy    != current.get(KEY_LOCAL_PROXY, "")
            or use_local_udp  != current.get(KEY_USE_LOCAL_UDP, True)
            or local_udp_port != current.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)
        )
        if not _validate_ip(local_proxy):
            errors["local_proxy"] = "invalid_ip"
        elif use_local_udp and sip_changed:
            ok, msg = await _test_sip_registration(
                sip_user      = current["sip_user"],
                sip_password  = current["sip_password"],
                # The domain runtime.configure uses in local UDP mode
                # (as _local_test_domain in the setup flow).
                sip_domain    = current.get(KEY_LOCAL_DOMAIN) or current["sip_domain"],
                local_proxy   = local_proxy,
                local_udp_port = local_udp_port,
                device_imei   = str(current.get("device_imei") or ""),
                device_uuid   = str(current.get("device_uuid") or ""),
                device_name   = str(current.get("device_name") or MY_NAME),
                # The entry is set up and registered with this identity:
                # do not leave its binding on the test's closed socket.
                unregister    = True,
            )
            if not ok:
                errors["local_proxy"] = "sip_registration_failed"
            else:
                # The test's unregister may have taken the live binding
                # with it: register again now, whether or not the form is
                # saved (saving reloads and registers anyway).
                entry_data = getattr(self.hass, "data", {}).get(DOMAIN, {}).get(
                    getattr(self._entry, "entry_id", None), {})
                hub = entry_data.get("hub") if isinstance(entry_data, dict) else None
                if hub is not None:
                    self.hass.async_create_background_task(
                        hub.async_register_now(), "vimar_intercom re-register")

    async def async_step_homekit(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """The HomeKit video doorbell: on/off, video mode, when a ring is answered."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self._entry.options,
                    CONF_HOMEKIT_ACCESSORY: bool(user_input.get(CONF_HOMEKIT_ACCESSORY)),
                    CONF_HOMEKIT_SMOOTH: bool(user_input.get(CONF_HOMEKIT_SMOOTH)),
                    CONF_HOMEKIT_ANSWER: user_input.get(
                        CONF_HOMEKIT_ANSWER, DEFAULT_HOMEKIT_ANSWER),
                    CONF_HOMEKIT_RING_BUTTON: bool(user_input.get(CONF_HOMEKIT_RING_BUTTON)),
                },
            )
        current = self._entry.options
        return self.async_show_form(
            step_id="homekit",
            description_placeholders={"pairing": _homekit_pairing_text(
                getattr(self, "hass", None), getattr(self._entry, "entry_id", None))},
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_HOMEKIT_ACCESSORY,
                    default=current.get(CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY),
                ): bool,
                vol.Optional(
                    CONF_HOMEKIT_SMOOTH,
                    default=current.get(CONF_HOMEKIT_SMOOTH, DEFAULT_HOMEKIT_SMOOTH),
                ): bool,
                vol.Optional(
                    CONF_HOMEKIT_ANSWER,
                    default=current.get(CONF_HOMEKIT_ANSWER, DEFAULT_HOMEKIT_ANSWER),
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[HOMEKIT_ANSWER_TALK, HOMEKIT_ANSWER_OPEN],
                    translation_key="homekit_answer",
                    mode=selector.SelectSelectorMode.LIST,
                )),
                vol.Optional(
                    CONF_HOMEKIT_RING_BUTTON,
                    default=current.get(CONF_HOMEKIT_RING_BUTTON, DEFAULT_HOMEKIT_RING_BUTTON),
                ): bool,
            }),
        )

    async def async_step_av_key(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """The /av key (#63): shown for go2rtc/Frigate/Scrypted, and regenerated on request.

        A new key goes in the entry data (not the options) and the entry reloads, so
        the camera hands HA's stream worker the new URL; the old key stops working.
        """
        if user_input is not None:
            if user_input.get("regenerate"):
                key = runtime.new_av_key()
                self.hass.config_entries.async_update_entry(
                    self._entry, data={**self._entry.data, "av_key": key})
                runtime.AV_KEY = key
                self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
            return self.async_create_entry(title="", data=dict(self._entry.options))
        key = str(self._entry.data.get("av_key") or "")
        return self.async_show_form(
            step_id="av_key",
            description_placeholders={
                "param": runtime.AV_KEY_PARAM,
                "key": key or "-",
            },
            data_schema=vol.Schema({vol.Optional("regenerate", default=False): bool}),
        )

    async def async_step_fetch_rubrica(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Scarica la rubrica **dal citofono**, senza doverla estrarre a mano.

        Usa l'API HTTP che il citofono espone in rete locale (`rest_client`)
        con le stesse credenziali SIP già nel config entry: niente token,
        niente account Vimar, niente cloud. Nella stessa occasione chiede i
        nickname, così il PICG lo **dichiara il citofono** invece di doverlo
        indovinare o leggere dalla rubrica.

        Funziona solo se il citofono è raggiungibile in LAN sulla porta 80; per
        gli impianti che si raggiungono solo dal cloud resta la voce «importa
        da file».
        """
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        host = (current.get(KEY_LOCAL_PROXY) or "").strip()
        default_gid = str(current.get(KEY_GID) or "101")

        if not host:
            # Senza indirizzo del citofono non c'è niente da contattare.
            errors["base"] = "no_local_proxy"
        elif user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid
            sip_user = current.get(KEY_SIP_USER, "")
            sip_password = current.get(KEY_SIP_PASSWORD, "")

            def _fetch() -> tuple[dict, str | None]:
                """Scarica, legge, ripulisce. Tutto bloccante, quindi executor."""
                fd, path = tempfile.mkstemp(suffix=".db", prefix="vimar_rubrica_")
                os.close(fd)
                try:
                    rest_client.download_db(
                        host, sip_user, sip_password, rest_client.DB_RUBRICA, dest=path
                    )
                    parsed = rubrica_import.parse_rubrica_file(path, gid)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:  # pragma: no cover — file già rimosso
                        pass

                # I nickname sono un di più: se non arrivano, l'import della
                # rubrica resta valido e il PICG rimane quello configurato.
                picg: str | None = None
                try:
                    picg = rest_client.find_picg(
                        rest_client.get_nicknames(host, sip_user, sip_password)
                    )
                except rest_client.RestError:
                    _LOGGER.debug("nickname non disponibili da %s", host)
                return parsed, picg

            try:
                result, picg = await self.hass.async_add_executor_job(_fetch)
            except rest_client.RestAuthError as exc:
                errors["base"] = "rest_auth_failed"
                self._rubrica_error = str(exc)
            except rest_client.RestUnavailable as exc:
                errors["base"] = "rest_unavailable"
                self._rubrica_error = str(exc)
            except (rest_client.RestError, rubrica_import.RubricaImportError,
                    ValueError, OSError) as exc:
                errors["base"] = "rubrica_import_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["base"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Rubrica scaricata, ma nessun attuatore per il GID {gid}."
                    )
                else:
                    self._imported = result
                    self._imported_gid = gid
                    self._picg_from_rest = picg
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="fetch_rubrica",
            data_schema=vol.Schema({
                vol.Optional(
                    "rubrica_gid",
                    default=(user_input or {}).get("rubrica_gid") or default_gid,
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "host": host or "—",
                "rubrica_error": self._rubrica_error or "",
            },
        )

    async def _cloud_token(self) -> tuple[str | None, str | None, str | None]:
        """(token, rubrica_ver, GID) dall'ultima risposta a GET_INIT_STATUS dell'hub.

        Se il token manca si richiede lo stato una volta e si aspetta qualche secondo:
        la risposta arriva come MESSAGE separato. Il token non si salva da nessuna parte.
        """
        hub = (self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}).get("hub")
        if hub is None:
            return None, None, None

        def _read():
            st = hub.stats
            return ((st.get("init_status") or {}).get("token"), st.get("rubrica_ver"),
                    st.get("apt_gid"))

        token, ver, gid = _read()
        if not token and hub.registered:
            await hub.async_request_status()
            for _ in range(10):
                await asyncio.sleep(0.5)
                token, ver, gid = _read()
                if token:
                    break
        return token, ver, (str(gid) if gid not in (None, "") else None)

    async def async_step_fetch_rubrica_cloud(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Scarica la rubrica **dal cloud Vimar** col `token` (issue #5).

        Solo sugli impianti che mandano la risposta lunga di GET_INIT_STATUS (visto su un
        40515 / 2FV2): lì c'è il token, e il file è lo stesso rubrica.db dell'app VIEW.
        Per gli impianti solo cloud senza porta 80 in LAN è l'unica via senza estrarre
        il file a mano.
        """
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        cproxy = (current.get(KEY_CLOUD_PROXY) or "").strip()
        cdomain = (current.get(KEY_CLOUD_DOMAIN) or "").strip()
        if not cdomain and cproxy and str(current.get(KEY_SIP_DOMAIN, "")).endswith("." + cproxy):
            cdomain = current[KEY_SIP_DOMAIN]   # entry manuale: il dominio SIP è quello cloud
        token, ver, plant_gid = await self._cloud_token()
        default_gid = plant_gid or str(current.get(KEY_GID) or "101")

        if not token:
            errors["base"] = "no_cloud_token"
        elif cloud_phonebook.check_inputs(cdomain, cproxy, token, ver):
            errors["base"] = "cloud_failed"
            self._rubrica_error = (
                f"manca {cloud_phonebook.check_inputs(cdomain, cproxy, token, ver)} "
                "(dominio e proxy cloud vengono dal QR)")
        elif user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid

            def _fetch() -> dict:
                data = cloud_phonebook.download(cdomain, cproxy, token, ver)
                fd, path = tempfile.mkstemp(suffix=".db", prefix="vimar_rubrica_")
                os.close(fd)
                try:
                    with open(path, "wb") as fh:
                        fh.write(data)
                    return rubrica_import.parse_rubrica_file(path, gid)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:  # pragma: no cover
                        pass

            try:
                result = await self.hass.async_add_executor_job(_fetch)
            except cloud_phonebook.CloudAuthError as exc:
                errors["base"] = "cloud_auth_failed"
                self._rubrica_error = str(exc)
            except (cloud_phonebook.CloudPhonebookError, rubrica_import.RubricaImportError,
                    ValueError, OSError) as exc:
                errors["base"] = "cloud_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["base"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Rubrica scaricata, ma nessun attuatore per il GID {gid}.")
                else:
                    self._imported = result
                    self._imported_gid = gid
                    self._picg_from_rest = None
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="fetch_rubrica_cloud",
            data_schema=vol.Schema({
                vol.Optional("rubrica_gid",
                             default=(user_input or {}).get("rubrica_gid") or default_gid): str,
            }),
            errors=errors,
            description_placeholders={
                "cproxy": cproxy or "—",
                "rubrica_error": self._rubrica_error or "",
            },
        )

    async def async_step_import_rubrica(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Carica un file rubrica.db ed estrae attuatori + SGA/parametri SYSTEM,
        sostituendo la necessità di girare tools/parse_rubrica.py a mano e
        incollarne l'output nel campo Attuatori (JSON)."""
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        default_gid = str(current.get(KEY_GID) or "101")

        if user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid
            upload_id = user_input.get("rubrica_file")

            def _load() -> dict:
                # process_uploaded_file è un context manager sincrono: la lettura
                # del file (SQLite) è I/O bloccante, quindi tutto il blocco gira
                # nell'executor, mai nel loop asyncio.
                with process_uploaded_file(self.hass, upload_id) as file_path:
                    return rubrica_import.parse_rubrica_file(str(file_path), gid)

            try:
                result = await self.hass.async_add_executor_job(_load)
            except (rubrica_import.RubricaImportError, ValueError, OSError) as exc:
                errors["rubrica_file"] = "rubrica_import_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["rubrica_file"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Nessun attuatore trovato per il GID appartamento {gid}."
                    )
                else:
                    self._imported = result
                    self._imported_gid = gid
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="import_rubrica",
            data_schema=vol.Schema({
                vol.Required("rubrica_file"): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".db")
                ),
                # Come sopra: se l'import fallisce, il GID digitato resta nel
                # campo invece di tornare al valore salvato.
                vol.Optional(
                    "rubrica_gid",
                    default=(user_input or {}).get("rubrica_gid") or default_gid,
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "rubrica_error": self._rubrica_error or "",
            },
        )

    def _confirm_picg(self, current: dict, sga: str | None) -> str:
        """Il picg_target che la conferma salverà — unica fonte per riepilogo e salvataggio.

        In ordine: il PICG dichiarato dal citofono (rubrica scaricata, nickname
        disponibili); altrimenti quello già configurato, che un import da file
        non deve toccare perché il file non dice nulla sul PICG; solo se non ne
        è configurato nessuno, l'SGA della rubrica (sugli impianti visti finora
        coincidono), e infine il default.

        Prima della correzione il riepilogo prometteva «resta quello già
        configurato» mentre il salvataggio lo sovrascriveva con l'SGA: un 60001
        messo a mano per un 2FV2 diventava 55001.
        """
        return (
            self._picg_from_rest
            or current.get(KEY_PICG_TARGET)
            or sga
            or PICG_TARGET
        )

    async def async_step_import_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Riepilogo di ciò che è stato letto da rubrica.db; confermando si
        sostituisce la lista attuatori corrente con quella importata."""
        current = {**self._entry.data, **self._entry.options}
        result = self._imported or {"actuators": [], "sga": None}
        actuators: list[dict] = result["actuators"]
        sga = result.get("sga")
        camera = result.get("camera")
        door = result.get("door")

        if user_input is not None:
            # Due fonti distinte per due valori distinti:
            #   sga_target  ← SYSTEM.MAGIC_APT_INTERCOM della rubrica
            #   picg_target ← il ruolo PICG dichiarato dal citofono nei nickname
            new_sga  = sga or current.get(KEY_SGA_TARGET) or SGA_TARGET
            new_picg = self._confirm_picg(current, sga)
            #   camera_target ← PHONEBOOK.AUTO del proprio GA, o la prima PE
            new_camera = camera or current.get(KEY_CAMERA_TARGET) or ""
            #   door_target ← GID_PE dell'attuatore porta (non l'SGA)
            new_door = door or current.get(KEY_DOOR_TARGET) or ""
            return self.async_create_entry(
                title="",
                data={
                    # Le altre opzioni (messaggio di assenza, foto...)
                    # non vengono dalla rubrica: restano quelle configurate.
                    **self._entry.options,
                    KEY_LOCAL_PROXY:    current.get(KEY_LOCAL_PROXY, ""),
                    KEY_USE_LOCAL_UDP:  current.get(KEY_USE_LOCAL_UDP, True),
                    KEY_LOCAL_UDP_PORT: current.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT),
                    KEY_MEDIA_ENC:      media_enc_mode(current.get(KEY_MEDIA_ENC)),
                    KEY_ACTUATORS:      actuators,
                    KEY_SGA_TARGET:     new_sga,
                    KEY_PICG_TARGET:    new_picg,
                    KEY_CAMERA_TARGET:  new_camera,
                    # La rubrica non dice quale sia il pannello interno:
                    # l'import non lo tocca.
                    KEY_INTERNAL_PANEL_TARGET: current.get(KEY_INTERNAL_PANEL_TARGET, ""),
                    KEY_DOOR_TARGET:    new_door,
                },
            )

        current_sga = current.get(KEY_SGA_TARGET) or SGA_TARGET
        if sga and sga != current_sga:
            sga_info = (
                f"{sga} — diverso da quello attualmente configurato ({current_sga}); "
                "confermando verrà impostato come nuovo sga_target/picg_target."
            )
        elif sga:
            sga_info = f"{sga} — coincide con quello già in uso."
        else:
            sga_info = "non trovato (tabella SYSTEM assente o senza MAGIC_APT_INTERCOM); resta invariato quello già configurato."

        configured_picg = current.get(KEY_PICG_TARGET)
        new_picg = self._confirm_picg(current, sga)
        if self._picg_from_rest and self._picg_from_rest != (configured_picg or PICG_TARGET):
            picg_info = (
                f"{self._picg_from_rest} — dichiarato dal citofono stesso, "
                f"diverso da quello configurato ({configured_picg or PICG_TARGET}); "
                "confermando verrà impostato come nuovo picg_target."
            )
        elif self._picg_from_rest:
            picg_info = f"{self._picg_from_rest} — dichiarato dal citofono, coincide con quello già in uso."
        elif configured_picg:
            picg_info = (
                f"non richiesto (rubrica da file): resta quello già configurato ({configured_picg})."
            )
        else:
            picg_info = (
                f"non richiesto (rubrica da file) e non ancora configurato: verrà impostato "
                f"uguale all'SGA ({new_picg})."
            )

        current_camera = current.get(KEY_CAMERA_TARGET) or CAMERA_TARGET
        if camera and camera != current_camera:
            camera_info = (
                f"{camera} — diversa da quella attualmente configurata ({current_camera}); "
                "confermando verrà impostata come nuova camera_target."
            )
        elif camera:
            camera_info = f"{camera} — coincide con quella già in uso."
        else:
            camera_info = (
                f"nessuna targa (PE) trovata nella rubrica; resta quella già configurata ({current_camera})."
            )

        current_door = current.get(KEY_DOOR_TARGET) or ""
        if door and door != current_door:
            door_info = (
                f"{door} — la targa dell'attuatore porta (GID_PE)"
                + (f", diversa da quella configurata ({current_door})" if current_door else "")
                + "; confermando il comando di apertura andrà lì."
            )
        elif door:
            door_info = f"{door} — coincide con quella già in uso."
        elif current_door:
            door_info = (
                f"nessun attuatore porta nella rubrica; resta quella già configurata ({current_door})."
            )
        else:
            door_info = (
                f"nessun attuatore porta nella rubrica; il comando di apertura resta all'SGA "
                f"({sga or current_sga})."
            )

        return self.async_show_form(
            step_id="import_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(actuators)),
                "names": ", ".join(a["name"] for a in actuators) or "—",
                "gid": self._imported_gid,
                "sga_info": sga_info,
                "picg_info": picg_info,
                "camera_info": camera_info,
                "door_info": door_info,
            },
        )
