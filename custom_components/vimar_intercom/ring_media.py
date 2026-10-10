"""Ring photo, clip and ring log, written to the snapshot folder.

Mixed into VimarIntercomHub: these use the hub's stats and its ring log lock.
"""

import asyncio
import logging
import os
from collections.abc import Callable

from . import frame_grabber, ring_log
from . import runtime as R
from . import sip_client as sip

_LOGGER = logging.getLogger(__name__)


class RingMedia:
    """The photo and clip of a ring, and its line in the ring log."""

    async def _ring_log(self, change: Callable[[list], None]) -> None:
        try:
            async with self._ring_log_lock:  # FIFO: stesso ordine delle richieste
                await asyncio.get_running_loop().run_in_executor(
                    None, ring_log.update_ring_log, R.SNAPSHOT_DIR, change)
        except OSError as e:
            _LOGGER.warning("Registro squilli non aggiornato in %s: %s", R.SNAPSHOT_DIR, e)

    def _log_outcome(self, outcome: str) -> None:
        """Esito dell'ultimo squillo nel registro: answered (Rispondi), declined (Rifiuta),
        away (messaggio), answered_elsewhere (altro dispositivo) o opened (porta aperta
        da HA durante lo squillo)."""
        key = getattr(self, "_ring_time", None)
        if not (R.SNAPSHOT_DIR and key):
            return

        def change(rings):
            for r in rings:
                if r.get("time") == key:
                    r["outcome"] = outcome

        self._spawn(self._ring_log(change), "ring log")

    async def _save_ring_photo(self, name: str) -> None:
        """Foto di chi ha suonato (anteprima dello squillo) nella cartella delle opzioni,
        col nome (ora dello squillo) già scritto nel registro: il primo fotogramma appena
        decodificato (~1 s dallo squillo: notifiche e card la vedono subito), poi, se
        SNAPSHOT_DELAY > 0, dopo quei secondi quello con l'esposizione regolata sullo
        stesso file (il primo IDR della targa è scuro: la telecamera si è appena accesa).
        Aspetta quanto dura lo squillo (non i soliti 6 s): un primo IDR senza SPS/PPS in
        banda (né in cache) va scartato, e il prossimo buono può arrivare più tardi (targa
        40515: SPS ogni ~6 s) — sempre entro lo squillo, perché wait_frame esce comunque
        appena il grabber muore (fine squillo/chiamata, frame_grabber.stop)."""
        jpeg = await frame_grabber.wait_frame(timeout=sip.RING_MAX_S)
        if not jpeg:
            _LOGGER.warning("Foto squillo: nessuna immagine (anteprima video non arrivata)")
            return
        if not await self._write_photo(name, jpeg) or not R.SNAPSHOT_DELAY:
            return
        await asyncio.sleep(R.SNAPSHOT_DELAY)
        better = await frame_grabber.wait_frame(after=1)
        if better and better != jpeg:
            await self._write_photo(name, better)

    async def _write_photo(self, name: str, jpeg: bytes) -> bool:
        try:
            v = await asyncio.get_running_loop().run_in_executor(
                None, ring_log.write_photo, R.SNAPSHOT_DIR, name, jpeg)
        except OSError as e:
            _LOGGER.warning("Foto squillo non salvata in %s: %s", R.SNAPSHOT_DIR, e)
            return False
        self.stats.update(last_photo=name, last_photo_path=os.path.join(R.SNAPSHOT_DIR, name),
                          last_photo_v=v)
        self._touch()
        return True

    def _clip_done(self, path: str | None) -> None:
        """Clip dello squillo chiuso (frame_grabber.record); None se non c'è stato video."""
        if path:
            self.stats.update(last_clip=os.path.basename(path), last_clip_path=path)
            self._touch()
