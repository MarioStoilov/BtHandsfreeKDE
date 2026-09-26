"""Message sync through obexd's Message Access client.

Unlike the phonebook pull, the MAP session of a phone stays open for as long as the
phone is connected: creating it makes obexd register for the phone's message
notifications, which then arrive as `Message1` objects appearing below the session. On
connect the newest messages of the inbox and the sent folder are listed; a new message
is fetched in full when it arrives, a long one when its conversation is opened. Messages
live in memory for the session only.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from dbus_fast import Variant
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.dbus.helpers import unwrap_variant
from bt_handsfree_kde.dbus.obex import (
    MAP_TARGET,
    MESSAGE_ACCESS_INTERFACE,
    MESSAGE_INTERFACE,
    SESSION_INTERFACE,
    TRANSFER_COMPLETE,
    ObexClient,
    ObexError,
)
from bt_handsfree_kde.messages.bmessage import BMessage, parse_bmessage
from bt_handsfree_kde.messages.message import TextMessage, message_from_properties

logger = logging.getLogger(__name__)

# Path of the message store on the phone, entered one folder at a time as MAP requires.
MESSAGE_ROOT_FOLDERS = ("telecom", "msg")
# Folders listed on connect, relative to the message store.
INBOX_FOLDER = "inbox"
SENT_FOLDER = "sent"
LISTED_FOLDERS = (INBOX_FOLDER, SENT_FOLDER)
# How many of the newest messages are listed per folder on connect and on refresh
# (decision 2026-09-20).
MESSAGES_PER_FOLDER = 25
# Delay between a phone connecting over HFP and opening its message session, in
# seconds; it follows the phonebook pull so the phone is asked for one channel at a time.
AUTOMATIC_SYNC_DELAY_S = 5.0
# Name pattern of the file a fetched message is written to.
BMESSAGE_FILE_PREFIX = "message-"
BMESSAGE_FILE_SUFFIX = ".bmsg"
# Whether `Message1.Get` should include attachments; never, only the text is shown.
FETCH_ATTACHMENTS = False
# Sync states of one phone's messages.
SYNC_STATE_IDLE = "idle"
SYNC_STATE_SYNCING = "syncing"
SYNC_STATE_SYNCED = "synced"
SYNC_STATE_FAILED = "failed"
# User-facing explanations of the failures a user can do something about.
OBEXD_MISSING_TEXT = (
    "Messages need obexd, the Bluetooth file transfer service; it is missing. "
    "Install the bluez-obexd package and refresh."
)
CONNECTION_REFUSED_TEXT = (
    "The phone refused the messages connection. Allow message access for this computer "
    "in the phone's Bluetooth settings (on iPhone: Show Notifications), then refresh."
)
LISTING_FORBIDDEN_TEXT = (
    "The phone did not allow reading its messages. Check the message access permission "
    "for this computer on the phone, then refresh."
)
CONNECTION_LOST_TEXT = "The messages connection to the phone was lost. Refresh to reconnect."
LISTING_FAILED_TEXT = "The phone stopped sending its messages. Refresh to try again."


class MessagesError(Exception):
    """A message operation failed; the message is written for the user."""


@dataclass(frozen=True)
class MessagesState:
    """Where one phone's message sync stands, as shown in the Messages tab."""

    # Upper-case Bluetooth address of the phone.
    address: str
    # One of the `SYNC_STATE_*` values.
    sync_state: str
    # Messages known so far, unordered; kept while a refresh is running.
    messages: tuple[TextMessage, ...]
    # Local time of the last successful listing.
    synced_at: datetime | None
    # User-facing reason when `sync_state` is failed, else empty.
    error_text: str


