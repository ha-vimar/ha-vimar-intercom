"""The relay delivers every request twice, milliseconds apart.

Observed live: each INVITE and each MESSAGE arrives in duplicate with the same
Call-ID and CSeq but different Via branches. Acting on both rings the doorbell
twice, double-counts events, and — worst — lets the second copy be declined
while the first is already being answered.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")


def headers(call_id="abc", cseq="20 INVITE"):
    return {"call-id": call_id, "cseq": cseq, "from": "<sip:55001@x>", "to": "<sip:60999@x>"}


@pytest.fixture(autouse=True)
def clean():
    sip._seen_requests.clear()
    yield
    sip._seen_requests.clear()


def test_the_first_delivery_is_acted_on():
    assert sip._is_duplicate("INVITE", headers()) is False


def test_the_second_copy_is_recognised():
    sip._is_duplicate("INVITE", headers())
    assert sip._is_duplicate("INVITE", headers()) is True


def test_a_genuinely_new_call_is_not_mistaken_for_a_copy():
    sip._is_duplicate("INVITE", headers(call_id="first"))
    assert sip._is_duplicate("INVITE", headers(call_id="second")) is False


def test_a_second_ring_of_the_same_dialog_is_new_when_the_cseq_advances():
    """A re-INVITE shares the Call-ID but carries a higher CSeq."""
    sip._is_duplicate("INVITE", headers(cseq="20 INVITE"))
    assert sip._is_duplicate("INVITE", headers(cseq="21 INVITE")) is False


def test_different_methods_on_one_dialog_do_not_shadow_each_other():
    sip._is_duplicate("INVITE", headers(cseq="20 INVITE"))
    assert sip._is_duplicate("CANCEL", headers(cseq="20 CANCEL")) is False


def test_a_request_without_a_call_id_is_never_treated_as_a_copy():
    assert sip._is_duplicate("MESSAGE", {"cseq": "1 MESSAGE"}) is False


def test_old_entries_do_not_accumulate(monkeypatch):
    """The window is relative to now, so the fake clock must move forward
    from the real one rather than to an arbitrary point."""
    sip._is_duplicate("MESSAGE", headers(call_id="old", cseq="1 MESSAGE"))
    later = sip.time.monotonic() + sip.DUPLICATE_WINDOW + 10
    monkeypatch.setattr(sip.time, "monotonic", lambda: later)
    sip._is_duplicate("MESSAGE", headers(call_id="new", cseq="1 MESSAGE"))
    assert all("old" not in str(key) for key in sip._seen_requests)
    assert any("new" in str(key) for key in sip._seen_requests)


def test_a_copy_arriving_within_the_window_is_still_caught(monkeypatch):
    sip._is_duplicate("INVITE", headers())
    later = sip.time.monotonic() + sip.DUPLICATE_WINDOW / 2
    monkeypatch.setattr(sip.time, "monotonic", lambda: later)
    assert sip._is_duplicate("INVITE", headers()) is True
