"""L'apri-porta va alla targa che possiede il relè, non per forza all'SGA (PR #20).

Sull'impianto di sviluppo (2F, Tab 7S 40507) SGA e targa della serratura sono
lo stesso indirizzo, 55001: mandare OPEN_2F all'SGA funzionava e deve
continuare a funzionare identico. Su un 2FV2 (Tab 5S Up 40515 di @Apeiv) l'SGA
è il 61000, che risponde 200 e non apre: la porta la apre la targa 55001, che è
dove la manda l'app VIEW — il ``GID_PE`` dell'attuatore porta nella rubrica.

Ripiego, uguale alla PR #21: opzione ``door_target`` → attuatore porta già
salvato (entry della 1.0.7) → SGA. I comandi di stato (VOICEMAIL;, DND;,
GET_INIT_STATUS) restano all'SGA/PICG, le chiamate alla targa video.
"""
from __future__ import annotations

import asyncio
import importlib
import re
import sqlite3
from pathlib import Path

import pytest

from custom_components.vimar_intercom import rubrica_import, runtime

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"
BASE = {"sip_user": "12345", "sip_password": "p", "sip_domain": "d", "use_local_udp": True}

# Gli attuatori che l'import produce dalla rubrica reale dell'impianto 2F
# (stesse righe di ACTUATOR_LIST; vedi _make_db_2f qui sotto).
ATTUATORI_2F = [
    {"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"},
    {"name": "TIRO", "msg": "TARGA_ULTIMA_SERRATURA", "target": "55001", "icon": "door"},
    {"name": "F1 ultima targa", "msg": "TARGA_ULTIMA_F1", "target": "55001", "icon": "switch"},
    {"name": "F2 ultima targa", "msg": "TARGA_ULTIMA_F2", "target": "55001", "icon": "switch"},
    {"name": "LUCE SCALA", "msg": "ATTUATORE_01", "target": "55001", "icon": "switch"},
    {"name": "Attuatore 02", "msg": "ATTUATORE_02", "target": "55001", "icon": "switch"},
]


@pytest.fixture(autouse=True)
def _ripristina_runtime():
    yield
    runtime.configure(BASE)


# ─── Impianto 2F (sviluppo): niente cambia ───────────────────────────────────

def test_2f_senza_rubrica_la_porta_resta_all_sga_come_prima():
    runtime.configure(BASE)
    assert runtime.DOOR_TARGET == runtime.SGA_TARGET == "55001"
    assert runtime.DOOR_ESTERNO == "sip:55001@d"


def test_2f_con_rubrica_importata_stesso_indirizzo_di_prima():
    runtime.configure({**BASE, "sga_target": "55001", "picg_target": "55001",
                       "camera_target": "55100", "door_target": "55001",
                       "actuators": ATTUATORI_2F})
    assert runtime.DOOR_ESTERNO == "sip:55001@d"
    assert runtime.SGA_TARGET == "55001"
    assert runtime.PICG_TARGET == "55001"
    assert runtime.INTERCOM == "sip:55100@d", "le chiamate restano alla targa video"


# ─── Impianto 2FV2 (@Apeiv): SGA 61000, PICG 60001, PE 55001 ─────────────────

def test_2fv2_la_porta_va_alla_targa_non_all_sga():
    runtime.configure({**BASE, "sga_target": "61000", "picg_target": "60001",
                       "camera_target": "55001", "door_target": "55001"})
    assert runtime.DOOR_ESTERNO == "sip:55001@d"
    assert runtime.SGA_TARGET == "61000", "VOICEMAIL;/DND; restano all'SGA"
    assert runtime.PICG_TARGET == "60001", "GET_INIT_STATUS resta al PICG"
    assert runtime.INTERCOM == "sip:55001@d"


def test_l_opzione_vince_sull_attuatore_salvato():
    runtime.configure({**BASE, "sga_target": "61000", "door_target": "55009",
                       "actuators": [{"name": "Serratura", "msg": "OPEN",
                                      "target": "55001", "icon": "door"}]})
    assert runtime.DOOR_ESTERNO == "sip:55009@d"


# ─── Entry salvate dalla 1.0.7: attuatori sì, opzione no ─────────────────────

