"""Tests for the call window."""

from bt_handsfree_kde.dbus.telephony import (
    CALL_STATE_ACTIVE,
    CALL_STATE_HELD,
    CALL_STATE_INCOMING,
    Call,
)
from bt_handsfree_kde.ui.call_window import CallWindow
from tests.widgets.conftest import GATEWAY_PATH

CALL_PATH = f"{GATEWAY_PATH}/call0"


def _call(state: str) -> Call:
    """A call in `state` from the fictional number."""
    return Call(CALL_PATH, GATEWAY_PATH, state, "+15550100", "", "", False)


def test_incoming_call_shows_answer_and_reject(qt_application) -> None:
    """A ringing call offers Answer and Reject and no dialpad."""
    window = CallWindow()
    answered: list[str] = []
    window.answer_requested.connect(answered.append)

    window.show_call(_call(CALL_STATE_INCOMING), "Alice", "Incoming call")
    assert window._answer_button.isVisibleTo(window)
    assert window._hangup_button.text() == "Reject"
    assert not window._dialpad_button.isVisibleTo(window)
    window._answer_button.click()
    assert answered == [CALL_PATH]


def test_active_call_offers_hold_and_dtmf(qt_application) -> None:
    """An active call has Hold, Hang up and a dialpad whose keys echo and emit tones."""
    window = CallWindow()
    tones: list[tuple[str, str]] = []
    holds: list[str] = []
    window.tone_requested.connect(lambda call_path, tone: tones.append((call_path, tone)))
    window.hold_requested.connect(holds.append)

    window.show_call(_call(CALL_STATE_ACTIVE), "Alice", "00:05")
    assert window._hold_button.text() == "Hold"
    window._send_tone("7")
    assert tones == [(CALL_PATH, "7")]
    assert window._sent_tones.text() == "7"
    window._hold_button.click()
    assert holds == [CALL_PATH]

    window.show_call(_call(CALL_STATE_HELD), "Alice", "On hold · 00:06")
    assert window._hold_button.text() == "Resume"
    window.hide_call()
    assert window.shown_call_path == ""
