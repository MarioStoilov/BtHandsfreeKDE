"""Desktop notifications for calls through `org.freedesktop.Notifications`."""

import logging
from typing import Any

from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde import APPLICATION_ID, APPLICATION_NAME
from bt_handsfree_kde.dbus_helpers import DBusRequestError, call_method

logger = logging.getLogger(__name__)

# Bus name, object path and interface of the notification server (session bus).
NOTIFICATIONS_BUS_NAME = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"

# Server capabilities the call notifications depend on; without them the application
# falls back to the active-call window.
ACTIONS_CAPABILITY = "actions"
PERSISTENCE_CAPABILITY = "persistence"

# Action keys sent back by the server when the user presses a button.
ANSWER_ACTION = "answer"
REJECT_ACTION = "reject"
HOLD_ACTION = "hold"
RESUME_ACTION = "resume"
HANGUP_ACTION = "hangup"

# Urgency levels defined by the notification specification.
URGENCY_LOW = 0
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2
# Expiry values: never expire (call notifications) and server default (informational).
NEVER_EXPIRE_MS = 0
SERVER_DEFAULT_EXPIRY_MS = -1
# Icon names shown by the server, from the freedesktop icon naming specification.
INCOMING_CALL_ICON = "call-start"
ACTIVE_CALL_ICON = "call-start"


class CallNotifier(QObject):
    """Shows, updates and closes the notifications that belong to calls.

    Keeps one notification per call path so a state change replaces the previous
    notification in place instead of stacking a new one.
    """

    # Emitted with (call path, action key) when the user presses a notification button.
    action_invoked = Signal(str, str)

    def __init__(self, bus: MessageBus, parent: QObject | None = None) -> None:
        """Create a notifier bound to an already connected session bus."""
        super().__init__(parent)
        self._bus = bus
        self._notification_id_by_call_path: dict[str, int] = {}
        self._call_path_by_notification_id: dict[int, str] = {}
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
        await self._show_for_call(
            call_path,
            INCOMING_CALL_ICON,
            "Incoming call",
            caller_label,
            [ANSWER_ACTION, "Answer", REJECT_ACTION, "Reject"],
            URGENCY_CRITICAL,
        )

    async def show_active_call(
        self, call_path: str, caller_label: str, status_text: str, is_held: bool
    ) -> None:
        """Show (or refresh in place) the persistent notification for a call in progress.

        Args:
            call_path: Object path of the call.
            caller_label: Name or number to display.
            status_text: Second line, typically the elapsed duration or the dialing state.
            is_held: When true the hold button reads Resume instead of Hold.
        """
        hold_label = "Resume" if is_held else "Hold"
        hold_action = RESUME_ACTION if is_held else HOLD_ACTION
        body_text = f"{caller_label}\n{status_text}"

        await self._show_for_call(
            call_path,
            ACTIVE_CALL_ICON,
            "Call in progress",
            body_text,
            [hold_action, hold_label, HANGUP_ACTION, "Hang up"],
            URGENCY_NORMAL,
        )

    async def close_for_call(self, call_path: str) -> None:
        """Close the notification that belongs to `call_path`, if there is one."""
        notification_id = self._notification_id_by_call_path.pop(call_path, None)
        if notification_id is None:
            return

        self._call_path_by_notification_id.pop(notification_id, None)
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
        hints = _hints(urgency, resident=False)

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
                    APPLICATION_ID,
                    summary,
                    body,
                    [],
                    hints,
                    SERVER_DEFAULT_EXPIRY_MS,
                ],
            )
        except DBusRequestError as request_error:
            logger.debug("informational notification failed: %s", request_error)

    async def _show_for_call(
        self,
        call_path: str,
        icon_name: str,
        summary: str,
        body: str,
        actions: list[str],
        urgency: int,
    ) -> None:
        """Send a non-expiring, resident notification for a call, replacing any earlier one."""
        replaces_id = self._notification_id_by_call_path.get(call_path, 0)
        hints = _hints(urgency, resident=True)

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
                    icon_name,
                    summary,
                    body,
                    actions,
                    hints,
                    NEVER_EXPIRE_MS,
                ],
            )
        except DBusRequestError as request_error:
            logger.warning("call notification failed: %s", request_error)
            return

        notification_id = int(reply_body[0])
        self._notification_id_by_call_path[call_path] = notification_id
        self._call_path_by_notification_id[notification_id] = call_path

    def _on_action_invoked(self, notification_id: int, action_key: str) -> None:
        """Translate a pressed notification button into a call action signal."""
        call_path = self._call_path_by_notification_id.get(notification_id)
        if call_path is None:
            return

        logger.info("notification action %s", action_key)
        self.action_invoked.emit(call_path, action_key)

    def _on_notification_closed(self, notification_id: int, reason: int) -> None:
        """Forget the mapping of a notification the server or the user closed."""
        call_path = self._call_path_by_notification_id.pop(notification_id, None)
        if call_path is None:
            return

        known_id = self._notification_id_by_call_path.get(call_path)
        if known_id == notification_id:
            del self._notification_id_by_call_path[call_path]


def _hints(urgency: int, resident: bool) -> dict[str, Any]:
    """Build the hints dictionary shared by every notification the app sends."""
    return {
        "urgency": Variant("y", urgency),
        "desktop-entry": Variant("s", APPLICATION_ID),
        "resident": Variant("b", resident),
    }
