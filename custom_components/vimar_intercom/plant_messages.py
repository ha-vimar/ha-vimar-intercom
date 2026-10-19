"""Messages the plant sends us (SIP MESSAGE, Panda: blue), read into the hub's stats.

Mixed into VimarIntercomHub: the handlers use the hub's stats, events and probe waiters.
"""

import logging

from . import const as C
from . import plant_state as S
from . import rest_client
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


class PlantMessages:
    """Parsing of the SIP MESSAGEs from the plant (status, phonebook, notifications)."""

    # ─── Parsing dei SIP MESSAGE in ingresso (Panda: blue) ───────────────────
    # Qui si LEGGE soltanto: nessun comando in uscita. Parsing difensivo: alcuni
    # body sono JSON, altri delimitati da ';'. Se non combacia → debug, no crash.
    def _handle_incoming_message(self, body: str) -> None:
        st = self.stats
        raw = (body or "").strip()
        upper = raw.upper()

        # Annunci di stato: "VOICEMAIL;ON|OFF" / "DND;ON|OFF" [VERIFICATO]
        if upper.startswith("VOICEMAIL;"):
            st["voicemail"] = ("ON" in upper and "OFF" not in upper)
            st["mode_seq"] = st.get("mode_seq", 0) + 1
            if st["voicemail"]:
                self.on_voicemail_on()
            return
        if upper.startswith("DND;"):
            st["dnd"] = ("ON" in upper and "OFF" not in upper)
            st["mode_seq"] = st.get("mode_seq", 0) + 1
            return

        # C;<call_id>;ANSWERED: the device that answered an incoming call tells the
        # others (PROTOCOL.md, sync between devices; seen on a 40517 in #164)
        parts = raw.split(";")
        if len(parts) >= 3 and parts[0].upper() == "C" and parts[-1].upper() == "ANSWERED":
            self._answered_elsewhere(";".join(parts[1:-1]))
            return

        # SET_APT_PARAMS_REPLY;{"MSGID","ERRCODE"} → risposta a async_set_apt_param
        if upper.startswith("SET_APT_PARAMS_REPLY"):
            self._handle_apt_params_reply(raw)
            return
        # APT_PARAMS_CHANGED;{"PARAM","VALUE"} → un parametro cambiato (da noi, dal
        # Tab o dall'app)
        if upper.startswith("APT_PARAMS_CHANGED"):
            self._handle_apt_params_changed(raw)
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

        # NEW_PHONEBOOK;<ver>;<gid>  [APK, ordine verificato nel sorgente]
        if upper.startswith("NEW_PHONEBOOK"):
            self._handle_new_phonebook(raw)
            return

        # GET_NICKS_REPLY;[{ROLE,EXT,NAME},…]  [VERIFICATO 20/09]
        if upper.startswith("GET_NICKS_REPLY"):
            self._handle_nicks_reply(raw)
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
        self._init_seq += 1
        self._resolve_probe("GET_INIT_STATUS", pairs)

        def _as_bool(v):
            return str(v).strip() in ("1", "true", "True", "ON", "on")

        if "voicemail" in pairs:
            st["voicemail"] = _as_bool(pairs["voicemail"])
            if st["voicemail"]:
                self.on_voicemail_on()
        if "dnd" in pairs:
            st["dnd"] = _as_bool(pairs["dnd"])
        if "voicemail" in pairs or "dnd" in pairs:
            # Una conferma fresca dello stato: gli switch smettono di mostrare il
            # comando in attesa anche se il valore non è cambiato (issue #9).
            st["mode_seq"] = st.get("mode_seq", 0) + 1
        if "vm_level" in pairs:
            st["vm_level"] = pairs["vm_level"]
        if "vm_ver" in pairs:
            st["vm_ver"] = pairs["vm_ver"]
        # Solo nella risposta lunga (40515/2FV2, issue #4): parametri dell'appartamento
        # e cifratura del media. Letti se ci sono, ignorati se mancano.
        self._apply_apt_params(pairs)
        if "GID" in pairs:
            st["apt_gid"] = pairs["GID"]
        if "media_enc" in pairs:
            st["media_enc"] = pairs["media_enc"]
            if S.set_plant_media_enc(pairs["media_enc"], R.MEDIA_ENC_OPTION):
                _LOGGER.info("Cifratura del media dall'impianto: media_enc=%s → SRTP %s",
                             pairs["media_enc"], "attivo" if S.MEDIA_ENC else "spento")
        # token / altri param restano in init_status per usi futuri (phonebook cloud)
        if "rubrica_ver" in pairs:
            self._update_rubrica_ver(pairs["rubrica_ver"])

        _LOGGER.info(
            "GET_INIT_STATUS_REPLY: voicemail=%s dnd=%s vm_level=%s rubrica_ver=%s",
            st.get("voicemail"), st.get("dnd"), st.get("vm_level"), st.get("rubrica_ver"),
        )

    def _apply_apt_params(self, pairs: dict) -> None:
        """vm_timeout, vm_timeout_values, apt_names: da GET_INIT_STATUS_REPLY o da
        APT_PARAMS_CHANGED. Valori non validi scartati, non propagati."""
        st = self.stats
        if "vm_timeout_values" in pairs:
            vals = pairs["vm_timeout_values"]
            if isinstance(vals, list):
                clean = [int(v) for v in vals if isinstance(v, int | str) and str(v).strip().isdigit()]
                st["vm_timeout_values"] = clean or None
        if "vm_timeout" in pairs and str(pairs["vm_timeout"]).strip().isdigit():
            st["vm_timeout"] = int(pairs["vm_timeout"])
        if "apt_names" in pairs and isinstance(pairs["apt_names"], list):
            st["apt_names"] = [str(n) for n in pairs["apt_names"]]

    def _handle_apt_params_changed(self, raw: str) -> None:
        import json
        try:
            j = json.loads(self._split_json_payload(raw, 1) or "")
        except ValueError:
            _LOGGER.debug("APT_PARAMS_CHANGED non parsabile: %r", raw[:120])
            return
        if isinstance(j, dict) and "PARAM" in j:
            self._apply_apt_params({str(j["PARAM"]): j.get("VALUE")})
            _LOGGER.info("Parametro dell'appartamento cambiato: %s=%s", j["PARAM"], j.get("VALUE"))

    def _handle_apt_params_reply(self, raw: str) -> None:
        import json
        try:
            j = json.loads(self._split_json_payload(raw, 1) or "")
        except ValueError:
            j = None
        if not isinstance(j, dict):
            _LOGGER.debug("SET_APT_PARAMS_REPLY non parsabile: %r", raw[:120])
            return
        fut = self._apt_param_waiters.pop(str(j.get("MSGID")), None)
        if fut and not fut.done():
            fut.set_result(j.get("ERRCODE"))

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

    def _handle_nicks_reply(self, raw: str) -> None:
        """GET_NICKS_REPLY: i nickname dell'impianto, con il ruolo di ciascuno.

        È la risposta che la ricerca del PICG aspetta: la voce con ruolo `PICG`
        è l'indirizzo che il citofono dichiara come capogruppo. Arriva dal Tab,
        non necessariamente dall'indirizzo interrogato (sull'impianto di
        riferimento risponde 55002 a una richiesta mandata a 55001).
        """
        nicks = rest_client.parse_nicks_reply(raw)
        if not nicks:
            _LOGGER.debug("GET_NICKS_REPLY senza voci leggibili: %r", raw[:120])
            return
        picg = rest_client.find_picg(nicks)
        self.stats["nicknames"] = nicks
        self.stats["picg_declared"] = picg
        self._nicks_seq += 1
        _LOGGER.info("GET_NICKS_REPLY: %d voci, PICG dichiarato=%s", len(nicks), picg)
        self._resolve_probe("GET_NICKS", nicks)

    def _resolve_probe(self, kind: str, value) -> None:
        waiter = self._probe_waiter
        if self._probe_kind == kind and waiter is not None and not waiter.done():
            waiter.set_result(value)
