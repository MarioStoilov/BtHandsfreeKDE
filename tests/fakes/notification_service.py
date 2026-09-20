"""Fake `org.freedesktop.Notifications` server recording what the app sends."""

from dataclasses import dataclass
from typing import Any

from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal

from bt_handsfree_kde.dbus.helpers import unwrap_variant
from bt_handsfree_kde.dbus.notifications import (
    NOTIFICATIONS_BUS_NAME,
    NOTIFICATIONS_INTERFACE,
    NOTIFICATIONS_PATH,
)

# Capabilities of a server that supports everything the app uses.
FULL_CAPABILITIES = ["actions", "persistence", "body"]
# Close reason reported when the app closed the notification itself.
CLOSED_BY_CALL_REASON = 3


@dataclass(frozen=True)
class ShownNotification:
    """One `Notify` call as received by the fake server."""

    notification_id: int
    replaces_id: int
    summary: str
    body: str
    actions: list[str]
    hints: dict[str, Any]
    expire_timeout: int


class FakeNotificationsInterface(ServiceInterface):
    """The notification server interface with a recording `Notify`."""

    def __init__(self, capabilities: list[str]) -> None:
        """Create the interface advertising `capabilities`."""
        super().__init__(NOTIFICATIONS_INTERFACE)
        self._capabilities = capabilities
        self._next_id = 1
        self.shown: list[ShownNotification] = []
        self.closed_ids: list[int] = []

    @method(name="Notify")
    def notify(
        self,
        app_name: "s",
        replaces_id: "u",
        app_icon: "s",
        summary: "s",
        body: "s",
        actions: "as",
        hints: "a{sv}",
        expire_timeout: "i",
    ) -> "u":
        """Record the notification and return its identifier."""
        notification_id = replaces_id if replaces_id else self._next_id
        if not replaces_id:
            self._next_id += 1

        self.shown.append(
            ShownNotification(
                notification_id,
                replaces_id,
                summary,
                body,
                list(actions),
                unwrap_variant(hints),
                expire_timeout,
            )
        )

        return notification_id

    @method(name="CloseNotification")
    def close_notification(self, notification_id: "u") -> None:
        """Record the close and announce it as the server would."""
        self.closed_ids.append(notification_id)
        self.notification_closed(notification_id, CLOSED_BY_CALL_REASON)

    @method(name="GetCapabilities")
    def get_capabilities(self) -> "as":
        """Advertise the configured capabilities."""
        return self._capabilities

    @method(name="GetServerInformation")
    def get_server_information(self) -> "ssss":
        """Identify the fake server."""
        return ["fake", "bt-handsfree-kde tests", "1", "1.2"]

    @signal(name="ActionInvoked")
    def action_invoked(self, notification_id: int, action_key: str) -> "us":
        """Announce a pressed button."""
        return [notification_id, action_key]

    @signal(name="NotificationClosed")
    def notification_closed(self, notification_id: int, reason: int) -> "uu":
        """Announce a closed notification."""
        return [notification_id, reason]


class FakeNotificationService:
    """Owns the notification bus name and lets tests press buttons."""

    def __init__(self, bus: MessageBus, capabilities: list[str] | None = None) -> None:
        """Create the service; `capabilities` defaults to a fully capable server."""
        self._bus = bus
        advertised = capabilities if capabilities is not None else FULL_CAPABILITIES
        self.interface = FakeNotificationsInterface(advertised)

    async def start(self) -> None:
        """Export the server object and claim its name."""
        self._bus.export(NOTIFICATIONS_PATH, self.interface)
        await self._bus.request_name(NOTIFICATIONS_BUS_NAME)

    @property
    def shown(self) -> list[ShownNotification]:
        """Return every notification received so far."""
        return self.interface.shown

    @property
    def closed_ids(self) -> list[int]:
        """Return the identifiers the app asked to close."""
        return self.interface.closed_ids

    def press(self, notification_id: int, action_key: str) -> None:
        """Simulate the user pressing a button."""
        self.interface.action_invoked(notification_id, action_key)
