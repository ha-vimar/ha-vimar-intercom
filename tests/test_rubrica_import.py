"""Test per rubrica_import.py: importer di rubrica.db per l'options flow.

Costruisce un rubrica.db sintetico (stesso schema minimo usato da
tools/parse_rubrica.py, vedi docs/RUBRICA.md §2) e verifica estrazione
attuatori/SYSTEM, fallback quando mancano ACTUATOR_RULES/ICON_LIST, e gli
errori su file mancante o non-SQLite.
"""
from __future__ import annotations

import sqlite3

import pytest

from custom_components.vimar_intercom import rubrica_import


def _make_db(path, *, with_rules=True, with_icons=True, with_system=True):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ACTUATOR_LIST (ID INTEGER, NAME TEXT, GID_PE TEXT, ATT_ID TEXT, MSG TEXT, ICON INTEGER)")
    con.execute(
        "INSERT INTO ACTUATOR_LIST VALUES (1,'Serratura','55001','A1','OPEN',1),"
        "(2,'LUCE SCALA','55001','A2','ATTUATORE_01',3)"
    )
    if with_rules:
        con.execute("CREATE TABLE ACTUATOR_RULES (ID INTEGER, GA_GID TEXT, ACTUATOR_ID INTEGER)")
        con.execute("INSERT INTO ACTUATOR_RULES VALUES (1,'101',1),(2,'101',2),(3,'999',1)")
    if with_icons:
        con.execute("CREATE TABLE ICON_LIST (ID INTEGER, NAME TEXT)")
        con.execute("INSERT INTO ICON_LIST VALUES (1,'DOOR'),(2,'LIGHT'),(3,'SWITCH')")
    if with_system:
        con.execute("CREATE TABLE SYSTEM (PARAM TEXT, VALUE TEXT)")
        con.execute("INSERT INTO SYSTEM VALUES ('MAGIC_APT_INTERCOM','55001'),('VM_PREFIX','8000')")
    con.commit()
    con.close()


