"""Tests for the application wiring: disconnects and service restarts seen end to end.

`HandsfreeApplication` is started against every fake at once; the tests then remove
gateways and stop and start the fake services the way the real ones behave, and check
what the contacts, messages and notification state look like afterwards.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from PySide6.QtWidgets import QApplication

import bt_handsfree_kde.app as app_module
from bt_handsfree_kde.app import HandsfreeApplication
from bt_handsfree_kde.contacts.client import SYNC_STATE_SYNCED as CONTACTS_SYNCED
from bt_handsfree_kde.dbus.telephony import CALL_STATE_INCOMING
from bt_handsfree_kde.messages.client import CONNECTION_LOST_TEXT
from bt_handsfree_kde.messages.client import SYNC_STATE_FAILED as MESSAGES_FAILED
from bt_handsfree_kde.messages.client import SYNC_STATE_SYNCED as MESSAGES_SYNCED
from tests.conftest import (
    PHONE_ADDRESS,
    PHONE_ALIAS,
    SECOND_PHONE_ADDRESS,
    SECOND_PHONE_ALIAS,
    wait_until,
)
from tests.fakes.bluez_service import FakeBlueZService
from tests.fakes.notification_service import FakeNotificationService
from tests.fakes.obex_service import FakeObexService
from tests.fakes.telephony_service import FakeTelephonyService
from tests.fakes.watcher_service import FakeWatcherService
from tests.integration.test_messages_client import INBOX_MESSAGE, PUSHED_MESSAGE
from tests.unit.test_vcard import VCARD_30_TEXT

# Sync delays used instead of the production ones, so a connect is followed by the
# phonebook pull and the message listing within the test's waiting time.
TEST_SYNC_DELAY_S = 0.05


@pytest_asyncio.fixture
async def application(
    qt_application: QApplication,
    private_buses: str,
    telephony: FakeTelephonyService,
    bluez: FakeBlueZService,
    obex: FakeObexService,
    notifications: FakeNotificationService,
    watcher: FakeWatcherService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> AsyncIterator[HandsfreeApplication]:
    """A started application with one connected phone that has contacts and messages."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(app_module, "CONTACTS_SYNC_DELAY_S", TEST_SYNC_DELAY_S)
    monkeypatch.setattr(app_module, "MESSAGES_SYNC_DELAY_S", TEST_SYNC_DELAY_S)
    bluez.add_device(PHONE_ADDRESS, PHONE_ALIAS, battery_percentage=80)
    obex.phonebook_vcard = VCARD_30_TEXT
    obex.messages_by_folder = {"inbox": [INBOX_MESSAGE], "sent": []}
    telephony.add_gateway(PHONE_ADDRESS)

    started_application = HandsfreeApplication(qt_application, [])
    await started_application.start()
    await _wait_for_syncs(started_application)

    yield started_application

    started_application.stop()


def _contacts_state(application: HandsfreeApplication) -> str:
    """Return the phone's contacts sync state as the tabs would show it."""
    return application._contacts.state_for_address(PHONE_ADDRESS).sync_state


def _messages_state(application: HandsfreeApplication) -> str:
    """Return the phone's messages sync state as the tabs would show it."""
    return application._messages.state_for_address(PHONE_ADDRESS).sync_state


async def _wait_for_syncs(application: HandsfreeApplication) -> None:
    """Wait until the phone's phonebook and messages have both been synced."""
    await wait_until(lambda: _contacts_state(application) == CONTACTS_SYNCED, "phonebook")
    await wait_until(lambda: _messages_state(application) == MESSAGES_SYNCED, "messages")


def _open_pbap_count_never_exceeds_one(session_events: list[tuple[str, str, str]]) -> bool:
    """Tell whether the recorded PBAP sessions were open one at a time."""
    open_pbap_count = 0

    for event, _destination, target in session_events:
        if target != "pbap":
            continue
        if event == "created":
            open_pbap_count += 1
        else:
            open_pbap_count -= 1
        if open_pbap_count > 1:
            return False

    return True


def _summaries(notifications: FakeNotificationService) -> list[str]:
    """Return the summaries of every notification shown so far, in order."""
    summaries: list[str] = []
    for shown in notifications.shown:
        summaries.append(shown.summary)

    return summaries


