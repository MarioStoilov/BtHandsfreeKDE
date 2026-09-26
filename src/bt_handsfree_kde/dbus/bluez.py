"""Client for the phone's BlueZ device object: name, connection state and battery level."""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.dbus.helpers import (
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
    DBusRequestError,
    add_signal_match,
    call_method,
    is_name_owner_changed,
    name_has_owner,
    name_owner_changed_match_rule,
    unwrap_variant,
)

logger = logging.getLogger(__name__)

# Well-known bus name of the Bluetooth daemon (system bus) and its object manager root.
BLUEZ_BUS_NAME = "org.bluez"
BLUEZ_ROOT_PATH = "/"
# Interfaces read from each device object.
DEVICE_INTERFACE = "org.bluez.Device1"
BATTERY_INTERFACE = "org.bluez.Battery1"
# Prefix shared by every BlueZ object path; used to filter signals.
BLUEZ_PATH_PREFIX = "/org/bluez/"

# Dataclass field written by each D-Bus property, per interface.
DEVICE_FIELD_BY_PROPERTY = {
    "Address": "address",
    "Alias": "alias",
    "Connected": "connected",
}
BATTERY_FIELD_BY_PROPERTY = {
    "Percentage": "battery_percentage",
}


@dataclass(frozen=True)
class PhoneInfo:
    """What BlueZ knows about one paired device."""

    path: str
    address: str
    alias: str
    connected: bool
    battery_percentage: int | None


