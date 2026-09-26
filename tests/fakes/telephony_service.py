"""Fake `org.pipewire.Telephony` service: gateways and calls the tests create and drive."""

from typing import Any

from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from bt_handsfree_kde.dbus.telephony import (
    AUDIO_GATEWAY_INTERFACE,
    CALL_INTERFACE,
    TELEPHONY_BUS_NAME,
    TELEPHONY_ROOT_PATH,
    TRANSPORT_INTERFACE,
)

# Interface exported on the root object so dbus-fast serves an object manager there; the
# real service exposes nothing but the object manager at that path.
ROOT_ANCHOR_INTERFACE = "org.pipewire.Telephony.Root1"
# Transport codec value while no audio link is open.
NO_CODEC = 0
# Initial HFP gain of a new fake gateway.
INITIAL_VOLUME = 15


class RootAnchorInterface(ServiceInterface):
    """Empty interface that anchors the object manager on the root path."""

    def __init__(self) -> None:
        """Create the anchor."""
        super().__init__(ROOT_ANCHOR_INTERFACE)


class FakeAudioGateway(ServiceInterface):
    """`AudioGateway1` of one fake phone; records every method call in the service log."""

    def __init__(self, address: str, request_log: list[tuple[str, str, tuple[Any, ...]]]) -> None:
        """Create the gateway interface.

        Args:
            address: Bluetooth address reported by the `Address` property.
            request_log: Shared list receiving (object path, member, arguments) entries.
        """
        super().__init__(AUDIO_GATEWAY_INTERFACE)
        self.object_path = ""
        self._address = address
        self._speaker_volume = INITIAL_VOLUME
        self._microphone_volume = INITIAL_VOLUME
        self._request_log = request_log

    def _log(self, member: str, *arguments: Any) -> None:
        """Append a request to the shared log."""
        self._request_log.append((self.object_path, member, arguments))

    @method(name="Dial")
    def dial(self, number: "s") -> None:
        """Record a dial request."""
        self._log("Dial", number)

    @method(name="HangupAll")
    def hangup_all(self) -> None:
        """Record a hang-up-all request."""
        self._log("HangupAll")

    @method(name="SendTones")
    def send_tones(self, tones: "s") -> None:
        """Record DTMF tones."""
        self._log("SendTones", tones)

    @method(name="SwapCalls")
    def swap_calls(self) -> None:
        """Record a swap request."""
        self._log("SwapCalls")

    @method(name="HoldAndAnswer")
    def hold_and_answer(self) -> None:
        """Record a hold-and-answer request."""
        self._log("HoldAndAnswer")

    @dbus_property(access=PropertyAccess.READ, name="Address")
    def address(self) -> "s":
        """Bluetooth address of the phone."""
        return self._address

    @dbus_property(access=PropertyAccess.READWRITE, name="SpeakerVolume")
    def speaker_volume(self) -> "y":
        """Speaker gain; changes are announced like PipeWire does."""
        return self._speaker_volume

    @speaker_volume.setter
    def speaker_volume(self, level: "y") -> None:
        """Store the gain and emit `PropertiesChanged`."""
        self._speaker_volume = level
        self.emit_properties_changed({"SpeakerVolume": level})

    @dbus_property(access=PropertyAccess.READWRITE, name="MicrophoneVolume")
    def microphone_volume(self) -> "y":
        """Microphone gain; changes are announced like PipeWire does."""
        return self._microphone_volume

    @microphone_volume.setter
    def microphone_volume(self, level: "y") -> None:
        """Store the gain and emit `PropertiesChanged`."""
        self._microphone_volume = level
        self.emit_properties_changed({"MicrophoneVolume": level})


class FakeTransport(ServiceInterface):
    """`AudioGatewayTransport1` of one fake phone; `RejectSCO` changes silently like PipeWire."""

    def __init__(self, request_log: list[tuple[str, str, tuple[Any, ...]]]) -> None:
        """Create the transport interface."""
        super().__init__(TRANSPORT_INTERFACE)
        self.object_path = ""
        self._reject_sco = False
        self._request_log = request_log

    @method(name="Activate")
    def activate(self) -> None:
        """Record an audio link activation."""
        self._request_log.append((self.object_path, "Activate", ()))

    @dbus_property(access=PropertyAccess.READ, name="State")
    def state(self) -> "s":
        """Audio link state."""
        return "idle"

    @dbus_property(access=PropertyAccess.READ, name="Codec")
    def codec(self) -> "y":
        """Negotiated codec; none."""
        return NO_CODEC

    @dbus_property(access=PropertyAccess.READWRITE, name="RejectSCO")
    def reject_sco(self) -> "b":
        """Whether audio links are refused."""
        return self._reject_sco

    @reject_sco.setter
    def reject_sco(self, value: "b") -> None:
        """Store the flag without announcing it, as PipeWire does."""
        self._reject_sco = value