def test_entry_107_con_attuatori_usa_la_targa_della_serratura():
    runtime.configure({**BASE, "sga_target": "61000", "actuators": [
        {"name": "Luce scala", "msg": "ATTUATORE_01", "target": "55002", "icon": "light"},
        {"name": "Serratura", "msg": "OPEN", "target": "55001", "icon": "door"}]})
    assert runtime.DOOR_TARGET == "55001"
    assert runtime.DOOR_ESTERNO == "sip:55001@d"
    assert runtime.SGA_TARGET == "61000"


def test_entry_107_con_serratura_auto_resta_all_sga():
    runtime.configure({**BASE, "sga_target": "61000", "door_target": "",
                       "actuators": [{"name": "Serratura", "msg": "OPEN",
                                      "target": "AUTO", "icon": "door"}]})
    assert runtime.DOOR_ESTERNO == "sip:61000@d"


def test_door_from_actuators():
    assert runtime.door_from_actuators(ATTUATORI_2F) == "55001"
    assert runtime.door_from_actuators([{"target": "55001", "icon": "light"}]) == ""
    assert runtime.door_from_actuators([None, {}]) == ""
    assert runtime.door_from_actuators(None) == ""


# ─── Import rubrica: DB sintetici, nessun file reale nel repo ────────────────

def _make_db_2f(path):
    """Le righe della rubrica dell'impianto di sviluppo, ricostruite.

    Stessi valori di ACTUATOR_LIST/ACTUATOR_RULES/ICON_LIST/SYSTEM/PHONEBOOK
    del file scaricato dal citofono (schema reale, colonne ridotte a quelle
    lette dall'importer): la serratura è sulla targa 55001, che è anche l'SGA.
    """
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE ACTUATOR_LIST (ID INTEGER, NAME TEXT, GID_PE INTEGER, ATT_ID INTEGER,
                                    MSG TEXT, DTMF TEXT, OUTPUT TEXT, TIME INTEGER, ICON INTEGER);
        INSERT INTO ACTUATOR_LIST VALUES
            (1,'Serratura',55001,1,'OPEN','*0000',NULL,500,1),
            (2,'TIRO',55001,1,'TARGA_ULTIMA_SERRATURA','',NULL,500,1),
            (3,'F1 ultima targa',55001,1,'TARGA_ULTIMA_F1','',NULL,500,3),
            (4,'F2 ultima targa',55001,1,'TARGA_ULTIMA_F2','',NULL,500,3),
            (5,'LUCE SCALA',55001,0,'ATTUATORE_01','',NULL,500,3),
            (6,'Attuatore 02',55001,0,'ATTUATORE_02','',NULL,500,3);
        CREATE TABLE ACTUATOR_RULES (ID INTEGER, GA_GID INTEGER, ACTUATOR_ID INTEGER);
        INSERT INTO ACTUATOR_RULES VALUES (1,101,1),(2,101,2),(3,101,3),(4,101,4),(5,101,5),(6,101,6);
        CREATE TABLE ICON_LIST (ID INTEGER, NAME TEXT);
        INSERT INTO ICON_LIST VALUES (1,'DOOR'),(2,'LIGHT'),(3,'SWITCH');
        CREATE TABLE SYSTEM (ID INTEGER, PARAM TEXT, VALUE TEXT);
        INSERT INTO SYSTEM VALUES (1,'SCHEMA_VER','1'),(2,'DB_VER','0'),
            (34,'MAGIC_APT_INTERCOM','55001'),(35,'VM_PREFIX','8000');
        CREATE TABLE PHONEBOOK (ID INTEGER, GID INTEGER, TYPE TEXT, NAME TEXT,
                                AUTO INTEGER, GATE INTEGER, ENABLE INTEGER);
        INSERT INTO PHONEBOOK VALUES (1,101,'GA','Casa',NULL,NULL,0),
            (2,55001,'PICG','Casa CG',NULL,NULL,0),
            (3,55100,'PE','Video',NULL,1,1),
            (4,55200,'P','Centralino',NULL,NULL,1);
    """)
    con.commit()
    con.close()


def _make_db_2fv2(path):
    """Impianto 2FV2 come descritto da @Apeiv: SGA 61000, PICG 60001, PE 55001."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE ACTUATOR_LIST (ID INTEGER, NAME TEXT, GID_PE INTEGER, ATT_ID INTEGER,
                                    MSG TEXT, ICON INTEGER);
        INSERT INTO ACTUATOR_LIST VALUES (1,'Luce scale',55001,0,'ATTUATORE_01',2),
                                         (2,'Serratura',55001,1,'OPEN',1);
        CREATE TABLE ACTUATOR_RULES (ID INTEGER, GA_GID INTEGER, ACTUATOR_ID INTEGER);
        INSERT INTO ACTUATOR_RULES VALUES (1,3,1),(2,3,2);
        CREATE TABLE ICON_LIST (ID INTEGER, NAME TEXT);
        INSERT INTO ICON_LIST VALUES (1,'DOOR'),(2,'LIGHT'),(3,'SWITCH');
        CREATE TABLE SYSTEM (ID INTEGER, PARAM TEXT, VALUE TEXT);
        INSERT INTO SYSTEM VALUES (1,'MAGIC_APT_INTERCOM','61000');
        CREATE TABLE PHONEBOOK (ID INTEGER, GID INTEGER, TYPE TEXT, NAME TEXT, AUTO INTEGER);
        INSERT INTO PHONEBOOK VALUES (1,3,'GA','Casa',NULL),(2,60001,'PICG','Casa CG',NULL),
                                     (3,55001,'PE','Ingresso',NULL);
    """)
    con.commit()
    con.close()


def test_rubrica_2f_porta_uguale_all_sga(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db_2f(db)
    result = rubrica_import.parse_rubrica_file(str(db), "101")
    assert result["actuators"] == ATTUATORI_2F
    assert result["sga"] == "55001"
    assert result["camera"] == "55100"
    assert result["door"] == "55001"


def test_rubrica_2fv2_porta_diversa_dall_sga(tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db_2fv2(db)
    result = rubrica_import.parse_rubrica_file(str(db), "3")
    assert result["sga"] == "61000"
    assert result["door"] == "55001"
    # E una volta salvata, l'entry manda lì il comando di apertura.
    runtime.configure({**BASE, "sga_target": result["sga"], "door_target": result["door"],
                       "actuators": result["actuators"]})
    assert runtime.DOOR_ESTERNO == "sip:55001@d"
    assert runtime.SGA_TARGET == "61000"


def test_rubrica_senza_attuatore_porta(tmp_path):
    db = tmp_path / "rubrica.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE ACTUATOR_LIST (ID INTEGER, NAME TEXT, GID_PE INTEGER, ATT_ID INTEGER,
                                    MSG TEXT, ICON INTEGER);
        INSERT INTO ACTUATOR_LIST VALUES (1,'Luce',55001,0,'ATTUATORE_01',2);
    """)
    con.close()
    assert rubrica_import.parse_rubrica_file(str(db), "101")["door"] is None


