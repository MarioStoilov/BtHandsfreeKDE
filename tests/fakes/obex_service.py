"""Fake `org.bluez.obex`: PBAP and MAP sessions backed by test-provided vCards and messages.

Transfers complete asynchronously a moment after the request, the way obexd's do, so the
clients' transfer tracking is exercised for real. The fake can refuse connections, forbid
phonebook access, push a new message into an open MAP session and drop a session.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbus_fast import DBusError, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from bt_handsfree_kde.dbus.obex import (
    MAP_TARGET,
    MESSAGE_ACCESS_INTERFACE,
    MESSAGE_INTERFACE,
    OBEX_BUS_NAME,
    OBEX_CLIENT_INTERFACE,
    OBEX_CLIENT_PATH,
    OBEX_FAILED_ERROR,
    OBEX_FORBIDDEN_ERROR,
    PBAP_TARGET,
    PHONEBOOK_ACCESS_INTERFACE,
    SESSION_INTERFACE,
    TRANSFER_COMPLETE,
    TRANSFER_INTERFACE,
)

# Seconds between a transfer request and its completion signal.
TRANSFER_DELAY_S = 0.05
# Initial status obexd reports for a transfer in its reply.
TRANSFER_QUEUED = "queued"
# D-Bus signatures of the `Message1` properties the fake serves.
MESSAGE_PROPERTY_SIGNATURES = {
    "Folder": "s",
    "Subject": "s",
    "Timestamp": "s",
    "Sender": "s",
    "SenderAddress": "s",
    "Recipient": "s",
    "RecipientAddress": "s",
    "Type": "s",
    "Size": "t",
    "Text": "b",
    "Status": "s",
    "Priority": "b",
    "Read": "b",
    "Sent": "b",
    "Protected": "b",
}


@dataclass
class FakeMessage:
    """One message the fake phone holds: listing properties plus its bMessage body."""

    handle: str
    properties: dict[str, Any]
    bmessage_text: str


@dataclass
class ObexRecords:
    """Everything the fake observed, for assertions."""

    created_sessions: list[tuple[str, str]] = field(default_factory=list)
    removed_sessions: list[str] = field(default_factory=list)
    # ("created" | "removed", destination, target) in the order they happened, for
    # checking that sessions of different phones do not overlap.
    session_events: list[tuple[str, str, str]] = field(default_factory=list)
    pull_filters: list[dict[str, Any]] = field(default_factory=list)
    selected_phonebooks: list[tuple[str, str]] = field(default_factory=list)
    set_folders: list[str] = field(default_factory=list)
    listed_folders: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    read_flags_set: list[tuple[str, bool]] = field(default_factory=list)
    fetched_message_paths: list[str] = field(default_factory=list)


class FakeTransfer(ServiceInterface):
    """`Transfer1` that reports completion after a short delay."""

    def __init__(self, session_path: str) -> None:
        """Create the transfer in the queued state."""
        super().__init__(TRANSFER_INTERFACE)
        self._session_path = session_path
        self._status = TRANSFER_QUEUED

    def complete(self) -> None:
        """Announce completion."""
        self._status = TRANSFER_COMPLETE
        self.emit_properties_changed({"Status": TRANSFER_COMPLETE, "Transferred": 1})

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def status(self) -> "s":
        """Transfer status."""
        return self._status

    @dbus_property(access=PropertyAccess.READ, name="Session")
    def session(self) -> "o":
        """Owning session."""
        return self._session_path


class FakeSession(ServiceInterface):
    """`Session1` of one fake session."""

    def __init__(self, destination: str, target: str) -> None:
        """Create the session interface."""
        super().__init__(SESSION_INTERFACE)
        self._destination = destination
        self._target = target

    @dbus_property(access=PropertyAccess.READ, name="Destination")
    def destination(self) -> "s":
        """Phone address."""
        return self._destination

    @dbus_property(access=PropertyAccess.READ, name="Target")
    def target(self) -> "s":
        """Profile target."""
        return self._target


class FakePhonebookAccess(ServiceInterface):
    """`PhonebookAccess1` serving the fake's vCard text."""

    def __init__(self, service: "FakeObexService", session_path: str) -> None:
        """Create the interface bound to its session."""
        super().__init__(PHONEBOOK_ACCESS_INTERFACE)
        self._service = service
        self._session_path = session_path

    @method(name="Select")
    def select(self, location: "s", phonebook: "s") -> None:
        """Record the selection, or refuse when the fake forbids phonebook access."""
        if self._service.forbid_phonebook:
            raise DBusError(OBEX_FORBIDDEN_ERROR, "Forbidden")

        self._service.records.selected_phonebooks.append((location, phonebook))

    @method(name="PullAll")
    def pull_all(self, targetfile: "s", filters: "a{sv}") -> "oa{sv}":
        """Write the vCard text to `targetfile` and start a transfer that completes shortly."""
        plain_filters: dict[str, Any] = {}
        for filter_name, filter_value in filters.items():
            plain_filters[filter_name] = filter_value.value
        self._service.records.pull_filters.append(plain_filters)

        Path(targetfile).write_text(self._service.phonebook_vcard, encoding="utf-8")
        transfer_path = self._service.start_transfer(self._session_path)

        return [transfer_path, {"Status": Variant("s", TRANSFER_QUEUED)}]