class MessagesClient(QObject):
    """Keeps one open message session per connected phone and the messages read through it."""

    # Emitted with the phone's upper-case address whenever its state or messages change.
    messages_changed = Signal(str)
    # Emitted with (upper-case address, `TextMessage`) when the phone pushes a new
    # incoming message, after its text and sender have been fetched.
    message_received = Signal(str, object)

    def __init__(self, obex: ObexClient, parent: QObject | None = None) -> None:
        """Create a client that talks through the shared obexd client.

        Args:
            obex: Started obexd client shared with the contacts feature.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._obex = obex
        self._state_by_address: dict[str, MessagesState] = {}
        self._session_path_by_address: dict[str, str] = {}
        self._sync_task_by_address: dict[str, asyncio.Task[None]] = {}
        # Sessions with a listing in flight: message objects appearing meanwhile belong
        # to the listing reply, not to a push from the phone.
        self._listing_session_paths: set[str] = set()

        self._obex.object_added.connect(self._on_object_added)
        self._obex.object_removed.connect(self._on_object_removed)

    def state_for_address(self, address: str) -> MessagesState:
        """Return the sync state of the phone at `address`; idle when never synced."""
        address_key = address.upper()
        known_state = self._state_by_address.get(address_key)
        if known_state is not None:
            return known_state

        return MessagesState(address_key, SYNC_STATE_IDLE, (), None, "")

    def start_sync(self, address: str, delay_seconds: float = 0.0) -> None:
        """List the newest messages of the phone at `address` in the background.

        Opens the session first when the phone has none. A listing already running for
        that phone is left alone. The outcome is published through `messages_changed`;
        a failure is stored in the state, not raised.

        Args:
            address: Bluetooth address of the phone, any letter case.
            delay_seconds: Wait before connecting, for syncs triggered by a connect.
        """
        address_key = address.upper()
        is_already_syncing = address_key in self._sync_task_by_address
        if is_already_syncing:
            return

        previous_state = self.state_for_address(address_key)
        self._set_state(replace(previous_state, sync_state=SYNC_STATE_SYNCING, error_text=""))

        event_loop = asyncio.get_event_loop()
        sync_task = event_loop.create_task(self._sync(address_key, delay_seconds))
        self._sync_task_by_address[address_key] = sync_task

    def forget(self, address: str) -> None:
        """Drop the messages of the phone at `address`, stop its sync and close its session."""
        address_key = address.upper()

        running_task = self._sync_task_by_address.pop(address_key, None)
        if running_task is not None:
            running_task.cancel()

        session_path = self._session_path_by_address.pop(address_key, None)
        if session_path is not None:
            event_loop = asyncio.get_event_loop()
            event_loop.create_task(self._obex.remove_session(session_path))

        had_state = self._state_by_address.pop(address_key, None) is not None
        if had_state:
            self.messages_changed.emit(address_key)

    async def fetch_full_text(self, address: str, message_path: str) -> None:
        """Download the whole text of the message at `message_path` and store it.

        Raises:
            MessagesError: the phone refused or the transfer failed.
        """
        address_key = address.upper()
        message = self._message_at(address_key, message_path)
        if message is None or message.is_text_complete:
            return

        fetched = await self._get_bmessage(message_path)
        complete_message = replace(message, text=fetched.text, is_text_complete=True)
        self._replace_message(address_key, complete_message)

    async def mark_read(self, address: str, message_paths: list[str]) -> None:
        """Mark the messages at `message_paths` as read on the phone (decision 2026-09-20).

        A message the phone refuses to update is logged and left unread locally.
        """
        address_key = address.upper()

        for message_path in message_paths:
            message = self._message_at(address_key, message_path)
            if message is None or message.is_read:
                continue

            try:
                await self._obex.set_property(
                    message_path, MESSAGE_INTERFACE, "Read", Variant("b", True)
                )
            except ObexError as obex_error:
                logger.warning("marking a message as read failed: %s", obex_error)
                continue

            self._replace_message(address_key, replace(message, is_read=True))

    async def _sync(self, address_key: str, delay_seconds: float) -> None:
        """Open the session when needed, list the folders and store the outcome."""
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            session_path = await self._ensure_session(address_key)
            messages = await self._list_messages(session_path)
        except asyncio.CancelledError:
            raise
        except MessagesError as messages_error:
            logger.warning("message sync failed: %s", messages_error)
            self._finish_sync(address_key, None, str(messages_error))
            return
        except Exception as unexpected_error:
            logger.exception("message sync failed unexpectedly")
            self._finish_sync(address_key, None, str(unexpected_error))
            return

        message_count = len(messages)
        logger.info("messages synced: %d message(s)", message_count)
        self._finish_sync(address_key, messages, "")

    def _finish_sync(
        self, address_key: str, messages: list[TextMessage] | None, error_text: str
    ) -> None:
        """Record the outcome of a listing, keeping the previous messages on failure."""
        self._sync_task_by_address.pop(address_key, None)
        previous_state = self.state_for_address(address_key)

        if messages is not None:
            new_state = MessagesState(
                address_key, SYNC_STATE_SYNCED, tuple(messages), datetime.now(), ""
            )
        else:
            new_state = replace(previous_state, sync_state=SYNC_STATE_FAILED, error_text=error_text)

        self._set_state(new_state)

    def _set_state(self, new_state: MessagesState) -> None:
        """Store a state and announce it."""
        self._state_by_address[new_state.address] = new_state
        self.messages_changed.emit(new_state.address)

    async def _ensure_session(self, address_key: str) -> str:
        """Return the phone's open message session, creating and positioning a new one.

        Raises:
            MessagesError: obexd is missing or the phone refused the connection.
        """
        existing_path = self._session_path_by_address.get(address_key)
        is_still_open = existing_path is not None and self._obex.owns_session(existing_path)
        if is_still_open:
            return existing_path

        try:
            session_path = await self._obex.create_session(address_key, MAP_TARGET)
        except ObexError as obex_error:
            raise _messages_error(obex_error, CONNECTION_REFUSED_TEXT) from obex_error
        self._session_path_by_address[address_key] = session_path

        # MAP accepts one folder level per request, so the store is entered step by step.
        try:
            for folder_name in MESSAGE_ROOT_FOLDERS:
                await self._obex.call(
                    session_path, MESSAGE_ACCESS_INTERFACE, "SetFolder", "s", [folder_name]
                )
        except ObexError as obex_error:
            raise _messages_error(obex_error, LISTING_FORBIDDEN_TEXT) from obex_error

        return session_path

    async def _list_messages(self, session_path: str) -> list[TextMessage]:
        """List the newest messages of the inbox and the sent folder.

        Raises:
            MessagesError: the phone refused or the listing failed.
        """
        listing_filters = {"MaxCount": Variant("q", MESSAGES_PER_FOLDER)}
        listed_messages: list[TextMessage] = []

        self._listing_session_paths.add(session_path)
        try:
            for folder_name in LISTED_FOLDERS:
                reply_body = await self._obex.call(
                    session_path,
                    MESSAGE_ACCESS_INTERFACE,
                    "ListMessages",
                    "sa{sv}",
                    [folder_name, listing_filters],
                )
                properties_by_path = _unwrap_listing(reply_body)

                for message_path, properties in properties_by_path.items():
                    message = message_from_properties(message_path, properties)
                    if message is not None:
                        listed_messages.append(message)
        except ObexError as obex_error:
            raise _messages_error(obex_error, LISTING_FORBIDDEN_TEXT) from obex_error
        finally:
            self._listing_session_paths.discard(session_path)

        return listed_messages

    async def _get_bmessage(self, message_path: str) -> BMessage:
        """Download the message at `message_path` as bMessage, parse it and delete the file.

        Raises:
            MessagesError: the phone refused or the transfer failed.
        """
        target_file = self._obex.new_transfer_file(BMESSAGE_FILE_PREFIX, BMESSAGE_FILE_SUFFIX)

        try:
            reply_body = await self._obex.call(
                message_path,
                MESSAGE_INTERFACE,
                "Get",
                "sb",
                [str(target_file), FETCH_ATTACHMENTS],
            )
            final_status = await self._obex.wait_for_transfer(reply_body)
            if final_status != TRANSFER_COMPLETE:
                raise MessagesError(LISTING_FAILED_TEXT)
            bmessage_text = target_file.read_text(encoding="utf-8", errors="replace")
        except ObexError as obex_error:
            raise _messages_error(obex_error, LISTING_FORBIDDEN_TEXT) from obex_error
        finally:
            target_file.unlink(missing_ok=True)

        return parse_bmessage(bmessage_text)

    def _address_for_object(self, object_path: str) -> str | None:
        """Return the phone whose session `object_path` belongs to, or `None`."""
        for address_key, session_path in self._session_path_by_address.items():
            is_same_session = object_path == session_path
            is_child = object_path.startswith(session_path + "/")
            if is_same_session or is_child:
                return address_key

        return None

    def _on_object_added(self, object_path: str, interfaces: dict[str, Any]) -> None:
        """Treat a message object appearing outside a listing as a push from the phone."""
        if MESSAGE_INTERFACE not in interfaces:
            return

        address_key = self._address_for_object(object_path)
        if address_key is None:
            return

        session_path = self._session_path_by_address[address_key]
        is_listing_result = session_path in self._listing_session_paths
        if is_listing_result:
            return

        already_known = self._message_at(address_key, object_path) is not None
        if already_known:
            return

        properties = interfaces[MESSAGE_INTERFACE]
        property_names = sorted(properties)
        folder = properties.get("Folder", "")
        logger.info("message pushed by the phone in folder %r with %s", folder, property_names)
        event_loop = asyncio.get_event_loop()
        event_loop.create_task(self._receive_pushed_message(address_key, object_path, properties))

    async def _receive_pushed_message(
        self, address_key: str, message_path: str, properties: dict[str, Any]
    ) -> None:
        """Complete a pushed message from its bMessage, store it and announce it.

        The notification event carries folder, type and at best a subject; the sender's
        address and the text come from the message itself.
        """
        pushed_message = message_from_properties(message_path, properties)
        if pushed_message is None:
            logger.warning("pushed message ignored: its folder is not inbox, sent or outbox")
            return

        try:
            fetched = await self._get_bmessage(message_path)
        except MessagesError as messages_error:
            logger.warning("fetching a new message failed: %s", messages_error)
            fetched = None

        if fetched is not None:
            counterpart_address = pushed_message.counterpart_address or fetched.originator_address
            text = fetched.text or pushed_message.text
            is_read = pushed_message.is_read if fetched.is_read is None else fetched.is_read
            pushed_message = replace(
                pushed_message,
                counterpart_address=counterpart_address,
                text=text,
                is_text_complete=True,
                is_read=is_read,
            )

        is_still_wanted = address_key in self._session_path_by_address
        if not is_still_wanted:
            return

        logger.info("new message received")
        self._replace_message(address_key, pushed_message)
        if pushed_message.is_incoming:
            self.message_received.emit(address_key, pushed_message)

    def _on_object_removed(self, object_path: str, interfaces: list[str]) -> None:
        """Drop a message the phone withdrew, or fail the sync when the session went away."""
        address_key = self._address_for_object(object_path)
        if address_key is None:
            return

        if SESSION_INTERFACE in interfaces:
            self._session_path_by_address.pop(address_key, None)
            previous_state = self.state_for_address(address_key)
            logger.warning("message session closed by obexd or the phone")
            self._set_state(
                replace(
                    previous_state, sync_state=SYNC_STATE_FAILED, error_text=CONNECTION_LOST_TEXT
                )
            )
            return

        if MESSAGE_INTERFACE in interfaces:
            self._remove_message(address_key, object_path)

    def _message_at(self, address_key: str, message_path: str) -> TextMessage | None:
        """Return the stored message at `message_path` for the phone, or `None`."""
        for message in self.state_for_address(address_key).messages:
            if message.path == message_path:
                return message

        return None

    def _replace_message(self, address_key: str, updated_message: TextMessage) -> None:
        """Store `updated_message`, replacing the one at its path or adding it, and announce."""
        previous_state = self.state_for_address(address_key)
        remaining_messages: list[TextMessage] = []
        for message in previous_state.messages:
            if message.path != updated_message.path:
                remaining_messages.append(message)
        remaining_messages.append(updated_message)

        self._set_state(replace(previous_state, messages=tuple(remaining_messages)))

    def _remove_message(self, address_key: str, message_path: str) -> None:
        """Forget the message at `message_path` and announce the change."""
        previous_state = self.state_for_address(address_key)
        remaining_messages: list[TextMessage] = []
        for message in previous_state.messages:
            if message.path != message_path:
                remaining_messages.append(message)

        was_known = len(remaining_messages) != len(previous_state.messages)
        if was_known:
            self._set_state(replace(previous_state, messages=tuple(remaining_messages)))


def _unwrap_listing(reply_body: list[Any]) -> dict[str, dict[str, Any]]:
    """Return a `ListMessages` reply as `{object path: properties}`, variants unwrapped.

    obexd delivers the listing as a dictionary keyed by object path.
    """
    listing = unwrap_variant(reply_body[0])
    properties_by_path: dict[str, dict[str, Any]] = {}

    for message_path, properties in listing.items():
        properties_by_path[str(message_path)] = dict(properties)

    return properties_by_path


def _messages_error(obex_error: ObexError, refusal_text: str) -> MessagesError:
    """Translate an obexd failure into a user-facing `MessagesError`.

    Args:
        obex_error: The failure as raised by the obexd client.
        refusal_text: Explanation used when the phone refused or forbade the step.
    """
    if obex_error.is_service_missing:
        return MessagesError(OBEXD_MISSING_TEXT)

    if obex_error.is_connection_lost:
        return MessagesError(CONNECTION_LOST_TEXT)

    is_phone_refusal = obex_error.is_refused or obex_error.is_forbidden
    if is_phone_refusal:
        return MessagesError(refusal_text)

    if not obex_error.error_name:
        return MessagesError(LISTING_FAILED_TEXT)

    return MessagesError(str(obex_error))
