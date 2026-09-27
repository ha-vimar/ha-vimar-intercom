"""I messaggi che il citofono manda all'hub: stato, chiamate perse, rubrica.

Una classe da cui VimarIntercomHub eredita: qui solo la lettura dei MESSAGE
in arrivo e gli eventi che ne nascono, lontano dalla gestione delle chiamate.
"""

from __future__ import annotations

import logging

from . import const as C
from . import runtime as R

_LOGGER = logging.getLogger(__name__)

# Nomi "umani" degli indirizzi SIP dell'impianto
SIP_ID_NAMES = {
    "55001": "Targa Esterna",
    "55002": "Targa Interna",
    "60001": "Monitor Interno",
}


def sip_id_name(sip_id: str | None) -> str | None:
    if not sip_id:
        return None
    return SIP_ID_NAMES.get(sip_id, sip_id)


class HubMessagesMixin:
    """Lettura dei SIP MESSAGE del citofono (GET_INIT_STATUS_REPLY, ...)."""

    def _handle_incoming_message(self, body: str) -> None:
        st = self.stats
        raw = (body or "").strip()
        upper = raw.upper()

        # Annunci di stato: "VOICEMAIL;ON|OFF" / "DND;ON|OFF" [VERIFICATO]
        if upper.startswith("VOICEMAIL;"):
            st["voicemail"] = ("ON" in upper and "OFF" not in upper)
            return
        if upper.startswith("DND;"):
            st["dnd"] = ("ON" in upper and "OFF" not in upper)
            return

        # GET_INIT_STATUS_REPLY;<json array [{PARAM,VALUE}]>
        if upper.startswith("GET_INIT_STATUS_REPLY"):
            self._parse_init_status_reply(raw)
            return

        # MISSED_CALL;{json}  [da confermare sul campo — PROTOCOL.md §4]
        if upper.startswith("MISSED_CALL"):
            self._handle_missed_call(raw)
            return

        # VM;VIDEO_MESSAGE_CHANGE;NEW[;<n>] | ;UPDATE  [da confermare sul campo]
        if upper.startswith("VM;VIDEO_MESSAGE_CHANGE"):
            self._handle_videomessage(raw)
            return

        # FP;{json}  fuoriporta  [da confermare sul campo]
        if upper.startswith("FP;") or upper.startswith("FP{"):
            self._handle_fuoriporta(raw)
            return

        # CALL_INFO;{json}  [da confermare sul campo]
        if upper.startswith("CALL_INFO"):
            self._handle_call_info(raw)
            return

        # NEW_PHONEBOOK;<gid>;<ver>  [da confermare sul campo]
        if upper.startswith("NEW_PHONEBOOK"):
            self._handle_new_phonebook(raw)
            return

        _LOGGER.debug("MESSAGE in ingresso non mappato: %r", raw[:120])

    @staticmethod
    def _split_json_payload(raw: str, prefix_parts: int):
        """Restituisce (json_str | None) dopo aver saltato `prefix_parts`
        segmenti separati da ';'. Es. raw='MISSED_CALL;{...}' → prefix_parts=1."""
        parts = raw.split(";", prefix_parts)
        if len(parts) <= prefix_parts:
            return None
        return parts[prefix_parts].strip()

    def _parse_init_status_reply(self, raw: str) -> None:
        """Parsa GET_INIT_STATUS_REPLY;[{PARAM,VALUE}] in modo generico e robusto.

        NB: il body dei MESSAGE arriva TRONCATO a 200 char da sip_client.broadcast,
        quindi il JSON può essere incompleto → usiamo un fallback a regex sui
        segmenti {PARAM..VALUE} presenti, così estraiamo tutto ciò che c'è.
        """
        import json
        import re

        payload = self._split_json_payload(raw, 1) or ""
        pairs: dict[str, str] = {}
        try:
            arr = json.loads(payload)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict) and "PARAM" in item:
                        pairs[str(item["PARAM"])] = item.get("VALUE")
        except Exception:
            # JSON incompleto/troncato: estrai le coppie PARAM/VALUE via regex.
            for m in re.finditer(
                r'"PARAM"\s*:\s*"([^"]+)"\s*,\s*"VALUE"\s*:\s*"([^"]*)"', payload
            ):
                pairs[m.group(1)] = m.group(2)
            if not pairs:
                _LOGGER.debug("GET_INIT_STATUS_REPLY non parsabile: %r", payload[:120])

        if not pairs:
            return

        st = self.stats
        st["init_status"] = {**st.get("init_status", {}), **pairs}

        def _as_bool(v):
            return str(v).strip() in ("1", "true", "True", "ON", "on")

        if "voicemail" in pairs:
            st["voicemail"] = _as_bool(pairs["voicemail"])
        if "dnd" in pairs:
            st["dnd"] = _as_bool(pairs["dnd"])
        if "vm_level" in pairs:
            st["vm_level"] = pairs["vm_level"]
        if "vm_ver" in pairs:
            st["vm_ver"] = pairs["vm_ver"]
        # token / altri param restano in init_status per usi futuri (phonebook cloud)
        if "rubrica_ver" in pairs:
            self._update_rubrica_ver(pairs["rubrica_ver"])

        _LOGGER.info(
            "GET_INIT_STATUS_REPLY: voicemail=%s dnd=%s vm_level=%s rubrica_ver=%s",
            st.get("voicemail"), st.get("dnd"), st.get("vm_level"), st.get("rubrica_ver"),
        )

    def _update_rubrica_ver(self, new_ver, gid: str | None = None) -> None:
        """Aggiorna rubrica_ver; se CAMBIA (dopo il primo) emette phonebook_changed."""
        new_ver = None if new_ver is None else str(new_ver)
        old = self.stats.get("rubrica_ver")
        self.stats["rubrica_ver"] = new_ver
        if old is not None and new_ver is not None and new_ver != old:
            _LOGGER.info("Rubrica cambiata: %s → %s", old, new_ver)
            self._fire_event(
                C.EVENT_PHONEBOOK_CHANGED,
                {"gid": gid or R.SIP_USER, "rubrica_ver": new_ver},
            )

    def _handle_missed_call(self, raw: str) -> None:
        import json
        data = {"sip_id": None, "ts": None}
        payload = self._split_json_payload(raw, 1)
        if payload:
            try:
                j = json.loads(payload)
                if isinstance(j, dict):
                    data["sip_id"] = j.get("SIP_ID") or j.get("sip_id")
                    data["ts"] = j.get("TS") or j.get("ts")
            except Exception:
                _LOGGER.debug("MISSED_CALL payload non-JSON: %r", payload[:120])
        data["name"] = sip_id_name(str(data["sip_id"])) if data["sip_id"] is not None else None
        self.stats["last_missed_call"] = data
        self.stats["missed_call_count"] += 1
        _LOGGER.info("Chiamata persa: %s", data)
        self._fire_event(C.EVENT_MISSED_CALL, data)

    def _handle_videomessage(self, raw: str) -> None:
        # VM;VIDEO_MESSAGE_CHANGE;NEW[;<n>] | ;UPDATE
        parts = raw.split(";")
        change = parts[2].strip().upper() if len(parts) > 2 else "NEW"
        extra = parts[3].strip() if len(parts) > 3 else None
        is_new = change == "NEW"
        self.stats["new_videomessage"] = is_new
        self.stats["last_videomessage"] = raw[:120]
        _LOGGER.info("Videomessaggio: change=%s extra=%s", change, extra)
        self._fire_event(
            C.EVENT_VIDEOMESSAGE, {"change": change, "extra": extra, "full": raw[:120]}
        )

    def _handle_fuoriporta(self, raw: str) -> None:
        import json
        data = {"sip_id": None, "msg": None}
        payload = self._split_json_payload(raw, 1)
        if payload:
            try:
                j = json.loads(payload)
                if isinstance(j, dict):
                    data["sip_id"] = j.get("SIP_ID") or j.get("sip_id")
                    data["msg"] = j.get("MSG") or j.get("msg")
            except Exception:
                _LOGGER.debug("FP payload non-JSON: %r", payload[:120])
        self.stats["last_fuoriporta"] = data
        _LOGGER.info("Fuoriporta: %s", data)
        self._fire_event(C.EVENT_FUORIPORTA, data)

    def _handle_call_info(self, raw: str) -> None:
        import json
        data = {"sip_id": None, "reason": None, "media_type": None, "video_src": None}
        payload = self._split_json_payload(raw, 1)
        if payload:
            try:
                j = json.loads(payload)
                if isinstance(j, dict):
                    # `is not None`, non `or`: 0 è un valore significativo in tre
                    # campi su quattro — MEDIA_TYPE 0 = audio, REASON 0 = rifiutata,
                    # VIDEO_SRC 0 = sorgente non commutabile. Con `or` fino alla
                    # 1.0.6 diventavano None (MsgCallInfoReceiver.java dell'app).
                    def _pick(upper: str, lower: str):
                        value = j.get(upper)
                        return j.get(lower) if value is None else value
                    data["sip_id"] = _pick("SIP_ID", "sip_id")
                    data["reason"] = _pick("REASON", "reason")
                    data["media_type"] = _pick("MEDIA_TYPE", "media_type")
                    data["video_src"] = _pick("VIDEO_SRC", "video_src")
            except Exception:
                _LOGGER.debug("CALL_INFO payload non-JSON: %r", payload[:120])
        self.stats["last_call_info"] = data
        _LOGGER.info("Call info: %s", data)
        self._fire_event(C.EVENT_CALL_INFO, data)

    def _handle_new_phonebook(self, raw: str) -> None:
        # NEW_PHONEBOOK;<ver>;<gid> — la versione (MD5 del file, = rubrica_ver)
        # viene PRIMA del gid. Fino alla 1.0.6 li leggevamo al contrario, seguendo
        # la nostra documentazione che diceva «da confermare sul campo»: il sensore
        # Versione Rubrica prendeva il GID, e al GET_INIT_STATUS_REPLY successivo
        # il valore «cambiava» di nuovo, con un secondo phonebook_changed spurio.
        # Ordine verificato nel sorgente dell'app (MsgNewPhonebookReceiver:
        # getOrNull(…, 0) = phonebookVersion, getOrNull(…, 1) = gid).
        parts = raw.split(";")
        ver = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        gid = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        _LOGGER.info("NEW_PHONEBOOK gid=%s ver=%s", gid, ver)
        # Aggiorna rubrica_ver ed emette phonebook_changed (anche se primo valore,
        # NEW_PHONEBOOK è per definizione un cambio → forziamo l'evento).
        if ver is not None:
            old = self.stats.get("rubrica_ver")
            self.stats["rubrica_ver"] = str(ver)
            if old != str(ver):
                self._fire_event(
                    C.EVENT_PHONEBOOK_CHANGED, {"gid": gid or R.SIP_USER, "rubrica_ver": str(ver)}
                )
        else:
            self._fire_event(
                C.EVENT_PHONEBOOK_CHANGED, {"gid": gid or R.SIP_USER, "rubrica_ver": None}
            )
