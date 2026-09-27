"""A 200 OK means the message was delivered, not that anything acted on it.

Observed live: VOICEMAIL;ON to 55001 is answered 200 OK and the Tab's state
does not change. A wrong recipient behaves exactly like a working one, so the
switch has to check the resulting state rather than trust the acknowledgement.
"""
import asyncio

import pytest

switch_mod = pytest.importorskip("custom_components.vimar_intercom.switch")


class FakeHub:
    def __init__(self, state=None, state_after_request=None):
        self.stats = {"voicemail": state}
        self._state_after_request = state_after_request
        self.commands = []
        self.init_status_requests = 0

    async def async_send_command(self, body, target, header_name=None, header_value=None):
        self.commands.append((body, target))
        return True, "OK (200)"

    async def async_request_init_status(self):
        self.init_status_requests += 1
        if self._state_after_request is not None:
            self.stats["voicemail"] = self._state_after_request
        return True, "OK"

    def register_state_callback(self, cb):
        pass

    def unregister_state_callback(self, cb):
        pass


def make_switch(hub):
    entity = switch_mod.VimarModeSwitch.__new__(switch_mod.VimarModeSwitch)
    entity._hub = hub
    entity._key = "voicemail"
    entity._target = "55001"
    entity._cmd_on, entity._cmd_off = "VOICEMAIL;ON", "VOICEMAIL;OFF"
    entity._state_attr = "voicemail"
    entity._hname, entity._hvalue = "Panda", "blue"
    entity._attr_name = "Segreteria"
    entity._is_on = False
    entity._last_result = None
    entity._verified = None
    entity._verify_task = None
    entity.async_write_ha_state = lambda: None
    return entity


@pytest.fixture(autouse=True)
def fast_verification(monkeypatch):
    monkeypatch.setattr(switch_mod, "VERIFY_ANNOUNCE_WAIT", 0.01)
    monkeypatch.setattr(switch_mod, "VERIFY_REPLY_WAIT", 0.01)


def run(entity, hub, turn_on=True):
    async def main():
        await (entity.async_turn_on() if turn_on else entity.async_turn_off())
        if entity._verify_task:
            await entity._verify_task
    asyncio.run(main())


def test_a_command_the_plant_acts_on_is_marked_verified():
    hub = FakeHub(state=None, state_after_request=True)
    entity = make_switch(hub)
    run(entity, hub)
    assert entity._verified is True
    assert hub.commands == [("VOICEMAIL;ON", "55001")]


def test_an_announced_change_needs_no_explicit_question():
    hub = FakeHub(state=True)  # the Tab announced it before we asked
    entity = make_switch(hub)
    run(entity, hub)
    assert entity._verified is True
    assert hub.init_status_requests == 0, "no need to ask when the state already agrees"


def test_an_ignored_command_is_reported_rather_than_shown_as_success():
    """The failure mode we actually hit: accepted, acknowledged, ignored."""
    hub = FakeHub(state=False, state_after_request=False)
    entity = make_switch(hub)
    run(entity, hub)
    assert entity._verified is False
    assert entity._is_on is False, "the switch must not claim a state the plant denies"
    assert "nessun effetto" in entity._last_result
    assert hub.init_status_requests == 1


def test_the_advice_names_the_recipient_and_the_option_to_change(caplog):
    hub = FakeHub(state=False, state_after_request=False)
    entity = make_switch(hub)
    with caplog.at_level("WARNING"):
        run(entity, hub)
    assert "55001" in caplog.text
    assert "sga_target" in caplog.text


def test_a_plant_that_never_reports_state_is_honest_about_it():
    hub = FakeHub(state=None, state_after_request=None)
    entity = make_switch(hub)
    run(entity, hub)
    assert entity._verified is None
    assert "impossibile verificare" in entity._last_result


def test_turning_off_is_verified_the_same_way():
    hub = FakeHub(state=True, state_after_request=False)
    entity = make_switch(hub)
    run(entity, hub, turn_on=False)
    assert entity._verified is True
    assert hub.commands == [("VOICEMAIL;OFF", "55001")]


def test_the_switch_shows_the_requested_state_at_once():
    """Waiting for confirmation before moving invites a second press — and the
    plant executes every press, which is what made the Tab flap."""
    hub = FakeHub(state=False, state_after_request=True)
    entity = make_switch(hub)

    async def main():
        await entity.async_turn_on()
        immediate = entity.is_on          # before verification has run
        await entity._verify_task
        return immediate, entity.is_on

    immediate, settled = asyncio.run(main())
    assert immediate is True, "the switch must move as soon as the command is sent"
    assert settled is True


def test_a_command_the_plant_refuses_snaps_back():
    hub = FakeHub(state=False, state_after_request=False)
    entity = make_switch(hub)

    async def main():
        await entity.async_turn_on()
        await entity._verify_task
        return entity.is_on

    assert asyncio.run(main()) is False, "optimism must not outlive the verification"


def test_a_failed_send_is_not_shown_as_success():
    hub = FakeHub(state=False)

    async def refuse(body, target, header_name=None, header_value=None):
        return False, "503"

    hub.async_send_command = refuse
    entity = make_switch(hub)
    asyncio.run(entity.async_turn_on())
    assert entity.is_on is False