def test_parse_rubrica_file_full_schema(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db(db)

    result = rubrica_import.parse_rubrica_file(str(db), gid="101")

    assert result["sga"] == "55001"
    assert result["system"]["VM_PREFIX"] == "8000"
    names = {a["name"] for a in result["actuators"]}
    assert names == {"Serratura", "LUCE SCALA"}
    serratura = next(a for a in result["actuators"] if a["name"] == "Serratura")
    assert serratura == {"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"}
    luce = next(a for a in result["actuators"] if a["name"] == "LUCE SCALA")
    assert luce["icon"] == "switch"


def test_parse_rubrica_file_filters_by_gid(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db(db)

    result = rubrica_import.parse_rubrica_file(str(db), gid="999")

    assert [a["name"] for a in result["actuators"]] == ["Serratura"]


def test_parse_rubrica_file_fallback_without_rules_table(tmp_path):
    """Senza ACTUATOR_RULES/ICON_LIST il fallback ritorna comunque tutte le
    righe di ACTUATOR_LIST (icona default 'switch')."""
    db = tmp_path / "rubrica.db"
    _make_db(db, with_rules=False, with_icons=False)

    result = rubrica_import.parse_rubrica_file(str(db), gid="101")

    assert len(result["actuators"]) == 2
    assert all(a["icon"] == "switch" for a in result["actuators"])


def test_parse_rubrica_file_no_system_table(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db(db, with_system=False)

    result = rubrica_import.parse_rubrica_file(str(db), gid="101")

    assert result["system"] == {}
    assert result["sga"] is None


def test_missing_file_raises_rubrica_import_error(tmp_path):
    with pytest.raises(rubrica_import.RubricaImportError):
        rubrica_import.parse_rubrica_file(str(tmp_path / "non_esiste.db"))


def test_non_sqlite_file_raises_rubrica_import_error(tmp_path):
    bogus = tmp_path / "non_e_un_db.db"
    bogus.write_text("questo non è un database SQLite")
    with pytest.raises(rubrica_import.RubricaImportError):
        rubrica_import.parse_rubrica_file(str(bogus))


def test_a_file_that_is_not_sqlite_leaves_no_connection_open(tmp_path, monkeypatch):
    # The connection opened before the check used to be left to the garbage
    # collector (ResourceWarning), and on Windows it keeps the file locked.
    opened = []
    connect = sqlite3.connect

    def _connect(*a, **k):
        opened.append(connect(*a, **k))
        return opened[-1]

    monkeypatch.setattr(sqlite3, "connect", _connect)
    bogus = tmp_path / "non_e_un_db.db"
    bogus.write_text("questo non è un database SQLite")
    with pytest.raises(rubrica_import.RubricaImportError):
        rubrica_import.parse_rubrica_file(str(bogus))
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_never_writes_to_the_database(tmp_path):
    """L'apertura è read-only: un tentativo di scrittura sulla stessa
    connessione usata dal modulo deve fallire."""
    db = tmp_path / "rubrica.db"
    _make_db(db)

    con = rubrica_import._connect_ro(str(db))
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM ACTUATOR_LIST")
    finally:
        con.close()


# ─── Targa video (camera_target) dalla PHONEBOOK — issue #3 ──────────────────

def _make_phonebook(path, rows):
    """rows: (ID, GID, TYPE, AUTO). Schema ridotto della PHONEBOOK reale."""
    _make_db(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE PHONEBOOK (ID INTEGER, GID INTEGER, TYPE TEXT, NAME TEXT, "
                "AUTO INTEGER, ENABLE INTEGER)")
    con.executemany("INSERT INTO PHONEBOOK VALUES (?,?,?,'x',?,0)", rows)
    con.commit()
    con.close()


def test_camera_target_prima_pe_se_auto_manca(tmp_path):
    """Impianto di sviluppo: GA 101 con AUTO NULL, PICG 55001, PE 55100."""
    db = tmp_path / "rubrica.db"
    _make_phonebook(db, [(1, 101, "GA", None), (2, 55001, "PICG", None),
                         (3, 55100, "PE", None), (4, 55200, "P", None)])
    assert rubrica_import.parse_rubrica_file(str(db), gid="101")["camera"] == "55100"


def test_camera_target_40515_la_pe_e_55001(tmp_path):
    """Il 40515 di #3: la PE è 55001, l'SGA 61000, il GA 3."""
    db = tmp_path / "rubrica.db"
    _make_phonebook(db, [(1, 3, "GA", None), (2, 55001, "PE", None), (3, 45001, "RELE", None)])
    assert rubrica_import.parse_rubrica_file(str(db), gid="3")["camera"] == "55001"


def test_camera_target_auto_del_proprio_appartamento_vince(tmp_path):
    """Regola dell'app: PHONEBOOK.AUTO della riga GID=<mio gid>, se > 0."""
    db = tmp_path / "rubrica.db"
    _make_phonebook(db, [(1, 101, "GA", 55002), (2, 55001, "PE", None), (3, 55002, "PE_EXT", None),
                         (4, 102, "GA", 55001)])
    assert rubrica_import.parse_rubrica_file(str(db), gid="101")["camera"] == "55002"


def test_camera_target_auto_zero_o_negativo_non_conta(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_phonebook(db, [(1, 101, "GA", 0), (2, 55007, "PE_EXT", None)])
    assert rubrica_import.parse_rubrica_file(str(db), gid="101")["camera"] == "55007"


def test_camera_target_assente(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db(db)  # nessuna PHONEBOOK
    assert rubrica_import.parse_rubrica_file(str(db), gid="101")["camera"] is None
    db2 = tmp_path / "r2.db"
    _make_phonebook(db2, [(1, 101, "GA", None), (2, 55200, "P", None)])
    assert rubrica_import.parse_rubrica_file(str(db2), gid="101")["camera"] is None


def test_a_path_that_sqlite_cannot_open_is_an_import_error(tmp_path):
    # A directory exists but is no database: the connect itself fails.
    with pytest.raises(rubrica_import.RubricaImportError, match="SQLite"):
        rubrica_import.parse_rubrica_file(str(tmp_path))


def test_rules_that_match_no_actuator_fall_back_to_the_whole_list(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db(db)
    result = rubrica_import.parse_rubrica_file(str(db), gid="555")  # no rule for this flat
    assert {a["name"] for a in result["actuators"]} == {"Serratura", "LUCE SCALA"}


def test_an_actuator_row_without_optional_columns_gets_defaults(tmp_path):
    db = tmp_path / "rubrica.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ACTUATOR_LIST (ID INTEGER, NAME TEXT, MSG TEXT)")
    con.execute("INSERT INTO ACTUATOR_LIST VALUES (1, NULL, NULL)")
    con.commit()
    con.close()
    result = rubrica_import.parse_rubrica_file(str(db))
    assert result["actuators"] == [{"name": "Attuatore", "msg": None, "target": "AUTO", "icon": "switch"}]


def test_system_rows_without_a_parameter_name_are_skipped(tmp_path):
    db = tmp_path / "rubrica.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE SYSTEM (KEY TEXT, VAL TEXT)")
    con.execute("INSERT INTO SYSTEM VALUES (NULL, 'x'), ('MAGIC_APT_INTERCOM', NULL), ('A', '1')")
    con.commit()
    con.close()
    assert rubrica_import.parse_rubrica_file(str(db))["system"] == {"MAGIC_APT_INTERCOM": None, "A": "1"}


def test_the_camera_skips_plates_without_a_valid_address(tmp_path):
    db = tmp_path / "rubrica.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE PHONEBOOK (ID INTEGER, GID TEXT, TYPE TEXT, AUTO TEXT)")
    con.execute("INSERT INTO PHONEBOOK VALUES (1,'101','GA','0'),(2,'abc','PE',NULL),"
                "(3,'55003','pe_ext',NULL)")
    con.commit()
    con.close()
    assert rubrica_import.parse_rubrica_file(str(db), gid="101")["camera"] == "55003"


def test_a_phonebook_without_plates_gives_no_camera(tmp_path):
    db = tmp_path / "rubrica.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE PHONEBOOK (ID INTEGER, GID TEXT, TYPE TEXT, AUTO TEXT)")
    con.execute("INSERT INTO PHONEBOOK VALUES (1,'101','GA',NULL)")
    con.commit()
    con.close()
    assert rubrica_import.parse_rubrica_file(str(db), gid="101")["camera"] is None
