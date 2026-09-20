"""Desktop notifications for calls and messages through `org.freedesktop.Notifications`."""

import logging
from typing import Any

from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde import APPLICATION_ID, APPLICATION_NAME
from bt_handsfree_kde.dbus.helpers import DBusRequestError, call_method
from bt_handsfree_kde.icons import application_icon_reference

logger = logging.getLogger(__name__)

# Bus name, object path and interface of the notification server (session bus).
NOTIFICATIONS_BUS_NAME = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"

# Server capabilities the incoming-call notification depends on; without them the
# application shows the call window for ringing calls instead.
ACTIONS_CAPABILITY = "actions"
PERSISTENCE_CAPABILITY = "persistence"

# Action keys sent back by the server when the user presses a button.
ANSWER_ACTION = "answer"
REJECT_ACTION = "reject"
OPEN_MESSAGE_ACTION = "open-message"
# Notification category hints (notification specification) for the server's grouping.
INCOMING_CALL_CATEGORY = "call.incoming"
RECEIVED_MESSAGE_CATEGORY = "im.received"

# Urgency levels defined by the notification specification.
URGENCY_LOW = 0
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2
# Expiry values: never expire (call notifications) and server default (informational).
NEVER_EXPIRE_MS = 0
SERVER_DEFAULT_EXPIRY_MS = -1


