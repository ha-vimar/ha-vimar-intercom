"""rubrica_import.py — estrazione in sola lettura da rubrica.db per l'options flow.

Stessa logica di ``tools/parse_rubrica.py`` (schema descritto in docs/RUBRICA.md §2),
riproposta qui come modulo puro — nessuna dipendenza da Home Assistant — così può
essere importato sia dal tool a riga di comando (indirettamente, restano comunque
due implementazioni volutamente separate per non rischiare regressioni nel tool già
in uso) sia da ``config_flow.OptionsFlowHandler.async_step_import_rubrica``, ed è
testabile da solo in ``tests/test_rubrica_import.py`` senza bisogno di HA installato
(vedi ``tests/conftest.py`` e la lista PURE in ``tests/test_smoke.py``).

Il database non viene MAI scritto: l'apertura è sempre read-only (``mode=ro``).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .runtime import door_from_actuators

ICON_MAP = {"DOOR": "door", "LIGHT": "light", "SWITCH": "switch"}


class RubricaImportError(Exception):
    """File non trovato, non è un database SQLite valido, o errore di lettura."""


def _connect_ro(path: str) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise RubricaImportError(f"File not found: {path}")
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        con.execute("SELECT 1")  # verifica che sia davvero un database SQLite leggibile
    except sqlite3.Error as exc:
        raise RubricaImportError(f"Not a valid SQLite database: {exc}") from exc
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"].lower() for r in rows}


def _get(row: sqlite3.Row, *names: str):
    """Ritorna il primo campo presente (case-insensitive) tra i nomi dati."""
    keys = {k.lower(): k for k in row.keys()}
    for n in names:
        k = keys.get(n.lower())
        if k is not None:
            return row[k]
    return None


def _norm_icon(val) -> str:
    if val is None:
        return "switch"
    return ICON_MAP.get(str(val).strip().upper(), "switch")


def _row_to_actuator(r: sqlite3.Row) -> dict:
    name = _get(r, "NAME") or "Attuatore"
    msg = _get(r, "MSG")
    gid_pe = _get(r, "GID_PE")
    icon = _norm_icon(_get(r, "ICON"))
    # Il comando va a GID_PE (destinatario dell'attuatore); solo se manca del
    # tutto si ricade su "AUTO" (targa di default dell'apri-porta).
    target = str(gid_pe) if gid_pe not in (None, "", 0, "0") else "AUTO"
    return {
        "name": str(name),
        "msg": None if msg is None else str(msg),
        "target": target,
        "icon": icon,
    }


def extract_actuators(con: sqlite3.Connection, gid: str) -> list[dict]:
    """Attuatori visibili al GID appartamento indicato.

    Percorso principale: join ACTUATOR_LIST/ACTUATOR_RULES(/ICON_LIST) come fa
    l'app VIEW. Fallback: tutte le righe di ACTUATOR_LIST se la join non produce
    risultati o le tabelle di regole non esistono su questo schema.
    """
    tables = _tables(con)
    actuators: list[dict] = []

    if {"actuator_list", "actuator_rules"} <= tables:
        icon_join = ", ICON_LIST" if "icon_list" in tables else ""
        icon_sel = "ICON_LIST.NAME" if "icon_list" in tables else "NULL"
        icon_where = " AND ACTUATOR_LIST.ICON = ICON_LIST.ID" if "icon_list" in tables else ""
        q = (
            f"SELECT ACTUATOR_LIST.ID AS ID, ACTUATOR_LIST.NAME AS NAME, "
            f"ACTUATOR_LIST.GID_PE AS GID_PE, ACTUATOR_LIST.ATT_ID AS ATT_ID, "
            f"ACTUATOR_LIST.MSG AS MSG, {icon_sel} AS ICON "
            f"FROM ACTUATOR_LIST, ACTUATOR_RULES{icon_join} "
            f"WHERE ACTUATOR_RULES.ACTUATOR_ID = ACTUATOR_LIST.ID "
            f"AND ACTUATOR_RULES.GA_GID = ?{icon_where}"
        )
        rows = con.execute(q, (gid,)).fetchall()
        actuators = [_row_to_actuator(r) for r in rows]
        if actuators:
            return actuators

    if "actuator_list" in tables:
        actuators = [_row_to_actuator(r) for r in con.execute("SELECT * FROM ACTUATOR_LIST").fetchall()]
    return actuators


def extract_system(con: sqlite3.Connection) -> dict:
    """Parametri della tabella SYSTEM (PARAM/VALUE), incluso MAGIC_APT_INTERCOM
    (l'SGA reale). Dizionario vuoto se la tabella non esiste su questo schema."""
    if "system" not in _tables(con):
        return {}
    out: dict[str, str | None] = {}
    for r in con.execute("SELECT * FROM SYSTEM").fetchall():
        param = _get(r, "PARAM", "KEY")
        value = _get(r, "VALUE", "VAL")
        if param is not None:
            out[str(param)] = None if value is None else str(value)
    return out


# Ruoli PHONEBOOK.TYPE delle targhe esterne (PROTOCOL §5): PE, PE_EXT.
_PLATE_TYPES = ("PE", "PE_EXT")


def _as_gid(val) -> str | None:
    """GID valido (intero > 0) come stringa, altrimenti None."""
    try:
        n = int(str(val).strip())
    except (TypeError, ValueError):
        return None
    return str(n) if n > 0 else None


def extract_camera_target(con: sqlite3.Connection, gid: str) -> str | None:
    """La targa video da chiamare per accendere la camera (issue #3).

    Stessa regola dell'app: la targa di autoaccensione è
    ``PHONEBOOK.AUTO`` della riga del proprio appartamento (``GID = gid``);
    se è ``NULL`` o ≤ 0 si prende la prima targa (``TYPE`` PE/PE_EXT, in
    ordine di ``ID``). ``ENABLE`` non conta: sugli impianti 2F l'app lo ignora.
    None se la tabella manca o non ci sono targhe.
    """
    if "phonebook" not in _tables(con):
        return None
    for r in con.execute("SELECT * FROM PHONEBOOK").fetchall():
        if str(_get(r, "GID")) == str(gid).strip():
            auto = _as_gid(_get(r, "AUTO"))
            if auto:
                return auto
    rows = con.execute("SELECT * FROM PHONEBOOK ORDER BY ID").fetchall()
    for r in rows:
        tipo = str(_get(r, "TYPE") or "").strip().upper()
        if tipo in _PLATE_TYPES:
            target = _as_gid(_get(r, "GID"))
            if target:
                return target
    return None


def parse_rubrica_file(path: str, gid: str = "101") -> dict:
    """Apre rubrica.db in sola lettura ed estrae attuatori + parametri SYSTEM.

    Ritorna ``{"actuators": [...], "system": {...}, "sga": str | None,
    "camera": str | None, "door": str | None}``. ``door`` è il ``GID_PE``
    dell'attuatore porta: la targa a cui va il comando di apertura, che non è
    per forza l'SGA (su un 2FV2 l'SGA è il 61000, la porta la apre la 55001).
    Solleva ``RubricaImportError`` se il file non esiste, non è un database
    SQLite valido, o una query fallisce inaspettatamente. Tabelle mancanti
    (schema diverso da questo impianto) non sono un errore: producono
    semplicemente liste/dizionari vuoti, così l'importer resta utilizzabile
    anche su impianti con uno schema rubrica.db leggermente diverso.
    """
    con = _connect_ro(path)
    try:
        try:
            actuators = extract_actuators(con, gid)
            system = extract_system(con)
            camera = extract_camera_target(con, gid)
        except sqlite3.Error as exc:
            raise RubricaImportError(f"Database read error: {exc}") from exc
        return {
            "actuators": actuators,
            "system": system,
            "sga": system.get("MAGIC_APT_INTERCOM"),
            "camera": camera,
            "door": door_from_actuators(actuators) or None,
        }
    finally:
        con.close()
