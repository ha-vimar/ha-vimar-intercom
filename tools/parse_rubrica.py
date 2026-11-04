#!/usr/bin/env python3
"""parse_rubrica.py — legge il rubrica.db (SQLite) dell'impianto Vimar e ne estrae,
in sola lettura, ciò che serve all'integrazione HA:

  - ATTUATORI visibili al nostro appartamento (GA_GID): nome, comando MSG (body SIP
    con `Panda: command`), destinatario (GID_PE oppure PHONEBOOK.AUTO), icona;
  - SGA reale = SYSTEM.MAGIC_APT_INTERCOM (destinatario dei comandi VOICEMAIL/DND);
  - altri parametri SYSTEM utili (VM_PREFIX, VM_WM_*, MAX_TVCC_TIME);
  - riepilogo PHONEBOOK.

Lo schema è quello ricavato dall'APK (docs/RUBRICA.md §2):
  ACTUATOR_LIST(ID, NAME, GID_PE, ATT_ID, MSG, DTMF, ICON, CMD)
  ACTUATOR_RULES(ID, ACTUATOR_ID, GA_GID)
  ICON_LIST(ID, NAME ∈ DOOR|LIGHT|SWITCH)
  SYSTEM(PARAM, VALUE)
  PHONEBOOK(GID, TYPE, NAME, ...)

Il tool NON scrive mai sul db (apertura read-only). Non stampa credenziali.

Uso:
  python tools/parse_rubrica.py <rubrica.db> [--gid <GID>] [--json]

`--gid` è il GID del tuo appartamento. Se non lo passi, viene ricavato dalla
PHONEBOOK (la voce di tipo GA); se l'impianto ne ha più d'una, il tool li elenca
e si ferma, così scegli tu. `--json` stampa SOLO la lista attuatori pronta da
incollare nelle opzioni dell'integrazione HA.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ICON_MAP = {"DOOR": "door", "LIGHT": "light", "SWITCH": "switch"}


def _force_utf8_stdout() -> None:
    """Evita il crash su console Windows (cp1252).

    Il riepilogo contiene frecce e lettere accentate: su una console con
    codepage 1252 `print` solleva UnicodeEncodeError e il tool muore a metà
    output. Qui lo stream viene riconfigurato in UTF-8, e i caratteri che il
    terminale non sa rappresentare vengono sostituiti invece di far fallire
    tutto.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _connect_ro(path: str) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        sys.exit(f"File non trovato: {path}")
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r["name"] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
    except sqlite3.Error:
        return []


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


def extract_actuators(con: sqlite3.Connection, gid: str) -> list[dict]:
    """Attuatori visibili al nostro GA_GID. Prova la join APK; se fallisce, fallback
    a ACTUATOR_LIST grezza (icona via ICON_LIST se possibile)."""
    tables = {t.lower() for t in _tables(con)}
    actuators: list[dict] = []

    # Percorso principale: join come fa l'app (query 936695)
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
        try:
            rows = con.execute(q, (gid,)).fetchall()
            for r in rows:
                actuators.append(_row_to_actuator(r))
            if actuators:
                return actuators
        except sqlite3.Error as e:
            print(f"# join principale fallita ({e}); fallback ACTUATOR_LIST grezza", file=sys.stderr)

    # Fallback: tutte le righe di ACTUATOR_LIST
    if "actuator_list" in tables:
        for r in con.execute("SELECT * FROM ACTUATOR_LIST").fetchall():
            actuators.append(_row_to_actuator(r))
    return actuators


def _row_to_actuator(r: sqlite3.Row) -> dict:
    name = _get(r, "NAME") or "Attuatore"
    msg = _get(r, "MSG")
    gid_pe = _get(r, "GID_PE")
    icon = _norm_icon(_get(r, "ICON"))
    # Il comando va a GID_PE (destinatario dell'attuatore). Solo se GID_PE manca
    # del tutto si ricade su PHONEBOOK.AUTO. (ATT_ID è l'id del modulo, non il target.)
    target = str(gid_pe) if gid_pe not in (None, "", 0, "0") else "AUTO"
    return {
        "name": str(name),
        "msg": None if msg is None else str(msg),
        "target": target,
        "icon": icon,
    }


