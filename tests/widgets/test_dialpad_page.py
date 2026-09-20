"""Tests for the Dialpad tab."""

from bt_handsfree_kde.ui.dialpad_page import DialpadPage
from tests.widgets.conftest import GATEWAY_PATH


def test_call_needs_a_phone_and_a_dialable_number(qt_application) -> None:
    """The Call button enables only with both, and emits the normalised dial string."""
    page = DialpadPage()
    dials: list[tuple[str, str]] = []
    page.dial_requested.connect(lambda gateway_path, number: dials.append((gateway_path, number)))

    page.update_phone("", None, "No phone connected")
    page.set_number("+1 (555) 010-0")
    assert not page._call_button.isEnabled()
    assert page._status_label.text() == "No phone connected"

    page.update_phone(GATEWAY_PATH, None, "")
    assert page._call_button.isEnabled()
    page._call_button.click()
    assert dials == [(GATEWAY_PATH, "+15550100")]


def test_keys_insert_and_send_tones_only_during_a_call(qt_application) -> None:
    """A key always types; it is sent as DTMF only while the phone has an active call."""
    page = DialpadPage()
    tones: list[tuple[str, str]] = []
    page.tone_requested.connect(lambda gateway_path, tone: tones.append((gateway_path, tone)))

    page.update_phone(GATEWAY_PATH, None, "")
    page._press_key("1")
    assert page._number_field.text() == "1"
    assert tones == []

    page.update_phone(GATEWAY_PATH, "Alice", "")
    page._press_key("2")
    assert page._number_field.text() == "12"
    assert tones == [(GATEWAY_PATH, "2")]
    assert page._status_label.text() == "Keys send tones to Alice"


def test_field_rejects_letters_and_backspace_deletes(qt_application) -> None:
    """The validator keeps letters out; the backspace button removes the last character."""
    page = DialpadPage()
    page.set_number("555")
    page._number_field.insert("x")
    assert page._number_field.text() == "555"

    page._backspace_button.click()
    assert page._number_field.text() == "55"
