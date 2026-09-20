"""Client for PipeWire's telephony D-Bus service (`org.pipewire.Telephony`).

The service is owned by WirePlumber when PipeWire's native HFP backend is in use. It
publishes one audio gateway object per connected phone and one call object per call.
This module discovers them, tracks their state and offers the call-control actions.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.dbus.helpers import (
    DBUS_DAEMON_BUS_NAME,
    DBUS_DAEMON_INTERFACE,
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
    DBusRequestError,
    add_signal_match,
    call_method,
    name_has_owner,
    set_property,
    unwrap_variant,
)

logger = logging.getLogger(__name__)

# Well-known bus name of the telephony service (session bus).
TELEPHONY_BUS_NAME = "org.pipewire.Telephony"
# Root object that implements ObjectManager; gateways and calls are its descendants.
TELEPHONY_ROOT_PATH = "/org/pipewire/Telephony"
# Interface names published by the service.
AUDIO_GATEWAY_INTERFACE = "org.pipewire.Telephony.AudioGateway1"
TRANSPORT_INTERFACE = "org.pipewire.Telephony.AudioGatewayTransport1"
CALL_INTERFACE = "org.pipewire.Telephony.Call1"

# Call states as reported in `Call1.State` (same vocabulary as oFono).
CALL_STATE_ACTIVE = "active"
CALL_STATE_HELD = "held"
CALL_STATE_DIALING = "dialing"
CALL_STATE_ALERTING = "alerting"
CALL_STATE_INCOMING = "incoming"
CALL_STATE_WAITING = "waiting"
CALL_STATE_DISCONNECTED = "disconnected"
# States in which the user is being asked to accept a call.
INCOMING_CALL_STATES = frozenset({CALL_STATE_INCOMING, CALL_STATE_WAITING})
# States in which an outgoing call has not been answered by the far end yet.
OUTGOING_PENDING_STATES = frozenset({CALL_STATE_DIALING, CALL_STATE_ALERTING})

# HFP volume gain range for speaker and microphone, inclusive.
MIN_VOLUME_LEVEL = 0
MAX_VOLUME_LEVEL = 15

# Dataclass field written by each D-Bus property, per interface.
GATEWAY_FIELD_BY_PROPERTY = {
    "Address": "address",
    "SpeakerVolume": "speaker_volume",
    "MicrophoneVolume": "microphone_volume",
}
TRANSPORT_FIELD_BY_PROPERTY = {
    "State": "transport_state",
    "Codec": "codec",
    "RejectSCO": "reject_sco",
}
CALL_FIELD_BY_PROPERTY = {
    "State": "state",
    "LineIdentification": "line_identification",
    "Name": "name",
    "IncomingLine": "incoming_line",
    "Multiparty": "multiparty",
}


class TelephonyError(Exception):
    """A call-control request was rejected by the telephony service or could not be sent."""


@dataclass(frozen=True)
class AudioGateway:
    """One connected phone as seen by the telephony service."""

    path: str
    address: str
    speaker_volume: int
    microphone_volume: int
    transport_state: str
    codec: int
    reject_sco: bool


@dataclass(frozen=True)
class Call:
    """One call on an audio gateway."""

    path: str
    gateway_path: str
    state: str
    line_identification: str
    name: str
    incoming_line: str
    multiparty: bool

    @property
    def is_incoming(self) -> bool:
        """Tell whether the call is waiting for the user to accept or reject it."""
        return self.state in INCOMING_CALL_STATES

    @property
    def caller_label(self) -> str:
        """Return the best available caller description: name, else number, else a fallback."""
        if self.name:
            return self.name

        if self.line_identification:
            return self.line_identification

        return "Unknown number"


class TelephonyClient(QObject):
    """Tracks gateways and calls of `org.pipewire.Telephony` and issues call-control requests.

    Emits Qt signals from the asyncio callbacks; both run on the Qt thread under qasync.
    """

    # Emitted when the telephony service appears or disappears from the bus.
    availability_changed = Signal(bool)
    # Emitted when a gateway is added, removed or changes a property.
    gateways_changed = Signal()
    # Emitted with a `Call` when a call object appears or changes state.
    call_added = Signal(object)
    call_changed = Signal(object)
    # Emitted with the call's object path when the call object is removed.
    call_removed = Signal(str)

    def __init__(self, bus: MessageBus, parent: QObject | None = None) -> None:
        """Create a client bound to an already connected session bus.

        Args:
            bus: Connected session bus shared with the other clients.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._bus = bus
        self._gateways: dict[str, AudioGateway] = {}
        self._calls: dict[str, Call] = {}
        self._is_available = False

    @property
    def is_available(self) -> bool:
        """Tell whether the telephony service is currently on the bus."""
        return self._is_available

    @property
    def gateways(self) -> list[AudioGateway]:
        """Return the known gateways in discovery order."""
        return list(self._gateways.values())

    @property
    def calls(self) -> list[Call]:
        """Return the known calls in discovery order."""
        return list(self._calls.values())

    def gateway_for(self, path: str) -> AudioGateway | None:
        """Return the gateway at `path`, or `None` when unknown."""
        return self._gateways.get(path)

    def call_for(self, path: str) -> Call | None:
        """Return the call at `path`, or `None` when unknown."""
        return self._calls.get(path)

    async def start(self) -> None:
        """Subscribe to bus signals and load the current gateways and calls.

        Safe to call when the service is not running: the client then waits for it to
        appear and synchronises at that moment.

        Raises:
            DBusRequestError: the bus daemon refused the signal subscriptions.
        """
        # Signal subscriptions come first so nothing is missed between the initial
        # snapshot and the moment the handlers are active.
        self._bus.add_message_handler(self._handle_message)
        await add_signal_match(
            self._bus,
            f"type='signal',sender='{TELEPHONY_BUS_NAME}',interface='{OBJECT_MANAGER_INTERFACE}'",
        )
        await add_signal_match(
            self._bus,
            f"type='signal',sender='{TELEPHONY_BUS_NAME}',interface='{PROPERTIES_INTERFACE}'",
        )
        await add_signal_match(
            self._bus,
            f"type='signal',sender='{DBUS_DAEMON_BUS_NAME}',interface='{DBUS_DAEMON_INTERFACE}',"
            f"member='NameOwnerChanged',arg0='{TELEPHONY_BUS_NAME}'",
        )

        service_is_running = await name_has_owner(self._bus, TELEPHONY_BUS_NAME)
        if service_is_running:
            await self._synchronise()
        else:
            logger.warning("telephony service %s is not on the bus", TELEPHONY_BUS_NAME)
            self._set_available(False)

    async def answer(self, call_path: str) -> None:
        """Accept the incoming call at `call_path`.

        Raises:
            TelephonyError: the call is unknown or the phone rejected the request.
        """
        await self._call_on_object(call_path, CALL_INTERFACE, "Answer")

    async def hangup(self, call_path: str) -> None:
        """End or reject the call at `call_path`.

        Raises:
            TelephonyError: the call is unknown or the phone rejected the request.
        """
        await self._call_on_object(call_path, CALL_INTERFACE, "Hangup")

    async def hangup_all(self, gateway_path: str) -> None:
        """End every call on the gateway at `gateway_path`.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the request.
        """
        await self._call_on_object(gateway_path, AUDIO_GATEWAY_INTERFACE, "HangupAll")

    async def swap_calls(self, gateway_path: str) -> None:
        """Put the active call on hold and resume a held one, if any.

        With a single active call this places it on hold; calling again resumes it.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the request.
        """
        await self._call_on_object(gateway_path, AUDIO_GATEWAY_INTERFACE, "SwapCalls")

    async def hold_and_answer(self, gateway_path: str) -> None:
        """Put the active call on hold and answer the waiting one.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the request.
        """
        await self._call_on_object(gateway_path, AUDIO_GATEWAY_INTERFACE, "HoldAndAnswer")

    async def dial(self, gateway_path: str, number: str) -> None:
        """Place a call to `number` from the gateway at `gateway_path`.

        Args:
            gateway_path: Object path of the gateway to dial from.
            number: Digits with optional leading `+`; no formatting characters.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the number.
        """
        await self._call_on_object(gateway_path, AUDIO_GATEWAY_INTERFACE, "Dial", "s", [number])

    async def send_tones(self, gateway_path: str, tones: str) -> None:
        """Send DTMF `tones` (digits, `*`, `#`) on the gateway's active call.

        Raises:
            TelephonyError: the gateway is unknown or there is no active call.
        """
        await self._call_on_object(gateway_path, AUDIO_GATEWAY_INTERFACE, "SendTones", "s", [tones])

    async def set_speaker_volume(self, gateway_path: str, level: int) -> None:
        """Set the phone-side speaker gain, clamped to the HFP range.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the value.
        """
        clamped_level = _clamp_volume(level)
        await self._set_gateway_property(
            gateway_path, AUDIO_GATEWAY_INTERFACE, "SpeakerVolume", Variant("y", clamped_level)
        )

    async def set_microphone_volume(self, gateway_path: str, level: int) -> None:
        """Set the phone-side microphone gain, clamped to the HFP range.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the value.
        """
        clamped_level = _clamp_volume(level)
        await self._set_gateway_property(
            gateway_path, AUDIO_GATEWAY_INTERFACE, "MicrophoneVolume", Variant("y", clamped_level)
        )

    async def set_audio_on_computer(self, gateway_path: str, audio_on_computer: bool) -> None:
        """Choose whether call audio is routed to this computer or stays on the phone.

        Routing to the computer clears `RejectSCO` and, when a call is already up, asks
        the phone to open the audio link. Keeping audio on the phone sets `RejectSCO`.

        Raises:
            TelephonyError: the gateway is unknown or the phone rejected the request.
        """
        reject_audio_link = not audio_on_computer
        await self._set_gateway_property(
            gateway_path, TRANSPORT_INTERFACE, "RejectSCO", Variant("b", reject_audio_link)
        )

        gateway_has_calls = any(call.gateway_path == gateway_path for call in self._calls.values())
        should_activate_link = audio_on_computer and gateway_has_calls
        if should_activate_link:
            await self._call_on_object(gateway_path, TRANSPORT_INTERFACE, "Activate")

    async def _synchronise(self) -> None:
        """Replace the known objects with the service's current object tree."""
        try:
            reply_body = await call_method(
                self._bus,
                TELEPHONY_BUS_NAME,
                TELEPHONY_ROOT_PATH,
                OBJECT_MANAGER_INTERFACE,
                "GetManagedObjects",
            )
        except DBusRequestError as request_error:
            logger.warning("could not list telephony objects: %s", request_error)
            self._set_available(False)
            return

        managed_objects = unwrap_variant(reply_body[0])
        self._gateways.clear()
        self._calls.clear()

        # Gateways are registered before calls so every call can name a known gateway.
        for object_path, interfaces in managed_objects.items():
            if AUDIO_GATEWAY_INTERFACE in interfaces:
                self._gateways[object_path] = _build_gateway(object_path, interfaces)

        for object_path, interfaces in managed_objects.items():
            if CALL_INTERFACE in interfaces:
                self._calls[object_path] = _build_call(object_path, interfaces)

        gateway_count = len(self._gateways)
        call_count = len(self._calls)
        logger.info("telephony service found: %d gateway(s), %d call(s)", gateway_count, call_count)

        self._set_available(True)
        self.gateways_changed.emit()
        for call in self._calls.values():
            self.call_added.emit(call)

    def _handle_message(self, message: Message) -> None:
        """Dispatch bus signals about the telephony service to the matching handler.

        Registered with dbus-fast as a raw message handler; returns `None` so other
        handlers keep seeing the message.
        """
        if message.message_type != MessageType.SIGNAL:
            return None

        is_name_owner_signal = (
            message.interface == DBUS_DAEMON_INTERFACE and message.member == "NameOwnerChanged"
        )
        if is_name_owner_signal:
            self._on_name_owner_changed(message.body)
            return None

        is_telephony_object = message.path is not None and message.path.startswith(
            TELEPHONY_ROOT_PATH
        )
        if not is_telephony_object:
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
        """React to the telephony service appearing on or leaving the bus."""
        changed_name = signal_body[0]
        new_owner = signal_body[2]
        if changed_name != TELEPHONY_BUS_NAME:
            return

        service_left = new_owner == ""
        if service_left:
            logger.warning("telephony service left the bus")
            removed_call_paths = list(self._calls.keys())
            self._gateways.clear()
            self._calls.clear()
            self._set_available(False)
            for call_path in removed_call_paths:
                self.call_removed.emit(call_path)
            self.gateways_changed.emit()
        else:
            logger.info("telephony service appeared on the bus")
            asyncio.get_event_loop().create_task(self._synchronise())

    def _on_interfaces_added(self, signal_body: list[Any]) -> None:
        """Register a new gateway or call announced by the object manager."""
        object_path = signal_body[0]
        interfaces = unwrap_variant(signal_body[1])

        if AUDIO_GATEWAY_INTERFACE in interfaces:
            self._gateways[object_path] = _build_gateway(object_path, interfaces)
            logger.info("gateway added")
            self.gateways_changed.emit()

        if CALL_INTERFACE in interfaces:
            new_call = _build_call(object_path, interfaces)
            self._calls[object_path] = new_call
            logger.info("call added in state %s", new_call.state)
            self.call_added.emit(new_call)

    def _on_interfaces_removed(self, signal_body: list[Any]) -> None:
        """Forget a gateway or call withdrawn by the object manager."""
        object_path = signal_body[0]
        removed_interfaces = signal_body[1]

        gateway_was_removed = AUDIO_GATEWAY_INTERFACE in removed_interfaces
        if gateway_was_removed and object_path in self._gateways:
            del self._gateways[object_path]
            logger.info("gateway removed")
            self.gateways_changed.emit()

        call_was_removed = CALL_INTERFACE in removed_interfaces
        if call_was_removed and object_path in self._calls:
            del self._calls[object_path]
            logger.info("call removed")
            self.call_removed.emit(object_path)

    def _on_properties_changed(self, object_path: str, signal_body: list[Any]) -> None:
        """Apply a property change to the gateway or call at `object_path`."""
        changed_interface = signal_body[0]
        changed_properties = unwrap_variant(signal_body[1])

        if object_path in self._gateways:
            field_by_property = GATEWAY_FIELD_BY_PROPERTY
            if changed_interface == TRANSPORT_INTERFACE:
                field_by_property = TRANSPORT_FIELD_BY_PROPERTY
            field_updates = _field_updates(changed_properties, field_by_property)

            if field_updates:
                current_gateway = self._gateways[object_path]
                self._gateways[object_path] = replace(current_gateway, **field_updates)
                self.gateways_changed.emit()
            return

        if object_path in self._calls and changed_interface == CALL_INTERFACE:
            field_updates = _field_updates(changed_properties, CALL_FIELD_BY_PROPERTY)

            if field_updates:
                current_call = self._calls[object_path]
                updated_call = replace(current_call, **field_updates)
                self._calls[object_path] = updated_call
                logger.info("call state is now %s", updated_call.state)
                self.call_changed.emit(updated_call)

    def _set_available(self, is_available: bool) -> None:
        """Record availability and notify listeners only when it actually changes."""
        availability_changed = is_available != self._is_available
        self._is_available = is_available

        if availability_changed:
            self.availability_changed.emit(is_available)

    async def _call_on_object(
        self,
        object_path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> None:
        """Invoke `member` on a known gateway or call, translating failures.

        Raises:
            TelephonyError: the object is unknown or the service answered with an error.
        """
        is_known_object = object_path in self._gateways or object_path in self._calls
        if not is_known_object:
            raise TelephonyError(f"unknown telephony object {object_path}")

        try:
            await call_method(
                self._bus, TELEPHONY_BUS_NAME, object_path, interface, member, signature, body
            )
        except DBusRequestError as request_error:
            raise TelephonyError(str(request_error)) from request_error

    async def _set_gateway_property(
        self, gateway_path: str, interface: str, property_name: str, value: Variant
    ) -> None:
        """Set a property on a known gateway, read it back and update the local copy.

        Raises:
            TelephonyError: the gateway is unknown or the service rejected the value.
        """
        if gateway_path not in self._gateways:
            raise TelephonyError(f"unknown gateway {gateway_path}")

        try:
            await set_property(
                self._bus, TELEPHONY_BUS_NAME, gateway_path, interface, property_name, value
            )
            reply_body = await call_method(
                self._bus,
                TELEPHONY_BUS_NAME,
                gateway_path,
                PROPERTIES_INTERFACE,
                "Get",
                "ss",
                [interface, property_name],
            )
        except DBusRequestError as request_error:
            raise TelephonyError(str(request_error)) from request_error

        # The service does not emit PropertiesChanged for every property it lets us set
        # (RejectSCO is silent, SpeakerVolume is not), so the value is read back and
        # applied locally as if the signal had arrived.
        applied_value = unwrap_variant(reply_body[0])
        self._on_properties_changed(gateway_path, [interface, {property_name: applied_value}, []])


def _clamp_volume(level: int) -> int:
    """Return `level` limited to the HFP gain range."""
    lower_bounded_level = max(MIN_VOLUME_LEVEL, level)
    clamped_level = min(MAX_VOLUME_LEVEL, lower_bounded_level)

    return clamped_level


def _field_updates(
    changed_properties: dict[str, Any], field_by_property: dict[str, str]
) -> dict[str, Any]:
    """Translate changed D-Bus properties into dataclass field assignments.

    Properties without a mapping are ignored.
    """
    field_updates: dict[str, Any] = {}

    for property_name, property_value in changed_properties.items():
        field_name = field_by_property.get(property_name)
        if field_name is not None:
            field_updates[field_name] = property_value

    return field_updates


def _build_gateway(object_path: str, interfaces: dict[str, dict[str, Any]]) -> AudioGateway:
    """Create an `AudioGateway` from an object manager entry (variants already unwrapped)."""
    gateway_properties = interfaces.get(AUDIO_GATEWAY_INTERFACE, {})
    transport_properties = interfaces.get(TRANSPORT_INTERFACE, {})

    address = gateway_properties.get("Address", "")
    speaker_volume = gateway_properties.get("SpeakerVolume", MAX_VOLUME_LEVEL)
    microphone_volume = gateway_properties.get("MicrophoneVolume", MAX_VOLUME_LEVEL)
    transport_state = transport_properties.get("State", "")
    codec = transport_properties.get("Codec", 0)
    reject_sco = transport_properties.get("RejectSCO", False)

    return AudioGateway(
        path=object_path,
        address=address,
        speaker_volume=int(speaker_volume),
        microphone_volume=int(microphone_volume),
        transport_state=str(transport_state),
        codec=int(codec),
        reject_sco=bool(reject_sco),
    )


def _build_call(object_path: str, interfaces: dict[str, dict[str, Any]]) -> Call:
    """Create a `Call` from an object manager entry (variants already unwrapped).

    The gateway path is the call path's parent, which is how the service nests them.
    """
    call_properties = interfaces.get(CALL_INTERFACE, {})
    gateway_path = object_path.rsplit("/", 1)[0]

    state = call_properties.get("State", "")
    line_identification = call_properties.get("LineIdentification", "")
    name = call_properties.get("Name", "")
    incoming_line = call_properties.get("IncomingLine", "")
    multiparty = call_properties.get("Multiparty", False)

    return Call(
        path=object_path,
        gateway_path=gateway_path,
        state=str(state),
        line_identification=str(line_identification),
        name=str(name),
        incoming_line=str(incoming_line),
        multiparty=bool(multiparty),
    )