class FakeMessageAccess(ServiceInterface):
    """`MessageAccess1` listing the fake's messages per folder."""

    def __init__(self, service: "FakeObexService", session_path: str) -> None:
        """Create the interface bound to its session."""
        super().__init__(MESSAGE_ACCESS_INTERFACE)
        self._service = service
        self._session_path = session_path

    @method(name="SetFolder")
    def set_folder(self, name: "s") -> None:
        """Record the folder change."""
        self._service.records.set_folders.append(name)

    @method(name="ListMessages")
    def list_messages(self, folder: "s", filters: "a{sv}") -> "a{oa{sv}}":
        """Export the folder's messages as objects and return their properties."""
        plain_filters: dict[str, Any] = {}
        for filter_name, filter_value in filters.items():
            plain_filters[filter_name] = filter_value.value
        self._service.records.listed_folders.append((folder, plain_filters))

        listing: dict[str, dict[str, Variant]] = {}
        for fake_message in self._service.messages_in(folder):
            message_path = self._service.export_message(self._session_path, fake_message)
            listing[message_path] = _variant_properties(fake_message.properties)

        return listing

    @dbus_property(access=PropertyAccess.READ, name="SupportedTypes")
    def supported_types(self) -> "as":
        """Message types the fake phone supports."""
        return ["SMS_GSM"]


class FakeMessageObject(ServiceInterface):
    """`Message1` of one fake message."""

    def __init__(
        self, service: "FakeObexService", session_path: str, fake_message: FakeMessage
    ) -> None:
        """Create the message interface."""
        super().__init__(MESSAGE_INTERFACE)
        self._service = service
        self._session_path = session_path
        self._fake_message = fake_message
        self.object_path = ""

    @method(name="Get")
    def get(self, targetfile: "s", attachment: "b") -> "oa{sv}":
        """Write the bMessage to `targetfile` and start a transfer that completes shortly."""
        self._service.records.fetched_message_paths.append(self.object_path)
        Path(targetfile).write_text(self._fake_message.bmessage_text, encoding="utf-8")
        transfer_path = self._service.start_transfer(self._session_path)

        return [transfer_path, {"Status": Variant("s", TRANSFER_QUEUED)}]

    @dbus_property(access=PropertyAccess.READWRITE, name="Read")
    def read(self) -> "b":
        """Read flag."""
        return bool(self._fake_message.properties.get("Read", False))

    @read.setter
    def read(self, value: "b") -> None:
        """Store the flag and record the write."""
        self._fake_message.properties["Read"] = value
        self._service.records.read_flags_set.append((self.object_path, value))

    @dbus_property(access=PropertyAccess.READ, name="Folder")
    def folder(self) -> "s":
        """Folder of the message."""
        return str(self._fake_message.properties.get("Folder", ""))

    @dbus_property(access=PropertyAccess.READ, name="Subject")
    def subject(self) -> "s":
        """Subject (the text preview for SMS)."""
        return str(self._fake_message.properties.get("Subject", ""))


class FakeObexClient(ServiceInterface):
    """`Client1`: session creation and removal."""

    def __init__(self, service: "FakeObexService") -> None:
        """Create the client interface."""
        super().__init__(OBEX_CLIENT_INTERFACE)
        self._service = service

    @method(name="CreateSession")
    def create_session(self, destination: "s", args: "a{sv}") -> "o":
        """Create a PBAP or MAP session, or refuse when the fake is set to."""
        if self._service.refuse_connections:
            raise DBusError(OBEX_FAILED_ERROR, "Unable to find service record")

        target = str(args["Target"].value)

        return self._service.create_session(destination, target)

    @method(name="RemoveSession")
    def remove_session(self, session: "o") -> None:
        """Remove the session and everything below it."""
        self._service.records.removed_sessions.append(session)
        self._service.drop_session(session)