class FakeCall(ServiceInterface):
    """`Call1` of one fake call; the test changes its state through `set_state`."""

    def __init__(
        self,
        state: str,
        line_identification: str,
        name: str,
        request_log: list[tuple[str, str, tuple[Any, ...]]],
    ) -> None:
        """Create the call interface with its initial properties."""
        super().__init__(CALL_INTERFACE)
        self.object_path = ""
        self._state = state
        self._line_identification = line_identification
        self._name = name
        self._request_log = request_log

    def set_state(self, state: str) -> None:
        """Change the state and announce it."""
        self._state = state
        self.emit_properties_changed({"State": state})

    @method(name="Answer")
    def answer(self) -> None:
        """Record an answer request."""
        self._request_log.append((self.object_path, "Answer", ()))

    @method(name="Hangup")
    def hangup(self) -> None:
        """Record a hang-up request."""
        self._request_log.append((self.object_path, "Hangup", ()))

    @dbus_property(access=PropertyAccess.READ, name="State")
    def state(self) -> "s":
        """Call state."""
        return self._state

    @dbus_property(access=PropertyAccess.READ, name="LineIdentification")
    def line_identification(self) -> "s":
        """Caller or callee number."""
        return self._line_identification

    # The Python attribute is not called `name`: `ServiceInterface.name` holds the D-Bus
    # interface name, and a property getter of that name would shadow it.
    @dbus_property(access=PropertyAccess.READ, name="Name")
    def caller_name(self) -> "s":
        """Caller name as sent by the phone."""
        return self._name

    @dbus_property(access=PropertyAccess.READ, name="IncomingLine")
    def incoming_line(self) -> "s":
        """Line the call arrived on."""
        return ""

    @dbus_property(access=PropertyAccess.READ, name="Multiparty")
    def multiparty(self) -> "b":
        """Whether the call is a conference."""
        return False


class FakeTelephonyService:
    """Owns the telephony bus name and exports gateways and calls on demand."""

    def __init__(self, bus: MessageBus) -> None:
        """Create the service on `bus`; `start` claims the name."""
        self._bus = bus
        self._next_gateway_number = 1
        self._next_call_number = 0
        self._gateways_by_path: dict[str, tuple[FakeAudioGateway, FakeTransport]] = {}
        self._calls_by_path: dict[str, FakeCall] = {}
        self._is_root_exported = False
        # (object path, member, arguments) for every method call the clients made.
        self.request_log: list[tuple[str, str, tuple[Any, ...]]] = []

    async def start(self) -> None:
        """Export the root object (once) and claim `org.pipewire.Telephony`.

        After a `stop`, starting again re-claims the name while the gateways and calls
        stay exported, like a restarted WirePlumber that found the same phone connected.
        """
        if not self._is_root_exported:
            self._bus.export(TELEPHONY_ROOT_PATH, RootAnchorInterface())
            self._is_root_exported = True

        await self._bus.request_name(TELEPHONY_BUS_NAME)

    async def stop(self) -> None:
        """Give the name up, as a stopping WirePlumber would; the objects stay exported."""
        await self._bus.release_name(TELEPHONY_BUS_NAME)

    def add_gateway(self, address: str) -> str:
        """Export a new gateway for the phone at `address` and return its path."""
        gateway_path = f"{TELEPHONY_ROOT_PATH}/ag{self._next_gateway_number}"
        self._next_gateway_number += 1

        gateway = FakeAudioGateway(address, self.request_log)
        gateway.object_path = gateway_path
        transport = FakeTransport(self.request_log)
        transport.object_path = gateway_path
        self._bus.export(gateway_path, gateway)
        self._bus.export(gateway_path, transport)
        self._gateways_by_path[gateway_path] = (gateway, transport)

        return gateway_path

    def remove_gateway(self, gateway_path: str) -> None:
        """Withdraw a gateway after every call on it, as PipeWire does on disconnect."""
        for call_path in list(self._calls_by_path):
            if call_path.startswith(gateway_path + "/"):
                self.remove_call(call_path)

        self.remove_gateway_only(gateway_path)

    def remove_gateway_only(self, gateway_path: str) -> None:
        """Withdraw a gateway object while its call objects stay exported.

        Exercises the client's own clean-up of calls whose gateway vanished first.
        """
        self._bus.unexport(gateway_path)
        del self._gateways_by_path[gateway_path]

    def add_call(
        self, gateway_path: str, state: str, line_identification: str = "", name: str = ""
    ) -> str:
        """Export a new call below `gateway_path` and return its path."""
        call_path = f"{gateway_path}/call{self._next_call_number}"
        self._next_call_number += 1

        call = FakeCall(state, line_identification, name, self.request_log)
        call.object_path = call_path
        self._bus.export(call_path, call)
        self._calls_by_path[call_path] = call

        return call_path

    def set_call_state(self, call_path: str, state: str) -> None:
        """Change a call's state, announced through `PropertiesChanged`."""
        self._calls_by_path[call_path].set_state(state)

    def remove_call(self, call_path: str) -> None:
        """Withdraw a call object."""
        self._bus.unexport(call_path)
        del self._calls_by_path[call_path]

    def reject_sco_of(self, gateway_path: str) -> bool:
        """Return the stored `RejectSCO` flag of a gateway."""
        _, transport = self._gateways_by_path[gateway_path]

        return transport.reject_sco

    def requests_named(self, member: str) -> list[tuple[str, str, tuple[Any, ...]]]:
        """Return the logged requests for `member`."""
        matching_requests: list[tuple[str, str, tuple[Any, ...]]] = []

        for request in self.request_log:
            if request[1] == member:
                matching_requests.append(request)

        return matching_requests