class PhoneInfoClient(QObject):
    """Mirrors BlueZ device objects so the UI can show phone name, state and battery.

    Tracks every device BlueZ manages, which keeps the address-to-object mapping trivial;
    the application looks up only the addresses of active audio gateways.
    """

    # Emitted with a `PhoneInfo` whenever a device appears, disappears or changes.
    phone_info_changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        """Create a client; nothing is connected until `start` runs."""
        super().__init__(parent)
        self._bus: MessageBus | None = None
        self._devices_by_path: dict[str, PhoneInfo] = {}
        self._is_available = False

    @property
    def is_available(self) -> bool:
        """Tell whether BlueZ could be reached on the system bus."""
        return self._is_available

    def info_for_address(self, address: str) -> PhoneInfo | None:
        """Return the device with Bluetooth `address`, comparing case-insensitively."""
        wanted_address = address.upper()

        for device in self._devices_by_path.values():
            device_address = device.address.upper()
            if device_address == wanted_address:
                return device

        return None

    async def start(self) -> None:
        """Connect to the system bus, subscribe to BlueZ signals and load the device list.

        Failure to reach the system bus is logged and leaves the client unavailable for
        good; the application then shows phones without name and battery. BlueZ itself
        being absent is not final: the client loads the devices as soon as the name
        appears, and drops them again when it leaves, so a restart of the Bluetooth
        daemon needs nothing from the caller.
        """
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as connect_failure:
            logger.warning(
                "system bus not reachable, phone details unavailable: %s", connect_failure
            )
            return

        # Signal subscriptions come first so nothing is missed between the initial
        # snapshot and the moment the handlers are active.
        self._bus.add_message_handler(self._handle_message)
        try:
            await add_signal_match(
                self._bus,
                f"type='signal',sender='{BLUEZ_BUS_NAME}',interface='{OBJECT_MANAGER_INTERFACE}'",
            )
            await add_signal_match(
                self._bus,
                f"type='signal',sender='{BLUEZ_BUS_NAME}',interface='{PROPERTIES_INTERFACE}'",
            )
            await add_signal_match(self._bus, name_owner_changed_match_rule(BLUEZ_BUS_NAME))
            bluez_is_running = await name_has_owner(self._bus, BLUEZ_BUS_NAME)
        except DBusRequestError as request_error:
            logger.warning("system bus daemon refused a request: %s", request_error)
            return

        if bluez_is_running:
            await self._synchronise()
        else:
            logger.warning("BlueZ is not on the system bus; phone details unavailable")

    def stop(self) -> None:
        """Close the system bus connection; nothing is delivered afterwards."""
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _synchronise(self) -> None:
        """Replace the known devices with BlueZ's current object tree and announce them."""
        try:
            reply_body = await call_method(
                self._bus,
                BLUEZ_BUS_NAME,
                BLUEZ_ROOT_PATH,
                OBJECT_MANAGER_INTERFACE,
                "GetManagedObjects",
            )
        except DBusRequestError as request_error:
            logger.warning("BlueZ not reachable, phone details unavailable: %s", request_error)
            return

        managed_objects = unwrap_variant(reply_body[0])
        self._devices_by_path.clear()
        for object_path, interfaces in managed_objects.items():
            if DEVICE_INTERFACE in interfaces:
                self._devices_by_path[object_path] = _build_phone_info(object_path, interfaces)

        device_count = len(self._devices_by_path)
        logger.info("BlueZ reachable: %d paired device(s)", device_count)
        self._is_available = True

        for phone_info in list(self._devices_by_path.values()):
            self.phone_info_changed.emit(phone_info)

    def _handle_message(self, message: Message) -> None:
        """Dispatch BlueZ signals to the matching handler; returns `None` to pass them on."""
        if message.message_type != MessageType.SIGNAL:
            return None

        if is_name_owner_changed(message):
            self._on_name_owner_changed(message.body)
            return None

        is_bluez_path = message.path is not None and (
            message.path == BLUEZ_ROOT_PATH or message.path.startswith(BLUEZ_PATH_PREFIX)
        )
        if not is_bluez_path:
            return None

        if message.interface == OBJECT_MANAGER_INTERFACE and message.member == "InterfacesAdded":
            self._on_interfaces_added(message.body)
        elif (
            message.interface == OBJECT_MANAGER_INTERFACE and message.member == "InterfacesRemoved"
        ):
            self._on_interfaces_removed(message.body)
        elif message.interface == PROPERTIES_INTERFACE and message.member == "PropertiesChanged":
            self._on_properties_changed(message.path, message.body)

        return None

    def _on_name_owner_changed(self, signal_body: list[Any]) -> None:
        """Forget every device when BlueZ leaves the bus; reload them when it returns."""
        changed_name = signal_body[0]
        new_owner = signal_body[2]
        if changed_name != BLUEZ_BUS_NAME:
            return

        service_left = new_owner == ""
        if service_left:
            logger.warning("BlueZ left the system bus; phone details unavailable")
            self._is_available = False
            forgotten_devices = list(self._devices_by_path.values())
            self._devices_by_path.clear()
            for device in forgotten_devices:
                disconnected_device = replace(device, connected=False, battery_percentage=None)
                self.phone_info_changed.emit(disconnected_device)
        else:
            logger.info("BlueZ appeared on the system bus")
            asyncio.get_event_loop().create_task(self._synchronise())

    def _on_interfaces_added(self, signal_body: list[Any]) -> None:
        """Register a new device, or attach a battery interface to a known one."""
        object_path = signal_body[0]
        interfaces = unwrap_variant(signal_body[1])

        if DEVICE_INTERFACE in interfaces:
            self._devices_by_path[object_path] = _build_phone_info(object_path, interfaces)
            self.phone_info_changed.emit(self._devices_by_path[object_path])
            return

        battery_appeared = BATTERY_INTERFACE in interfaces and object_path in self._devices_by_path
        if battery_appeared:
            battery_properties = interfaces[BATTERY_INTERFACE]
            percentage = battery_properties.get("Percentage")
            current_device = self._devices_by_path[object_path]
            self._devices_by_path[object_path] = replace(
                current_device, battery_percentage=_optional_int(percentage)
            )
            self.phone_info_changed.emit(self._devices_by_path[object_path])

    def _on_interfaces_removed(self, signal_body: list[Any]) -> None:
        """Forget a device, or clear its battery level when only that interface goes away."""
        object_path = signal_body[0]
        removed_interfaces = signal_body[1]
        if object_path not in self._devices_by_path:
            return

        device_removed = DEVICE_INTERFACE in removed_interfaces
        if device_removed:
            removed_device = self._devices_by_path.pop(object_path)
            disconnected_device = replace(removed_device, connected=False, battery_percentage=None)
            self.phone_info_changed.emit(disconnected_device)
            return

        battery_removed = BATTERY_INTERFACE in removed_interfaces
        if battery_removed:
            current_device = self._devices_by_path[object_path]
            self._devices_by_path[object_path] = replace(current_device, battery_percentage=None)
            self.phone_info_changed.emit(self._devices_by_path[object_path])

    def _on_properties_changed(self, object_path: str, signal_body: list[Any]) -> None:
        """Apply a device or battery property change."""
        if object_path not in self._devices_by_path:
            return

        changed_interface = signal_body[0]
        changed_properties = unwrap_variant(signal_body[1])
        field_updates: dict[str, Any] = {}

        if changed_interface == DEVICE_INTERFACE:
            for property_name, property_value in changed_properties.items():
                field_name = DEVICE_FIELD_BY_PROPERTY.get(property_name)
                if field_name is not None:
                    field_updates[field_name] = property_value
        elif changed_interface == BATTERY_INTERFACE:
            percentage = changed_properties.get("Percentage")
            if percentage is not None:
                field_updates["battery_percentage"] = int(percentage)

        if field_updates:
            current_device = self._devices_by_path[object_path]
            self._devices_by_path[object_path] = replace(current_device, **field_updates)
            self.phone_info_changed.emit(self._devices_by_path[object_path])


def _optional_int(value: Any) -> int | None:
    """Return `value` as an int, or `None` when it is missing."""
    if value is None:
        return None

    return int(value)


def _build_phone_info(object_path: str, interfaces: dict[str, dict[str, Any]]) -> PhoneInfo:
    """Create a `PhoneInfo` from an object manager entry (variants already unwrapped)."""
    device_properties = interfaces.get(DEVICE_INTERFACE, {})
    battery_properties = interfaces.get(BATTERY_INTERFACE, {})

    address = device_properties.get("Address", "")
    alias = device_properties.get("Alias", "")
    connected = device_properties.get("Connected", False)
    percentage = battery_properties.get("Percentage")

    return PhoneInfo(
        path=object_path,
        address=str(address),
        alias=str(alias),
        connected=bool(connected),
        battery_percentage=_optional_int(percentage),
    )