class FakeObexService:
    """Owns `org.bluez.obex` on the test bus and holds the phone's fake data."""

    def __init__(self, bus: MessageBus) -> None:
        """Create the service; set the data attributes before the clients connect."""
        self._bus = bus
        self._next_session_number = 0
        self._next_transfer_number = 0
        self._child_paths_by_session: dict[str, list[str]] = {}
        self._destination_and_target_by_session: dict[str, tuple[str, str]] = {}
        self._message_path_by_handle: dict[tuple[str, str], str] = {}
        self._map_session_paths: list[str] = []
        self._is_client_exported = False
        # Data the fake phone serves.
        self.phonebook_vcard = ""
        self.messages_by_folder: dict[str, list[FakeMessage]] = {"inbox": [], "sent": []}
        # Failure switches.
        self.refuse_connections = False
        self.forbid_phonebook = False
        self.records = ObexRecords()

    async def start(self) -> None:
        """Export the client object (once) and claim `org.bluez.obex`.

        After a `stop`, starting again re-claims the name with no sessions, like a
        restarted obexd.
        """
        if not self._is_client_exported:
            self._bus.export(OBEX_CLIENT_PATH, FakeObexClient(self))
            self._is_client_exported = True

        await self._bus.request_name(OBEX_BUS_NAME)

    async def stop(self) -> None:
        """Leave the bus the way a crashing obexd does: the name goes, no object signals.

        The name is released first, so the removal signals of the sessions withdrawn
        afterwards no longer carry the obexd sender and never reach the client.
        """
        await self._bus.release_name(OBEX_BUS_NAME)

        for session_path in list(self._child_paths_by_session):
            self.drop_session(session_path)

    @property
    def open_map_session_paths(self) -> list[str]:
        """Return the MAP sessions currently open."""
        return list(self._map_session_paths)

    def messages_in(self, folder: str) -> list[FakeMessage]:
        """Return the fake messages of `folder`."""
        return list(self.messages_by_folder.get(folder, []))

    def create_session(self, destination: str, target: str) -> str:
        """Export a session with the interface `target` calls for and return its path."""
        session_path = f"{OBEX_CLIENT_PATH}/client/session{self._next_session_number}"
        self._next_session_number += 1
        self.records.created_sessions.append((destination, target))
        self.records.session_events.append(("created", destination, target))
        self._child_paths_by_session[session_path] = []
        self._destination_and_target_by_session[session_path] = (destination, target)

        self._bus.export(session_path, FakeSession(destination, target))
        if target == PBAP_TARGET:
            self._bus.export(session_path, FakePhonebookAccess(self, session_path))
        elif target == MAP_TARGET:
            self._bus.export(session_path, FakeMessageAccess(self, session_path))
            self._map_session_paths.append(session_path)

        return session_path

    def drop_session(self, session_path: str) -> None:
        """Withdraw a session and its children without being asked, or on `RemoveSession`."""
        child_paths = self._child_paths_by_session.pop(session_path, [])
        for child_path in child_paths:
            self._bus.unexport(child_path)
        session_identity = self._destination_and_target_by_session.pop(session_path, None)
        if session_identity is not None:
            destination, target = session_identity
            self.records.session_events.append(("removed", destination, target))
        for handle_key in list(self._message_path_by_handle):
            if handle_key[0] == session_path:
                del self._message_path_by_handle[handle_key]
        if session_path in self._map_session_paths:
            self._map_session_paths.remove(session_path)

        self._bus.unexport(session_path)

    def start_transfer(self, session_path: str) -> str:
        """Export a transfer below `session_path` that completes after `TRANSFER_DELAY_S`."""
        transfer_path = f"{session_path}/transfer{self._next_transfer_number}"
        self._next_transfer_number += 1

        transfer = FakeTransfer(session_path)
        self._bus.export(transfer_path, transfer)
        self._child_paths_by_session[session_path].append(transfer_path)

        event_loop = asyncio.get_event_loop()
        event_loop.create_task(self._complete_transfer(transfer_path, transfer, session_path))

        return transfer_path

    async def _complete_transfer(
        self, transfer_path: str, transfer: FakeTransfer, session_path: str
    ) -> None:
        """Announce completion, then remove the transfer object like obexd does."""
        await asyncio.sleep(TRANSFER_DELAY_S)
        transfer.complete()

        still_exported = transfer_path in self._child_paths_by_session.get(session_path, [])
        if still_exported:
            self._child_paths_by_session[session_path].remove(transfer_path)
            self._bus.unexport(transfer_path)

    def export_message(self, session_path: str, fake_message: FakeMessage) -> str:
        """Export a `Message1` for `fake_message` below `session_path`, once per handle."""
        handle_key = (session_path, fake_message.handle)
        known_path = self._message_path_by_handle.get(handle_key)
        if known_path is not None:
            return known_path

        message_path = f"{session_path}/message{fake_message.handle}"
        message_object = FakeMessageObject(self, session_path, fake_message)
        message_object.object_path = message_path
        self._bus.export(message_path, message_object)
        self._child_paths_by_session[session_path].append(message_path)
        self._message_path_by_handle[handle_key] = message_path

        return message_path

    def push_message(self, fake_message: FakeMessage) -> str:
        """Announce a new message in the open MAP session, as obexd does on a phone event."""
        session_path = self._map_session_paths[-1]
        self.messages_by_folder.setdefault("inbox", []).append(fake_message)

        return self.export_message(session_path, fake_message)

    def remove_message(self, session_path: str, handle: str) -> None:
        """Withdraw a message object, as obexd does when the phone deletes a message."""
        message_path = self._message_path_by_handle.pop((session_path, handle))
        self._child_paths_by_session[session_path].remove(message_path)
        self._bus.unexport(message_path)


def _variant_properties(properties: dict[str, Any]) -> dict[str, Variant]:
    """Wrap listing properties in variants with the signatures obexd uses."""
    variant_properties: dict[str, Variant] = {}

    for property_name, property_value in properties.items():
        signature = MESSAGE_PROPERTY_SIGNATURES[property_name]
        variant_properties[property_name] = Variant(signature, property_value)

    return variant_properties