# ─── Conferma dell'import: salva door_target e lo mostra ─────────────────────


@pytest.fixture(scope="module")
def of():
    # conftest stubs the HA modules it imports, ConfigFlow included.
    return importlib.import_module("custom_components.vimar_intercom.options_flow")


class _Entry:
    def __init__(self, options: dict):
        self.data = {"sip_user": "12345", "sip_password": "x", "local_proxy": "192.0.2.1", "gid": "3"}
        self.options = options


def _confirm(of, options: dict, imported: dict):
    def mk():
        flow = of.OptionsFlowHandler(_Entry(options))
        flow._imported = imported
        flow._imported_gid = "3"
        flow._picg_from_rest = None
        flow.async_show_form = lambda **kw: {"type": "form", **kw}
        flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
        return flow
    form = asyncio.run(mk().async_step_import_confirm(None))
    saved = asyncio.run(mk().async_step_import_confirm({}))
    return form["description_placeholders"]["door_info"], saved["data"]


def test_conferma_2fv2_salva_la_targa_della_porta(of, tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db_2fv2(db)
    imported = rubrica_import.parse_rubrica_file(str(db), "3")
    info, data = _confirm(of, {"sga_target": "61000", "picg_target": "60001"}, imported)
    assert data["door_target"] == "55001"
    assert data["sga_target"] == "61000"
    assert "55001" in info


def test_conferma_2f_porta_uguale_a_prima(of, tmp_path):
    db = tmp_path / "rubrica.db"
    _make_db_2f(db)
    imported = rubrica_import.parse_rubrica_file(str(db), "101")
    info, data = _confirm(of, {"sga_target": "55001", "door_target": "55001"}, imported)
    assert data["door_target"] == "55001"
    assert "coincide" in info


def test_conferma_senza_porta_lascia_quella_configurata(of):
    imported = {"actuators": [{"name": "Luce", "msg": "X", "target": "55001", "icon": "light"}],
                "sga": "61000", "system": {}, "door": None}
    info, data = _confirm(of, {"door_target": "55009"}, imported)
    assert data["door_target"] == "55009"
    assert "resta" in info


def test_conferma_senza_porta_ne_opzione_dice_sga(of):
    imported = {"actuators": [{"name": "Luce", "msg": "X", "target": "55001", "icon": "light"}],
                "sga": "61000", "system": {}}
    info, data = _confirm(of, {}, imported)
    assert data["door_target"] == ""
    assert "SGA" in info and "61000" in info


# ─── hub.async_door, serratura, servizio open_door, pulsanti AUTO ────────────

def test_async_door_senza_target_usa_la_targa_della_porta(monkeypatch):
    hub_mod = importlib.import_module("custom_components.vimar_intercom.hub")
    runtime.configure({**BASE, "sga_target": "61000", "door_target": "55001"})
    inviati = []

    async def _fake(uri, body, extra_headers=None, timeout=15):
        inviati.append((uri, body, extra_headers))
        return True, "200 OK", 200

    monkeypatch.setattr(hub_mod.sip, "send_message", _fake)
    hub = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(hub, "_touch", lambda: None)

    asyncio.run(hub.async_door())
    assert inviati[-1] == ("sip:55001@d", "OPEN_2F", {"Panda": "command"})
    assert hub.stats["last_door_target"] == "55001"

    # Il comando passato senza targa non viene più ignorato.
    asyncio.run(hub.async_door(command="OPEN"))
    assert inviati[-1][:2] == ("sip:55001@d", "OPEN")


def test_la_serratura_non_fissa_un_indirizzo():
    src = (COMPONENT / "lock.py").read_text(encoding="utf-8")
    assert "door_target=None" in src


def test_open_door_senza_target_non_ripiega_sull_sga():
    """Con default = SGA il servizio mandava sempre al 61000 su un 2FV2."""
    src = (COMPONENT / "services.py").read_text(encoding="utf-8")
    blocco = src.split("OPEN_DOOR_SCHEMA = vol.Schema({", 1)[1].split("})", 1)[0]
    assert 'vol.Optional("target"):' in blocco and "default=_default_sga_target" not in blocco
    yaml = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
    open_door = yaml.split("open_door:", 1)[1].split("\n\n", 1)[0]
    assert 'default: "55001"' not in open_door


def test_open_door_non_assegna_un_ruolo_agli_indirizzi_di_esempio():
    """55001/55002 non hanno un ruolo fisso (ADR-13): su un 2F 55002 è il Tab stesso,
    non la «targa interna». Le opzioni del selettore restano solo esempi."""
    yaml = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
    open_door = yaml.split("open_door:", 1)[1].split("\n\n", 1)[0]
    etichette = re.findall(r"- label: (.+)", open_door)
    assert etichette
    for etichetta in etichette:
        assert not re.search(r"(?i)targa|cancello|portone", etichetta), etichetta


def test_open_door_accetta_open_e_open_2f_ma_non_altri_message():
    """`command: OPEN` (la colonna MSG dell'attuatore porta) e `OPEN_2F` passano;
    un MESSAGE qualsiasi no, il servizio è aperto a ogni utente."""
    src = (COMPONENT / "services.py").read_text(encoding="utf-8")
    blocco = src.split("OPEN_DOOR_SCHEMA = vol.Schema({", 1)[1]
    pattern = re.search(r'"command".*vol\.Match\(r"([^"]+)"\)', blocco).group(1)
    for ok in ("OPEN", "OPEN_2F", "OPEN_3F"):
        assert re.match(pattern, ok), ok
    for no in ("VOICEMAIL;ON", "OPEN_", "open", "OPEN_2F\r\nX: y", "OPEN_" + "A" * 17):
        assert not re.match(pattern, no), no


def test_pulsante_auto_segue_la_targa_della_porta():
    src = (COMPONENT / "button.py").read_text(encoding="utf-8")
    assert "R.DOOR_TARGET if target.upper() == _AUTO_TARGET_SENTINEL" in src


def test_segreteria_e_dnd_restano_all_sga():
    src = (COMPONENT / "switch.py").read_text(encoding="utf-8")
    assert src.count("target=R.SGA_TARGET") == 2  # segreteria e DND
    assert "DOOR_TARGET" not in src
