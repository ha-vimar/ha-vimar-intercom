"""The command-line phonebook reader (tools/parse_rubrica.py).

The integration has its own importer (rubrica_import.py, tested elsewhere); this is the
script the docs tell users to run by hand, so its output is what they paste into the options.
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "parse_rubrica", Path(__file__).resolve().parents[1] / "tools" / "parse_rubrica.py")
pr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pr)


def _make_db(path, *, with_rules=True, with_icons=True, apartments=("101",)):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ACTUATOR_LIST (ID INTEGER, NAME TEXT, GID_PE TEXT, ATT_ID TEXT, MSG TEXT, ICON INTEGER)")
    con.execute(
        "INSERT INTO ACTUATOR_LIST VALUES (1,'Serratura','55001','A1','OPEN',1),"
        "(2,'LUCE SCALA','55001','A2','ATTUATORE_01',2),(3,'Senza targa','','A3','AUX1',9)"
    )
    if with_rules:
        con.execute("CREATE TABLE ACTUATOR_RULES (ID INTEGER, GA_GID TEXT, ACTUATOR_ID INTEGER)")
        con.execute("INSERT INTO ACTUATOR_RULES VALUES (1,'101',1),(2,'101',2),(3,'999',1)")
    if with_icons:
        con.execute("CREATE TABLE ICON_LIST (ID INTEGER, NAME TEXT)")
        con.execute("INSERT INTO ICON_LIST VALUES (1,'DOOR'),(2,'LIGHT'),(3,'SWITCH')")
    con.execute("CREATE TABLE SYSTEM (PARAM TEXT, VALUE TEXT)")
    con.execute("INSERT INTO SYSTEM VALUES ('MAGIC_APT_INTERCOM','55001'),('VM_PREFIX','8000'),('OTHER',NULL)")
    con.execute("CREATE TABLE PHONEBOOK (GID TEXT, TYPE TEXT, NAME TEXT)")
    con.executemany("INSERT INTO PHONEBOOK VALUES (?,'GA',?)", [(g, f"Casa {g}") for g in apartments])
    con.execute("INSERT INTO PHONEBOOK VALUES ('55100','PE','Video')")
    con.commit()
    con.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "rubrica.db"
    _make_db(path)
    return path


def _open(path):
    return pr._connect_ro(str(path))


def test_actuators_follow_the_apartment_rules_and_icons(db):
    con = _open(db)
    try:
        assert pr.extract_actuators(con, "101") == [
            {"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"},
            {"name": "LUCE SCALA", "msg": "ATTUATORE_01", "target": "55001", "icon": "light"},
        ]
        assert [a["name"] for a in pr.extract_actuators(con, "999")] == ["Serratura"]
    finally:
        con.close()


def test_without_rules_every_actuator_is_listed_and_a_missing_target_is_auto(tmp_path):
    path = tmp_path / "rubrica.db"
    _make_db(path, with_rules=False, with_icons=False)
    con = _open(path)
    try:
        actuators = pr.extract_actuators(con, "101")
    finally:
        con.close()
    assert [a["name"] for a in actuators] == ["Serratura", "LUCE SCALA", "Senza targa"]
    # Without ICON_LIST the raw icon id is not a known name: every actuator falls back to switch.
    assert {a["icon"] for a in actuators} == {"switch"}
    assert actuators[2]["target"] == "AUTO"


def test_an_apartment_without_rules_falls_back_to_the_whole_list(db):
    con = _open(db)
    try:
        assert len(pr.extract_actuators(con, "555")) == 3
    finally:
        con.close()


def test_system_values_are_strings_or_none(db):
    con = _open(db)
    try:
        assert pr.extract_system(con) == {"MAGIC_APT_INTERCOM": "55001", "VM_PREFIX": "8000", "OTHER": None}
    finally:
        con.close()


def test_missing_tables_give_empty_results(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    con = _open(path)
    try:
        assert pr.extract_actuators(con, "101") == []
        assert pr.extract_system(con) == {}
        assert pr.summarize_phonebook(con) == []
    finally:
        con.close()


def test_the_database_is_opened_read_only(db):
    con = _open(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM SYSTEM")
    finally:
        con.close()


@pytest.mark.parametrize("folder", ["rubrica #2", "copia 100%41"])
def test_uri_characters_in_the_path_are_not_read_as_uri_syntax(tmp_path, folder):
    (tmp_path / folder).mkdir()
    path = tmp_path / folder / "rubrica.db"
    _make_db(path)
    con = _open(path)
    try:
        assert pr.extract_system(con)["MAGIC_APT_INTERCOM"] == "55001"
    finally:
        con.close()


def test_a_missing_file_stops_with_a_message(tmp_path):
    with pytest.raises(SystemExit, match="File non trovato"):
        pr._connect_ro(str(tmp_path / "missing.db"))


@pytest.mark.parametrize(("value", "icon"), [
    (None, "switch"), ("DOOR", "door"), (" light ", "light"), ("Switch", "switch"), ("FAN", "switch"),
])
def test_icon_names_are_normalised(value, icon):
    assert pr._norm_icon(value) == icon


def test_gid_passed_by_hand_wins(db):
    con = _open(db)
    try:
        assert pr.resolve_gid(con, "999") == "999"
    finally:
        con.close()


def test_gid_comes_from_the_only_apartment(db, capsys):
    con = _open(db)
    try:
        assert pr.resolve_gid(con, None) == "101"
    finally:
        con.close()
    assert "uso il GA della rubrica (101" in capsys.readouterr().out


def test_gid_is_not_guessed_between_two_apartments(tmp_path):
    path = tmp_path / "rubrica.db"
    _make_db(path, apartments=("101", "102"))
    con = _open(path)
    try:
        with pytest.raises(SystemExit, match="Più appartamenti") as exc:
            pr.resolve_gid(con, None)
    finally:
        con.close()
    assert "--gid 101" in str(exc.value) and "--gid 102" in str(exc.value)


def test_gid_without_apartments_asks_for_it(tmp_path):
    path = tmp_path / "rubrica.db"
    _make_db(path, apartments=())
    con = _open(path)
    try:
        with pytest.raises(SystemExit, match="nessuna voce di tipo GA"):
            pr.resolve_gid(con, None)
    finally:
        con.close()


def test_json_output_is_only_the_actuator_list(db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["parse_rubrica.py", str(db), "--gid", "101", "--json"])
    pr.main()
    out = capsys.readouterr().out
    assert [a["name"] for a in json.loads(out)] == ["Serratura", "LUCE SCALA"]


def test_summary_names_the_sga_and_ends_with_the_json(db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["parse_rubrica.py", str(db)])
    pr.main()
    out = capsys.readouterr().out
    assert "SGA (MAGIC_APT_INTERCOM) = '55001'" in out
    assert "VM_PREFIX = '8000'" in out
    assert "altri SYSTEM: {'OTHER': None}" in out
    assert "gid=55100 type=PE name='Video'" in out
    tail = out.split("(--json per solo questo) ==\n", 1)[1]
    assert len(json.loads(tail)) == 2


def test_summary_says_when_no_actuator_matches(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(sys, "argv", ["parse_rubrica.py", str(path), "--gid", "101"])
    pr.main()
    assert "(nessuno: verifica il --gid" in capsys.readouterr().out
