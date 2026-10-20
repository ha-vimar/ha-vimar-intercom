"""A REGISTER the intercom refuses (403/404) shows a repair in Home Assistant.

Seen on a 40507 on 16/10/2026: after another phone was paired, the intercom
answered every REGISTER of Home Assistant with 403. The integration kept
retrying every two minutes and the entities went unavailable, with nothing in
the UI saying why, for four days.
"""
from __future__ import annotations

import types

import pytest
from harness.web import load_views

from custom_components.vimar_intercom import sip_client as sip


@pytest.mark.parametrize("registered, code, refused", [
    (False, 403, 403),
    (False, 404, 404),
    (False, 401, None),   # a challenge, not a refusal
    (False, 503, None),   # transient (Tab busy, network): not the credentials
    (False, None, None),  # nothing answered yet
    (True, 403, None),    # registered again: the old code no longer counts
])
def test_only_a_403_or_404_while_unregistered_is_a_refusal(hub, monkeypatch, registered, code, refused):
    monkeypatch.setattr(sip, "registered", registered)
    monkeypatch.setattr(sip, "last_register_code", code, raising=False)
    assert hub.registration_refused == refused


def test_a_reload_forgets_the_previous_refusal(monkeypatch):
    """The module outlives a reload: after pairing again with a new QR, the old
    403 must not raise the repair before the new hub has even registered."""
    monkeypatch.setattr(sip, "last_register_code", 403, raising=False)
    sip.reset_state()
    assert sip.last_register_code is None


class _Issues:
    def __init__(self):
        self.created: dict[str, dict] = {}
        self.deleted: list[str] = []

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created[issue_id] = {"domain": domain, **kwargs}

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)
        self.created.pop(issue_id, None)


@pytest.fixture
def init(monkeypatch):
    mod = load_views(monkeypatch)
    issues = _Issues()
    monkeypatch.setattr(mod, "ir", types.SimpleNamespace(
        async_create_issue=issues.async_create_issue,
        async_delete_issue=issues.async_delete_issue,
        IssueSeverity=types.SimpleNamespace(ERROR="error")))
    return mod, issues


def _entry():
    return types.SimpleNamespace(entry_id="e1", title="Casa")


def test_a_refused_registration_raises_a_repair_with_the_code(init):
    mod, issues = init
    hub = types.SimpleNamespace(registration_refused=403)
    mod._update_registration_issue(object(), _entry(), hub)
    issue = issues.created["registration_refused_e1"]
    assert issue["translation_key"] == "registration_refused"
    assert issue["translation_placeholders"] == {"code": "403", "title": "Casa"}
    assert issue["is_fixable"] is False
    assert issue["severity"] == "error"


def test_the_repair_goes_away_once_registered_again(init):
    mod, issues = init
    hub = types.SimpleNamespace(registration_refused=403)
    mod._update_registration_issue(object(), _entry(), hub)
    hub.registration_refused = None
    mod._update_registration_issue(object(), _entry(), hub)
    assert issues.created == {}
    assert issues.deleted == ["registration_refused_e1"]