class DesktopNotifier(QObject):
    """Shows and closes incoming-call and new-message notifications, plus informational ones.

    Notifications with buttons are keyed by the caller's subject (a call path, a message
    path), so a repeated show for the same key replaces the previous notification in
    place instead of stacking a new one.
    """

    # Emitted with (subject key, action key) when the user presses a notification button.
    action_invoked = Signal(str, str)

    def __init__(self, bus: MessageBus, parent: QObject | None = None) -> None:
        """Create a notifier bound to an already connected session bus."""
        super().__init__(parent)
        self._bus = bus
        self._notification_id_by_key: dict[str, int] = {}
        self._key_by_notification_id: dict[int, str] = {}
        self._supports_actions = False
        self._supports_persistence = False

    @property
    def supports_call_notifications(self) -> bool:
        """Tell whether the server can show persistent notifications with buttons."""
        return self._supports_actions and self._supports_persistence

    async def start(self) -> None:
        """Query the server's capabilities and subscribe to its action and close signals.

        A missing server is logged; `supports_call_notifications` then stays false and
        informational notifications are silently dropped.
        """
        try:
            introspection = await self._bus.introspect(NOTIFICATIONS_BUS_NAME, NOTIFICATIONS_PATH)
        except Exception as introspect_failure:
            logger.warning("notification server not reachable: %s", introspect_failure)
            return

        server_proxy = self._bus.get_proxy_object(
            NOTIFICATIONS_BUS_NAME, NOTIFICATIONS_PATH, introspection
        )
        server_interface = server_proxy.get_interface(NOTIFICATIONS_INTERFACE)
        server_interface.on_action_invoked(self._on_action_invoked)
        server_interface.on_notification_closed(self._on_notification_closed)

        capabilities = await server_interface.call_get_capabilities()
        self._supports_actions = ACTIONS_CAPABILITY in capabilities
        self._supports_persistence = PERSISTENCE_CAPABILITY in capabilities
        logger.info(
            "notification server: actions=%s persistence=%s",
            self._supports_actions,
            self._supports_persistence,
        )

    async def show_incoming_call(self, call_path: str, caller_label: str) -> None:
        """Show (or refresh) the critical, non-expiring notification for a ringing call."""
        await self._show_with_actions(
            call_path,
            "Incoming call",
            caller_label,
            [ANSWER_ACTION, "Answer", REJECT_ACTION, "Reject"],
            URGENCY_CRITICAL,
            INCOMING_CALL_CATEGORY,
            resident=True,
            expiry_ms=NEVER_EXPIRE_MS,
        )

    async def show_new_message(self, message_path: str, sender_label: str, preview: str) -> None:
        """Show a normal-urgency notification for a received message with an Open button.

        Args:
            message_path: Key the `action_invoked` signal reports for the Open button.
            sender_label: Contact name or address of the sender.
            preview: Start of the message text.
        """
        await self._show_with_actions(
            message_path,
            sender_label,
            preview,
            [OPEN_MESSAGE_ACTION, "Open"],
            URGENCY_NORMAL,
            RECEIVED_MESSAGE_CATEGORY,
            resident=False,
            expiry_ms=SERVER_DEFAULT_EXPIRY_MS,
        )

    async def close_for_call(self, call_path: str) -> None:
        """Close the notification that belongs to `call_path`, if there is one."""
        await self.close_for_key(call_path)

    async def close_for_key(self, key: str) -> None:
        """Close the notification shown for `key`, if there is one."""
        notification_id = self._notification_id_by_key.pop(key, None)
        if notification_id is None:
            return

        self._key_by_notification_id.pop(notification_id, None)
        try:
            await call_method(
                self._bus,
                NOTIFICATIONS_BUS_NAME,
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_INTERFACE,
                "CloseNotification",
                "u",
                [notification_id],
            )
        except DBusRequestError as request_error:
            logger.debug("closing notification failed: %s", request_error)

    async def show_information(self, summary: str, body: str, urgency: int = URGENCY_LOW) -> None:
        """Show a plain informational notification with the server's default expiry."""
        hints = _hints(urgency, resident=False, category="")
        icon_reference = application_icon_reference()

        try:
            await call_method(
                self._bus,
                NOTIFICATIONS_BUS_NAME,
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_INTERFACE,
                "Notify",
                "susssasa{sv}i",
                [
                    APPLICATION_NAME,
                    0,
                    icon_reference,
                    summary,
                    body,
                    [],
                    hints,
                    SERVER_DEFAULT_EXPIRY_MS,
                ],
            )
        except DBusRequestError as request_error:
            logger.debug("informational notification failed: %s", request_error)

    async def _show_with_actions(
        self,
        key: str,
        summary: str,
        body: str,
        actions: list[str],
        urgency: int,
        category: str,
        resident: bool,
        expiry_ms: int,
    ) -> None:
        """Send a notification with buttons for `key`, replacing an earlier one for it.

        Args:
            key: Subject the buttons act on, reported back through `action_invoked`.
            summary: Title line.
            body: Text below the title.
            actions: Alternating action keys and button labels.
            urgency: One of the `URGENCY_*` values.
            category: Notification category hint.
            resident: Whether the notification stays after a button was pressed.
            expiry_ms: Timeout, `NEVER_EXPIRE_MS` or `SERVER_DEFAULT_EXPIRY_MS`.
        """
        replaces_id = self._notification_id_by_key.get(key, 0)
        hints = _hints(urgency, resident, category)
        icon_reference = application_icon_reference()

        try:
            reply_body = await call_method(
                self._bus,
                NOTIFICATIONS_BUS_NAME,
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_INTERFACE,
                "Notify",
                "susssasa{sv}i",
                [
                    APPLICATION_NAME,
                    replaces_id,
                    icon_reference,
                    summary,
                    body,
                    actions,
                    hints,
                    expiry_ms,
                ],
            )
        except DBusRequestError as request_error:
            logger.warning("notification with actions failed: %s", request_error)
            return

        notification_id = int(reply_body[0])
        self._notification_id_by_key[key] = notification_id
        self._key_by_notification_id[notification_id] = key

    def _on_action_invoked(self, notification_id: int, action_key: str) -> None:
        """Translate a pressed notification button into an action signal for its key."""
        key = self._key_by_notification_id.get(notification_id)
        if key is None:
            return

        logger.info("notification action %s", action_key)
        self.action_invoked.emit(key, action_key)

    def _on_notification_closed(self, notification_id: int, reason: int) -> None:
        """Forget the mapping of a notification the server or the user closed."""
        key = self._key_by_notification_id.pop(notification_id, None)
        if key is None:
            return

        known_id = self._notification_id_by_key.get(key)
        if known_id == notification_id:
            del self._notification_id_by_key[key]


def _hints(urgency: int, resident: bool, category: str) -> dict[str, Any]:
    """Build the hints dictionary shared by every notification the app sends.

    Args:
        urgency: One of the `URGENCY_*` values.
        resident: Whether the notification stays after a button was pressed.
        category: Notification category hint; empty to send none.
    """
    hints = {
        "urgency": Variant("y", urgency),
        "desktop-entry": Variant("s", APPLICATION_ID),
        "resident": Variant("b", resident),
    }
    if category:
        hints["category"] = Variant("s", category)

    return hints
