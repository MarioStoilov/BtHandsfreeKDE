"""Tests for the desktop notifier against the fake notification server."""

from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.notifications import (
    ANSWER_ACTION,
    OPEN_MESSAGE_ACTION,
    URGENCY_CRITICAL,
    DesktopNotifier,
)
from tests.conftest import wait_until
from tests.fakes.notification_service import FakeNotificationService

CALL_PATH = "/org/pipewire/Telephony/ag1/call0"
MESSAGE_PATH = "/org/bluez/obex/client/session0/message1"


async def test_incoming_call_notification_round_trip(
    client_bus: MessageBus, notifications: FakeNotificationService
) -> None:
    """The call notification carries its buttons and hints; a press comes back; close closes."""
    notifier = DesktopNotifier(client_bus)
    actions = []
    notifier.action_invoked.connect(lambda key, action: actions.append((key, action)))
    await notifier.start()
    assert notifier.supports_call_notifications

    await notifier.show_incoming_call(CALL_PATH, "Alice")
    shown = notifications.shown[0]
    assert shown.summary == "Incoming call" and shown.body == "Alice"
    assert shown.actions == [ANSWER_ACTION, "Answer", "reject", "Reject"]
    assert shown.hints["urgency"] == URGENCY_CRITICAL and shown.hints["resident"] is True
    assert shown.expire_timeout == 0

    await notifier.show_incoming_call(CALL_PATH, "Alice Doe")
    assert notifications.shown[1].replaces_id == shown.notification_id

    await notifier.show_incoming_call(CALL_PATH, "Alice Doe", "Other phone")
    assert notifications.shown[2].summary == "Incoming call · Other phone"

    notifications.press(shown.notification_id, ANSWER_ACTION)
    await wait_until(lambda: actions == [(CALL_PATH, ANSWER_ACTION)], "action")

    await notifier.close_for_call(CALL_PATH)
    assert notifications.closed_ids == [shown.notification_id]


async def test_message_notification_and_capability_gate(
    client_bus: MessageBus, notifications: FakeNotificationService
) -> None:
    """A message notification is normal urgency with an Open button and a category hint."""
    notifier = DesktopNotifier(client_bus)
    actions = []
    notifier.action_invoked.connect(lambda key, action: actions.append((key, action)))
    await notifier.start()

    await notifier.show_new_message(MESSAGE_PATH, "Alice", "hello")
    shown = notifications.shown[0]
    assert shown.summary == "Alice" and shown.body == "hello"
    await notifier.show_new_message(MESSAGE_PATH, "Alice", "hello", "Other phone")
    assert notifications.shown[1].summary == "Alice · Other phone"
    assert shown.actions == [OPEN_MESSAGE_ACTION, "Open"]
    assert shown.hints["category"] == "im.received" and shown.hints["resident"] is False
    notifications.press(shown.notification_id, OPEN_MESSAGE_ACTION)
    await wait_until(lambda: actions == [(MESSAGE_PATH, OPEN_MESSAGE_ACTION)], "open action")


async def test_server_without_actions_disables_call_notifications(
    private_buses: str, connect_bus
) -> None:
    """A server lacking the actions capability makes the app fall back to the window."""
    server = FakeNotificationService(await connect_bus(), capabilities=["body"])
    await server.start()
    notifier = DesktopNotifier(await connect_bus())

    await notifier.start()

    assert not notifier.supports_call_notifications
