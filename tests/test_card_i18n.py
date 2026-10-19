"""The card's string tables (www/card-i18n.js): every language has the same keys, every key
the card asks for exists, and no Italian label is left hardcoded in the card's modules."""
from __future__ import annotations

import re
from pathlib import Path

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom" / "www"
I18N = (WWW / "card-i18n.js").read_text(encoding="utf-8")
MODULES = {p.name: p.read_text(encoding="utf-8") for p in WWW.glob("*.js") if p.name != "card-i18n.js"}


def _tables() -> dict[str, dict[str, str]]:
    body = I18N[I18N.index("const STRINGS = {"):I18N.index("\n};\n")]
    starts = [(m.group(1), m.end()) for m in re.finditer(r"^  (\w+): \{$", body, re.M)]
    tables = {}
    for i, (lang, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        tables[lang] = dict(re.findall(r'^    (\w+): "((?:[^"\\]|\\.)*)",$', body[start:end], re.M))
    return tables


def test_english_and_italian_have_the_same_keys():
    tables = _tables()
    assert set(tables) == {"en", "it"}
    assert len(tables["en"]) > 50
    assert set(tables["en"]) == set(tables["it"])


def test_placeholders_match_across_languages():
    tables = _tables()
    for key, en in tables["en"].items():
        assert set(re.findall(r"\{(\w+)\}", en)) == set(re.findall(r"\{(\w+)\}", tables["it"][key])), key


def test_every_key_the_card_uses_exists():
    keys = set(_tables()["en"])
    used = set()
    for src in MODULES.values():
        for args in re.findall(r"\bt\(([^()]*)", src):  # t("key"), t(cond ? "a" : "b"), t("key", {...})
            pairs = re.findall(r'^\s*"(\w+)"|[?:]\s*"(\w+)"', args)
            used |= {k for pair in pairs for k in pair if k}
    assert len(used) > 40 and used <= keys, sorted(used - keys)
    # Keys built at runtime: state_<state>, outcome_<outcome>, ed_<field>, ed_<field>_<value>.
    card = MODULES["vimar-intercom-card.js"]
    for name, prefix in (("STATES", "state_"), ("OUTCOMES", "outcome_")):
        values = re.findall(r'"(\w+)"', re.search(rf"const {name} = \[(.*?)\];", card).group(1))
        assert values and {prefix + v for v in values} <= keys, name
    editor = MODULES["card-editor.js"]
    fields = re.findall(r'\{ name: "(\w+)", selector', editor)
    assert fields and {f"ed_{f}" for f in fields} <= keys
    for key, values in re.findall(r'opts\("(\w+)", \[([^\]]*)\]\)', editor):
        assert {f"ed_{key}_{v}" for v in re.findall(r'"(\w+)"', values)} <= keys, key


def test_no_italian_label_left_in_the_card_code():
    """Comments may stay Italian; strings the user reads come from the tables."""
    labels = set(_tables()["it"].values()) - set(_tables()["en"].values())  # "Audio", "Tile": same in both
    for name, src in MODULES.items():
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)
        strings = re.findall(r'"((?:[^"\\\n]|\\.)*)"|`([^`]*)`', code)
        found = {s for pair in strings for s in pair if s in labels}
        assert not found, (name, found)
