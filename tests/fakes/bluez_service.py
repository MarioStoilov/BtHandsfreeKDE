"""Fake `org.bluez` service: devices with alias, pairing, services, connection state and battery."""

from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property

from bt_handsfree_kde.dbus.bluez import (
    BATTERY_INTERFACE,
    BLUEZ_BUS_NAME,
    BLUEZ_ROOT_PATH,
    DEVICE_INTERFACE,
    HANDS_FREE_AUDIO_GATEWAY_UUID,
)

# Interface exported on `/` so dbus-fast serves BlueZ's object manager there.
ROOT_ANCHOR_INTERFACE = "org.bluez.Root1"
# Adapter path the fake devices hang below.
ADAPTER_PATH = "/org/bluez/hci0"
# Icon name BlueZ reports for a phone-class device.
PHONE_ICON_NAME = "phone"
# Service list of a device that can be a hands-free gateway.
HANDS_FREE_SERVICE_UUIDS = [HANDS_FREE_AUDIO_GATEWAY_UUID]


class RootAnchorInterface(ServiceInterface):
    """Empty interface that anchors the object manager on the root path."""

    def __init__(self) -> None:
        """Create the anchor."""
        super().__init__(ROOT_ANCHOR_INTERFACE)


class FakeDevice(ServiceInterface):
    """`org.bluez.Device1` of one device BlueZ has seen or paired with."""

    def __init__(
        self,
        address: str,
        alias: str,
        connected: bool,
        paired: bool,
        icon_name: str,
        service_uuids: list[str],
    ) -> None:
        """Create the device interface with its initial properties."""
        super().__init__(DEVICE_INTERFACE)
        self._address = address
        self._alias = alias
        self._connected = connected
        self._paired = paired
        self._icon_name = icon_name
        self._service_uuids = list(service_uuids)

    def set_alias(self, alias: str) -> None:
        """Change the alias and announce it."""
        self._alias = alias
        self.emit_properties_changed({"Alias": alias})

    def set_connected(self, connected: bool) -> None:
        """Change the connection state and announce it."""
        self._connected = connected
        self.emit_properties_changed({"Connected": connected})

    def set_paired(self, paired: bool) -> None:
        """Change the pairing state and announce it."""
        self._paired = paired
        self.emit_properties_changed({"Paired": paired})

    def set_service_uuids(self, service_uuids: list[str]) -> None:
        """Replace the advertised services and announce them."""
        self._service_uuids = list(service_uuids)
        self.emit_properties_changed({"UUIDs": self._service_uuids})

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

    @dbus_property(access=PropertyAccess.READ, name="Paired")
    def paired(self) -> "b":
        """Whether the device is paired with the computer."""
        return self._paired

    @dbus_property(access=PropertyAccess.READ, name="Icon")
    def icon_name(self) -> "s":
        """Freedesktop icon name derived from the device class."""
        return self._icon_name

    @dbus_property(access=PropertyAccess.READ, name="UUIDs")
    def service_uuids(self) -> "as":
        """Services the device advertises."""
        return self._service_uuids


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
        self._is_root_exported = False

    async def start(self) -> None:
        """Export the root object (once) and claim `org.bluez`.

        After a `stop`, starting again re-claims the name while the devices stay
        exported, like a restarted BlueZ that loaded the same paired devices.
        """
        if not self._is_root_exported:
            self._bus.export(BLUEZ_ROOT_PATH, RootAnchorInterface())
            self._is_root_exported = True

        await self._bus.request_name(BLUEZ_BUS_NAME)

    async def stop(self) -> None:
        """Give the name up, as a stopping BlueZ would; the objects stay exported."""
        await self._bus.release_name(BLUEZ_BUS_NAME)

    def add_device(
        self,
        address: str,
        alias: str,
        connected: bool = True,
        battery_percentage: int | None = None,
        paired: bool = True,
        icon_name: str = PHONE_ICON_NAME,
        service_uuids: list[str] | None = None,
    ) -> str:
        """Export a device (and its battery when given) and return its object path.

        By default the device is a paired phone offering the hands-free gateway
        service; pass `paired=False` or another `service_uuids` list for devices the
        UI must leave out.
        """
        path_suffix = address.replace(":", "_")
        device_path = f"{ADAPTER_PATH}/dev_{path_suffix}"
        if service_uuids is None:
            service_uuids = list(HANDS_FREE_SERVICE_UUIDS)

        device = FakeDevice(address, alias, connected, paired, icon_name, service_uuids)
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

    def set_paired(self, device_path: str, paired: bool) -> None:
        """Change a device's pairing state."""
        self._devices_by_path[device_path].set_paired(paired)

    def set_service_uuids(self, device_path: str, service_uuids: list[str]) -> None:
        """Replace the services a device advertises."""
        self._devices_by_path[device_path].set_service_uuids(service_uuids)

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
