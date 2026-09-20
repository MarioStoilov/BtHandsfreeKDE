"""Fake `org.kde.StatusNotifierWatcher` recording tray registrations."""

from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from bt_handsfree_kde.dbus.statusnotifier import (
    WATCHER_BUS_NAME,
    WATCHER_INTERFACE,
    WATCHER_PATH,
)


class FakeWatcherInterface(ServiceInterface):
    """The watcher interface with a recording registration method."""

    def __init__(self) -> None:
        """Create the interface."""
        super().__init__(WATCHER_INTERFACE)
        self.registered_services: list[str] = []

    @method(name="RegisterStatusNotifierItem")
    def register_status_notifier_item(self, service: "s") -> None:
        """Record the registering connection."""
        self.registered_services.append(service)

    @dbus_property(access=PropertyAccess.READ, name="IsStatusNotifierHostRegistered")
    def is_host_registered(self) -> "b":
        """A host is always present in the fake."""
        return True

    @dbus_property(access=PropertyAccess.READ, name="ProtocolVersion")
    def protocol_version(self) -> "i":
        """Protocol version."""
        return 0


class FakeWatcherService:
    """Owns the watcher bus name."""

    def __init__(self, bus: MessageBus) -> None:
        """Create the service on `bus`."""
        self._bus = bus
        self.interface = FakeWatcherInterface()
        self._is_exported = False

    async def start(self) -> None:
        """Export the watcher (once) and claim its name."""
        if not self._is_exported:
            self._bus.export(WATCHER_PATH, self.interface)
            self._is_exported = True
        await self._bus.request_name(WATCHER_BUS_NAME)

    async def stop(self) -> None:
        """Give the name up, as a restarting kded would."""
        await self._bus.release_name(WATCHER_BUS_NAME)

    @property
    def registered_services(self) -> list[str]:
        """Return the unique names that registered an item."""
        return self.interface.registered_services
