"""Camera platform for Vimar Intercom."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import frame_grabber, ring_log
from . import runtime as R
from .const import DOMAIN
from .device import device_info, legacy_entity_id

_LOGGER = logging.getLogger(__name__)

# Entità che la card del citofono legge, per chiave della sua config → (dominio,
# suffisso dell'unique_id). Gli entity_id cambiano da un'installazione all'altra
# (area del dispositivo, rinomine): la card li prende da qui invece di indovinarli.
CARD_ENTITIES = {
    "status": ("sensor", "status"),
    "last_ring": ("sensor", "last_ring"),
    "lock": ("lock", "lock"),
    # Impostazioni del dialogo della card (Configurazione).
    "dnd": ("switch", "dnd"),
    "segreteria": ("switch", "segreteria"),
    "delay": ("select", "vm_timeout"),
    "file": ("select", "away_file"),
    "text": ("text", "away_text"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomCamera(hub, entry.entry_id, hass)])


class VimarIntercomCamera(Camera):
    """Intercom camera — streams video from SIP/RTP pipeline.

    Il frontend usa lo stream di HA (HLS/WebRTC) su `stream_source()`, cioè
    `/api/vimar_intercom/av` (MPEG-TS H.264 + PCMU). Aprire lo stream fa
    partire l'autoaccensione verso la targa video (`camera_target`).

    Fino alla 1.0.7 la camera dichiarava MJPEG senza `CameraEntityFeature.STREAM`:
    HA non apriva mai `stream_source()` e l'MJPEG leggeva `hub.video_frame`,
    che è sempre None → camera sempre nera (issue #8).
    """

    _attr_has_entity_name = True
    _attr_name = None  # the device's main entity: named after the device
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hub, entry_id: str, hass: HomeAssistant) -> None:
        super().__init__()
        self._hub = hub
        self._hass = hass
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_camera"
        self.entity_id = legacy_entity_id("camera", "intercom")
        self._attr_device_info = device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.register_video_end_callback(self._stop_stream))

    @callback
    def _stop_stream(self) -> None:
        """Finiti chiamata o squillo si ferma lo stream di HA. Resterebbe a riprovare
        su /av (503 a riposo, niente chiamate da solo) con attese di 10, 20, 30 s, e al
        "Vedi esterno" dopo la card restava bianca; fermo, il prossimo riquadro video
        ne apre uno nuovo che parte subito."""
        # Non durante una registrazione (camera.record): fermarlo la butterebbe via.
        if self.stream and "recorder" not in self.stream.outputs():
            self.hass.async_create_task(self.stream.stop())

    @property
    def available(self) -> bool:
        """Registrato = disponibile. HA segnerebbe la camera "unavailable" per i 503
        voluti di /av a riposo, e il frontend non aprirebbe più il video."""
        return self._hub.registered

    @property
    def extra_state_attributes(self) -> dict:
        """`card_entities`: gli entity_id veri delle entità che la card legge (stato, serratura, impostazioni),
        per la card (`custom:vimar-intercom-card`) quando la sua config non li scrive."""
        reg = er.async_get(self._hass)
        found = {
            key: reg.async_get_entity_id(domain, DOMAIN, f"{self._entry_id}_{suffix}")
            for key, (domain, suffix) in CARD_ENTITIES.items()
        }
        return {"card_entities": {k: v for k, v in found.items() if v}}

    @property
    def is_streaming(self) -> bool:
        """True when there's an active SIP call with video."""
        return self._hub.video_active

    async def stream_source(self) -> str | None:
        """AV stream URL (MPEG-TS with H264 video + PCMU audio) for HA's stream worker."""
        # Loopback, on Home Assistant's real port and scheme (8123 and http do
        # not hold everywhere). Not internal_url: it may point at a reverse
        # proxy (NGINX add-on), whose X-Forwarded-For makes /av refuse the
        # request (403, _is_local_request).
        http = getattr(self._hass, "http", None)
        scheme = "https" if getattr(http, "ssl_certificate", None) else "http"
        port = getattr(http, "server_port", None) or 8123
        # The key (#63) does not expire: the stream worker reuses this URL on every
        # reconnect, where a signed path (authSig) would be refused after its lifetime.
        return (f"{scheme}://127.0.0.1:{port}/api/vimar_intercom/av"
                f"?{R.AV_KEY_PARAM}={R.AV_KEY}")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """L'ultimo fotogramma della chiamata o dell'anteprima dello squillo.

        Fuori da lì niente video: aprirlo per una miniatura farebbe chiamare la
        targa. Istantaneo: lo tiene aggiornato il frame grabber di media_handler.

        Between calls, the last ring photo when there is one (read in the
        executor, never a call): HA turns None into a 500/502, a broken tile on
        dashboards without the custom card. With no photo, None as before.
        """
        if not self._hub.video_active:
            path = self._hub.stats.get("last_photo_path")
            if not (path or R.SNAPSHOT_DIR):
                return None  # photos are off: nothing to read
            photo = await self._hass.async_add_executor_job(
                ring_log.read_last_photo, path, R.SNAPSHOT_DIR)
            if photo:
                return photo
            return None
        return await frame_grabber.wait_frame(after=1)  # non il primo IDR, scuro