def extract_system(con: sqlite3.Connection) -> dict:
    if "system" not in {t.lower() for t in _tables(con)}:
        return {}
    out = {}
    for r in con.execute("SELECT * FROM SYSTEM").fetchall():
        param = _get(r, "PARAM", "KEY")
        value = _get(r, "VALUE", "VAL")
        if param is not None:
            out[str(param)] = None if value is None else str(value)
    return out


def summarize_phonebook(con: sqlite3.Connection) -> list[dict]:
    if "phonebook" not in {t.lower() for t in _tables(con)}:
        return []
    out = []
    for r in con.execute("SELECT * FROM PHONEBOOK").fetchall():
        out.append({
            "gid": _get(r, "GID"),
            "type": _get(r, "TYPE"),
            "name": _get(r, "NAME"),
        })
    return out


def resolve_gid(con: sqlite3.Connection, gid: str | None) -> str:
    """Il GID dell'appartamento: quello passato, o l'unico di tipo GA in rubrica.

    Il GID varia da impianto a impianto, quindi non ha un default sensato. Se la
    PHONEBOOK ha una sola voce di tipo GA (l'appartamento), la si usa; se ne ha
    più d'una le elenca e si ferma, perché indovinare qui significa leggere gli
    attuatori dell'appartamento sbagliato.
    """
    if gid:
        return str(gid)

    ga = [e for e in summarize_phonebook(con) if str(e.get("type") or "").upper() == "GA"]
    if len(ga) == 1:
        resolved = str(ga[0]["gid"])
        print(f"--gid non passato: uso il GA della rubrica ({resolved}, {ga[0]['name']!r})\n")
        return resolved

    if not ga:
        sys.exit(
            "Impossibile ricavare il GID: nessuna voce di tipo GA nella PHONEBOOK.\n"
            "Passalo a mano con --gid <GID>."
        )

    elenco = "\n".join(f"  --gid {e['gid']}   ({e['name']})" for e in ga)
    sys.exit(f"Più appartamenti in rubrica, scegli quale leggere:\n{elenco}")


def main() -> None:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="percorso di rubrica.db")
    ap.add_argument(
        "--gid",
        default=None,
        help="GID del tuo appartamento; se omesso viene ricavato dalla PHONEBOOK",
    )
    ap.add_argument("--json", action="store_true", help="stampa solo la lista attuatori JSON")
    args = ap.parse_args()

    con = _connect_ro(args.db)
    try:
        gid = resolve_gid(con, args.gid)
        actuators = extract_actuators(con, gid)

        if args.json:
            print(json.dumps([a for a in actuators], ensure_ascii=False, indent=2))
            return

        tables = _tables(con)
        print(f"# rubrica.db — tabelle: {', '.join(tables)}\n")

        system = extract_system(con)
        sga = system.get("MAGIC_APT_INTERCOM")
        print("== SYSTEM ==")
        print(f"  SGA (MAGIC_APT_INTERCOM) = {sga!r}   ← destinatario comandi VOICEMAIL/DND")
        for k in ("VM_PREFIX", "VM_WM_PLAY", "VM_WM_REC", "MAX_TVCC_TIME", "VIP"):
            if k in system:
                print(f"  {k} = {system[k]!r}")
        extra = {k: v for k, v in system.items()
                 if k not in {"MAGIC_APT_INTERCOM", "VM_PREFIX", "VM_WM_PLAY", "VM_WM_REC", "MAX_TVCC_TIME", "VIP"}}
        if extra:
            print(f"  (altri SYSTEM: {extra})")

        print(f"\n== ATTUATORI (GA_GID={gid}) — {len(actuators)} trovati ==")
        for a in actuators:
            print(f"  • {a['name']:<24} icon={a['icon']:<7} target={a['target']:<8} MSG={a['msg']!r}")
        if not actuators:
            print("  (nessuno: verifica il --gid o lo schema tabelle sopra)")

        pb = summarize_phonebook(con)
        if pb:
            print(f"\n== PHONEBOOK ({len(pb)} voci) ==")
            for e in pb[:40]:
                print(f"  gid={e['gid']} type={e['type']} name={e['name']!r}")

        print("\n== Attuatori pronti per le opzioni HA (--json per solo questo) ==")
        print(json.dumps(actuators, ensure_ascii=False, indent=2))
    finally:
        con.close()


if __name__ == "__main__":
    main()