async def test_phone_disconnect_and_reconnect(
    application: HandsfreeApplication,
    telephony: FakeTelephonyService,
    obex: FakeObexService,
    notifications: FakeNotificationService,
) -> None:
    """Disconnecting drops calls, data and notifications; reconnecting syncs again."""
    gateway_path = application._telephony.gateways[0].path
    telephony.add_call(gateway_path, CALL_STATE_INCOMING, "+15550100")
    await wait_until(lambda: "Incoming call" in _summaries(notifications), "call notification")
    obex.push_message(PUSHED_MESSAGE)
    await wait_until(lambda: "Alice Doe" in _summaries(notifications), "message notification")
    call_notification_id = notifications.shown[-2].notification_id
    message_notification_id = notifications.shown[-1].notification_id

    telephony.remove_gateway(gateway_path)

    await wait_until(lambda: "Phone disconnected" in _summaries(notifications), "disconnected")
    await wait_until(
        lambda: (
            sorted(notifications.closed_ids)
            == sorted([call_notification_id, message_notification_id])
        ),
        "notifications withdrawn",
    )
    assert application._contacts.state_for_address(PHONE_ADDRESS).phonebook is None
    assert application._messages.state_for_address(PHONE_ADDRESS).messages == ()
    await wait_until(lambda: obex.open_map_session_paths == [], "message session closed")
    assert application._telephony.calls == []

    telephony.add_gateway(PHONE_ADDRESS)

    await wait_until(lambda: _summaries(notifications).count("Phone connected") == 2, "reconnect")
    await _wait_for_syncs(application)
    assert len(obex.records.created_sessions) == 4
    assert application._contacts.lookup_name(PHONE_ADDRESS, "+15550100") == "Alice Doe"


async def test_telephony_restart_resynchronises(
    application: HandsfreeApplication,
    telephony: FakeTelephonyService,
    obex: FakeObexService,
    notifications: FakeNotificationService,
) -> None:
    """Losing WirePlumber drops the phone's data; its return triggers the same syncs."""
    await telephony.stop()

    await wait_until(
        lambda: "Telephony service unavailable" in _summaries(notifications), "unavailable"
    )
    await wait_until(lambda: "Phone disconnected" in _summaries(notifications), "disconnected")
    assert application._contacts.state_for_address(PHONE_ADDRESS).phonebook is None
    await wait_until(lambda: obex.open_map_session_paths == [], "message session closed")

    await telephony.start()

    await wait_until(lambda: application._telephony.is_available, "available again")
    await _wait_for_syncs(application)
    assert _summaries(notifications).count("Phone connected") == 2
    assert len(obex.open_map_session_paths) == 1


async def test_obexd_restart_is_reported_and_refresh_reconnects(
    application: HandsfreeApplication, obex: FakeObexService
) -> None:
    """An obexd restart marks messages as lost; Refresh reopens the session."""
    await obex.stop()

    await wait_until(lambda: _messages_state(application) == MESSAGES_FAILED, "lost")
    messages_state = application._messages.state_for_address(PHONE_ADDRESS)
    assert messages_state.error_text == CONNECTION_LOST_TEXT
    assert len(messages_state.messages) == 1

    await obex.start()
    gateway_path = application._telephony.gateways[0].path
    application._refresh_messages(gateway_path)

    await wait_until(lambda: _messages_state(application) == MESSAGES_SYNCED, "resynced")
    assert len(obex.open_map_session_paths) == 1


async def test_second_phone_during_a_call(
    application: HandsfreeApplication,
    telephony: FakeTelephonyService,
    bluez: FakeBlueZService,
    obex: FakeObexService,
    notifications: FakeNotificationService,
) -> None:
    """A second phone syncs after the first without touching the call window; alerts name phones."""
    first_gateway_path = application._telephony.gateways[0].path
    telephony.add_call(first_gateway_path, CALL_STATE_INCOMING, "+15550100")
    await wait_until(lambda: "Incoming call" in _summaries(notifications), "call notification")
    application._focus_call(f"{first_gateway_path}/call0")
    call_window = application._call_window
    assert call_window.shown_call_path == f"{first_gateway_path}/call0"

    bluez.add_device(SECOND_PHONE_ADDRESS, SECOND_PHONE_ALIAS)
    second_gateway_path = telephony.add_gateway(SECOND_PHONE_ADDRESS)
    await wait_until(
        lambda: (
            application._contacts.state_for_address(SECOND_PHONE_ADDRESS).sync_state
            == CONTACTS_SYNCED
        ),
        "second phonebook",
    )
    await wait_until(
        lambda: (
            application._messages.state_for_address(SECOND_PHONE_ADDRESS).sync_state
            == MESSAGES_SYNCED
        ),
        "second messages",
    )

    assert call_window.shown_call_path == f"{first_gateway_path}/call0"
    assert call_window.isVisible()
    assert _open_pbap_count_never_exceeds_one(obex.records.session_events)
    assert len(obex.open_map_session_paths) == 2
    assert f"Incoming call · {PHONE_ALIAS}" in _summaries(notifications)

    telephony.add_call(second_gateway_path, CALL_STATE_INCOMING, "+15550101")
    await wait_until(
        lambda: f"Incoming call · {SECOND_PHONE_ALIAS}" in _summaries(notifications),
        "second call names its phone",
    )
    obex.push_message(PUSHED_MESSAGE)
    await wait_until(
        lambda: f"Alice Doe · {SECOND_PHONE_ALIAS}" in _summaries(notifications),
        "message names its phone",
    )
    assert call_window.shown_call_path == f"{first_gateway_path}/call0"
