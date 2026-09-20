"""Fake `org.bluez` service: paired devices with alias, connection state and battery."""

from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property

from bt_handsfree_kde.dbus.bluez import (
    BATTERY_INTERFACE,
    BLUEZ_BUS_NAME,
    BLUEZ_ROOT_PATH,
    DEVICE_INTERFACE,
)

# Interface exported on `/` so dbus-fast serves BlueZ's object manager there.
ROOT_ANCHOR_INTERFACE = "org.bluez.Root1"
# Adapter path the fake devices hang below.
ADAPTER_PATH = "/org/bluez/hci0"


class RootAnchorInterface(ServiceInterface):
    """Empty interface that anchors the object manager on the root path."""

    def __init__(self) -> None:
        """Create the anchor."""
        super().__init__(ROOT_ANCHOR_INTERFACE)


class FakeDevice(ServiceInterface):
    """`org.bluez.Device1` of one paired device."""

    def __init__(self, address: str, alias: str, connected: bool) -> None:
        """Create the device interface with its initial properties."""
        super().__init__(DEVICE_INTERFACE)
        self._address = address
        self._alias = alias
        self._connected = connected

    def set_alias(self, alias: str) -> None:
        """Change the alias and announce it."""
        self._alias = alias
        self.emit_properties_changed({"Alias": alias})

    def set_connected(self, connected: bool) -> None:
        """Change the connection state and announce it."""
        self._connected = connected
        self.emit_properties_changed({"Connected": connected})

    @dbus_property(access=PropertyAccess.READ, name="Address")
    def address(self) -> "s":
        """Bluetooth address."""
        return self._address

    @dbus_property(access=PropertyAccess.READ, name="Alias")
    def alias(self) -> "s":
        """User-visible name."""
        return self._alias

    @dbus_property(access=PropertyAccess.READ, name="Connected")
    def connected(self) -> "b":
        """Whether the device is connected."""
        return self._connected


class FakeBattery(ServiceInterface):
    """`org.bluez.Battery1` of one device."""

    def __init__(self, percentage: int) -> None:
        """Create the battery interface."""
        super().__init__(BATTERY_INTERFACE)
        self._percentage = percentage

    def set_percentage(self, percentage: int) -> None:
        """Change the level and announce it."""
        self._percentage = percentage
        self.emit_properties_changed({"Percentage": percentage})

    @dbus_property(access=PropertyAccess.READ, name="Percentage")
    def percentage(self) -> "y":
        """Battery level in percent."""
        return self._percentage


class FakeBlueZService:
    """Owns the BlueZ bus name on the test's system bus and exports devices on demand."""

    def __init__(self, bus: MessageBus) -> None:
        """Create the service on `bus`; `start` claims the name."""
        self._bus = bus
        self._devices_by_path: dict[str, FakeDevice] = {}
        self._batteries_by_path: dict[str, FakeBattery] = {}

    async def start(self) -> None:
        """Export the root object and claim `org.bluez`."""
        self._bus.export(BLUEZ_ROOT_PATH, RootAnchorInterface())
        await self._bus.request_name(BLUEZ_BUS_NAME)

    def add_device(
        self,
        address: str,
        alias: str,
        connected: bool = True,
        battery_percentage: int | None = None,
    ) -> str:
        """Export a device (and its battery when given) and return its object path."""
        path_suffix = address.replace(":", "_")
        device_path = f"{ADAPTER_PATH}/dev_{path_suffix}"

        device = FakeDevice(address, alias, connected)
        self._bus.export(device_path, device)
        self._devices_by_path[device_path] = device
        if battery_percentage is not None:
            self.set_battery(device_path, battery_percentage)

        return device_path

    def set_alias(self, device_path: str, alias: str) -> None:
        """Rename a device."""
        self._devices_by_path[device_path].set_alias(alias)

    def set_connected(self, device_path: str, connected: bool) -> None:
        """Change a device's connection state."""
        self._devices_by_path[device_path].set_connected(connected)

    def set_battery(self, device_path: str, percentage: int) -> None:
        """Report a battery level, exporting the battery interface on first use."""
        battery = self._batteries_by_path.get(device_path)
        if battery is None:
            battery = FakeBattery(percentage)
            self._bus.export(device_path, battery)
            self._batteries_by_path[device_path] = battery
            return

        battery.set_percentage(percentage)

    def remove_battery(self, device_path: str) -> None:
        """Withdraw the battery interface, as BlueZ does when the level becomes unknown."""
        battery = self._batteries_by_path.pop(device_path)
        self._bus.unexport(device_path, battery)

    def remove_device(self, device_path: str) -> None:
        """Withdraw a device, as BlueZ does when it is unpaired."""
        self._batteries_by_path.pop(device_path, None)
        self._bus.unexport(device_path)
        del self._devices_by_path[device_path]
