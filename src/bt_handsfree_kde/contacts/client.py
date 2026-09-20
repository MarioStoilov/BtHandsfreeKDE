"""Phonebook sync through obexd's Phonebook Access client (`org.bluez.obex`, session bus).

One pull per phone: an OBEX session to the phone's PBAP server, the internal phonebook
selected, the whole book fetched as vCard into a file in the app's cache directory,
parsed, and the file deleted. Phonebooks live in memory for the session only.

obexd ties a client session to the D-Bus connection that created it and drops the
session as soon as that connection closes, so the client uses the app's long-lived bus
connection and never a throwaway one.
"""

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde import APPLICATION_ID
from bt_handsfree_kde.contacts.phonebook import Phonebook
from bt_handsfree_kde.contacts.vcard import parse_contacts
from bt_handsfree_kde.dbus.helpers import (
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
    DBusRequestError,
    add_signal_match,
    call_method,
    unwrap_variant,
)

logger = logging.getLogger(__name__)

# Well-known bus name of obexd (session bus) and its client entry point.
OBEX_BUS_NAME = "org.bluez.obex"
OBEX_CLIENT_PATH = "/org/bluez/obex"
OBEX_CLIENT_INTERFACE = "org.bluez.obex.Client1"
# Interfaces on the session object and on its transfer children.
SESSION_INTERFACE = "org.bluez.obex.Session1"
PHONEBOOK_ACCESS_INTERFACE = "org.bluez.obex.PhonebookAccess1"
TRANSFER_INTERFACE = "org.bluez.obex.Transfer1"
# Prefix of every client session and transfer path; used to filter signals.
OBEX_CLIENT_PATH_PREFIX = "/org/bluez/obex/client/"
# `CreateSession` target for the Phonebook Access Profile.
PBAP_TARGET = "pbap"
# Phonebook selected for the pull: the phone's internal memory, main contact list.
PHONEBOOK_LOCATION = "int"
PHONEBOOK_NAME = "pb"
# vCard format and fields requested from the phone; nothing else (photos in particular)
# is transferred. A phone may ignore either, which the parser copes with.
VCARD_FORMAT = "vcard30"
REQUESTED_FIELDS = ["N", "FN", "TEL"]
# `Transfer1.Status` values that end a transfer.
TRANSFER_COMPLETE = "complete"
TRANSFER_ERROR = "error"
FINAL_TRANSFER_STATUSES = frozenset({TRANSFER_COMPLETE, TRANSFER_ERROR})
# How long to wait for the phone to finish sending the phonebook, in seconds. A large
# phonebook over a slow link takes tens of seconds; beyond this the sync is reported
# as failed and the user can refresh.
TRANSFER_TIMEOUT_S = 120
# Delay between a phone connecting over HFP and the automatic pull, in seconds, so the
# phone is not asked for a second Bluetooth channel while it is still setting up the
# first.
AUTOMATIC_SYNC_DELAY_S = 2.0
# D-Bus error names that get a specific explanation.
SERVICE_UNKNOWN_ERROR = "org.freedesktop.DBus.Error.ServiceUnknown"
OBEX_FAILED_ERROR = "org.bluez.obex.Error.Failed"
OBEX_FORBIDDEN_ERROR = "org.bluez.obex.Error.Forbidden"
# Name pattern of the transfer file inside the cache directory; the random part keeps
# concurrent pulls from several phones apart.
VCARD_FILE_PREFIX = "phonebook-"
VCARD_FILE_SUFFIX = ".vcf"
# Bytes of randomness in the transfer file name.
VCARD_FILE_RANDOM_BYTES = 8
# Permission bits of the cache directory: owner only, the file holds personal data.
CACHE_DIRECTORY_MODE = 0o700
# Environment variable naming the cache root, and the fallback below the home directory.
CACHE_HOME_VARIABLE = "XDG_CACHE_HOME"
DEFAULT_CACHE_HOME_RELATIVE = ".cache"
# Sync states of one phone's phonebook.
SYNC_STATE_IDLE = "idle"
SYNC_STATE_SYNCING = "syncing"
SYNC_STATE_SYNCED = "synced"
SYNC_STATE_FAILED = "failed"
# User-facing explanations of the failures a user can do something about.
OBEXD_MISSING_TEXT = (
    "Contacts need obexd, the Bluetooth file transfer service; it is missing. "
    "Install the bluez-obexd package and refresh."
)
CONNECTION_REFUSED_TEXT = (
    "The phone refused the contacts connection. Allow contact sharing for this computer "
    "in the phone's Bluetooth settings, then refresh."
)
PHONEBOOK_FORBIDDEN_TEXT = (
    "The phone did not allow reading its phonebook. Check the contact sharing permission "
    "for this computer on the phone, then refresh."
)
TRANSFER_FAILED_TEXT = "The phone stopped sending its phonebook. Refresh to try again."
TRANSFER_TIMEOUT_TEXT = "The phone did not finish sending its phonebook. Refresh to try again."


class ContactsError(Exception):
    """A phonebook pull failed; the message is written for the user."""


@dataclass(frozen=True)
class PhonebookState:
    """Where one phone's phonebook sync stands, as shown in the Contacts tab."""

    # Upper-case Bluetooth address of the phone.
    address: str
    # One of the `SYNC_STATE_*` values.
    sync_state: str
    # The last successful pull; kept while a refresh is running.
    phonebook: Phonebook | None
    # Local time of the last successful pull.
    synced_at: datetime | None
    # User-facing reason when `sync_state` is failed, else empty.
    error_text: str


class ContactsClient(QObject):
    """Pulls and keeps one phonebook per connected phone and answers name lookups."""

    # Emitted with the phone's upper-case address whenever its sync state changes.
    phonebook_changed = Signal(str)

    def __init__(self, bus: MessageBus, parent: QObject | None = None) -> None:
        """Create a client bound to the app's long-lived session bus connection.

        Args:
            bus: Connected session bus shared with the other clients.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._bus = bus
        self._state_by_address: dict[str, PhonebookState] = {}
        self._sync_task_by_address: dict[str, asyncio.Task[None]] = {}
        # Latest `Transfer1.Status` per transfer path, for transfers whose completion
        # signal may arrive before the `PullAll` reply that names them.
        self._transfer_status_by_path: dict[str, str] = {}
        # Futures resolved with the final status of the transfer at each path.
        self._transfer_waiter_by_path: dict[str, asyncio.Future[str]] = {}
        self._cache_directory = phonebook_cache_directory()

    async def start(self) -> None:
        """Subscribe to obexd's transfer signals.

        obexd is started on demand by the bus, so nothing is checked here; a missing
        obexd surfaces as a failed sync with an explanation.

        Raises:
            DBusRequestError: the bus daemon refused the signal subscriptions.
        """
        self._bus.add_message_handler(self._handle_message)
        await add_signal_match(
            self._bus,
            f"type='signal',sender='{OBEX_BUS_NAME}',interface='{PROPERTIES_INTERFACE}'",
        )
        await add_signal_match(
            self._bus,
            f"type='signal',sender='{OBEX_BUS_NAME}',interface='{OBJECT_MANAGER_INTERFACE}'",
        )

    def state_for_address(self, address: str) -> PhonebookState:
        """Return the sync state of the phone at `address`; idle when never synced."""
        address_key = address.upper()
        known_state = self._state_by_address.get(address_key)
        if known_state is not None:
            return known_state

        return PhonebookState(address_key, SYNC_STATE_IDLE, None, None, "")

    def lookup_name(self, address: str, number: str) -> str:
        """Return the contact name for `number` in the phonebook of `address`, or empty."""
        phonebook_state = self.state_for_address(address)
        if phonebook_state.phonebook is None:
            return ""

        return phonebook_state.phonebook.lookup_name(number)

    def start_sync(self, address: str, delay_seconds: float = 0.0) -> None:
        """Pull the phonebook of the phone at `address` in the background.

        A pull already running for that phone is left alone. The outcome is published
        through `phonebook_changed`; a failure is stored in the state, not raised.

        Args:
            address: Bluetooth address of the phone, any letter case.
            delay_seconds: Wait before connecting, for pulls triggered by a connect.
        """
        address_key = address.upper()
        is_already_syncing = address_key in self._sync_task_by_address
        if is_already_syncing:
            return

        previous_state = self.state_for_address(address_key)
        self._set_state(
            PhonebookState(
                address_key,
                SYNC_STATE_SYNCING,
                previous_state.phonebook,
                previous_state.synced_at,
                "",
            )
        )

        event_loop = asyncio.get_event_loop()
        sync_task = event_loop.create_task(self._sync(address_key, delay_seconds))
        self._sync_task_by_address[address_key] = sync_task

    def forget(self, address: str) -> None:
        """Drop the phonebook of the phone at `address` and stop a pull in progress."""
        address_key = address.upper()

        running_task = self._sync_task_by_address.pop(address_key, None)
        if running_task is not None:
            running_task.cancel()

        had_state = self._state_by_address.pop(address_key, None) is not None
        if had_state:
            self.phonebook_changed.emit(address_key)

    async def _sync(self, address_key: str, delay_seconds: float) -> None:
        """Run one pull for `address_key` and store its outcome."""
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            phonebook = await self._pull_phonebook(address_key)
        except asyncio.CancelledError:
            raise
        except ContactsError as contacts_error:
            logger.warning("phonebook sync failed: %s", contacts_error)
            self._finish_sync(address_key, None, str(contacts_error))
            return
        except Exception as unexpected_error:
            logger.exception("phonebook sync failed unexpectedly")
            self._finish_sync(address_key, None, str(unexpected_error))
            return

        logger.info("phonebook synced: %d contact(s)", phonebook.contact_count)
        self._finish_sync(address_key, phonebook, "")

    def _finish_sync(self, address_key: str, phonebook: Phonebook | None, error_text: str) -> None:
        """Record the outcome of a pull, keeping the previous phonebook on failure."""
        self._sync_task_by_address.pop(address_key, None)
        previous_state = self.state_for_address(address_key)

        if phonebook is not None:
            new_state = PhonebookState(
                address_key, SYNC_STATE_SYNCED, phonebook, datetime.now(), ""
            )
        else:
            new_state = PhonebookState(
                address_key,
                SYNC_STATE_FAILED,
                previous_state.phonebook,
                previous_state.synced_at,
                error_text,
            )

        self._set_state(new_state)

    def _set_state(self, new_state: PhonebookState) -> None:
        """Store a state and announce it."""
        self._state_by_address[new_state.address] = new_state
        self.phonebook_changed.emit(new_state.address)

    async def _pull_phonebook(self, address: str) -> Phonebook:
        """Fetch and parse the phone's phonebook, cleaning up the session and the file.

        Raises:
            ContactsError: any step failed; the message explains what to do.
        """
        session_path = await self._create_session(address)
        target_file = self._new_target_file()

        try:
            await self._select_phonebook(session_path)
            await self._pull_all(session_path, target_file)
            vcard_text = target_file.read_text(encoding="utf-8", errors="replace")
        finally:
            target_file.unlink(missing_ok=True)
            await self._remove_session(session_path)

        contacts = parse_contacts(vcard_text)

        return Phonebook(tuple(contacts))

    async def _create_session(self, address: str) -> str:
        """Open a PBAP session to the phone and return its object path.

        Raises:
            ContactsError: obexd is missing or the phone refused the connection.
        """
        session_arguments = {"Target": Variant("s", PBAP_TARGET)}

        try:
            reply_body = await call_method(
                self._bus,
                OBEX_BUS_NAME,
                OBEX_CLIENT_PATH,
                OBEX_CLIENT_INTERFACE,
                "CreateSession",
                "sa{sv}",
                [address, session_arguments],
            )
        except DBusRequestError as request_error:
            if request_error.error_name == SERVICE_UNKNOWN_ERROR:
                raise ContactsError(OBEXD_MISSING_TEXT) from request_error
            if request_error.error_name == OBEX_FAILED_ERROR:
                raise ContactsError(CONNECTION_REFUSED_TEXT) from request_error
            raise ContactsError(str(request_error)) from request_error

        session_path = str(reply_body[0])

        return session_path

    async def _select_phonebook(self, session_path: str) -> None:
        """Select the internal phonebook on the session.

        Raises:
            ContactsError: the phone rejected the selection.
        """
        try:
            await call_method(
                self._bus,
                OBEX_BUS_NAME,
                session_path,
                PHONEBOOK_ACCESS_INTERFACE,
                "Select",
                "ss",
                [PHONEBOOK_LOCATION, PHONEBOOK_NAME],
            )
        except DBusRequestError as request_error:
            if request_error.error_name == OBEX_FORBIDDEN_ERROR:
                raise ContactsError(PHONEBOOK_FORBIDDEN_TEXT) from request_error
            raise ContactsError(str(request_error)) from request_error

    async def _pull_all(self, session_path: str, target_file: Path) -> None:
        """Download the selected phonebook into `target_file` and wait for it to finish.

        Raises:
            ContactsError: the phone refused, the transfer failed or it timed out.
        """
        pull_filters = {
            "Format": Variant("s", VCARD_FORMAT),
            "Fields": Variant("as", REQUESTED_FIELDS),
        }

        try:
            reply_body = await call_method(
                self._bus,
                OBEX_BUS_NAME,
                session_path,
                PHONEBOOK_ACCESS_INTERFACE,
                "PullAll",
                "sa{sv}",
                [str(target_file), pull_filters],
            )
        except DBusRequestError as request_error:
            if request_error.error_name == OBEX_FORBIDDEN_ERROR:
                raise ContactsError(PHONEBOOK_FORBIDDEN_TEXT) from request_error
            raise ContactsError(str(request_error)) from request_error

        transfer_path = str(reply_body[0])
        transfer_properties = unwrap_variant(reply_body[1])
        initial_status = str(transfer_properties.get("Status", ""))

        final_status = await self._wait_for_transfer(transfer_path, initial_status)
        if final_status != TRANSFER_COMPLETE:
            raise ContactsError(TRANSFER_FAILED_TEXT)

    async def _wait_for_transfer(self, transfer_path: str, initial_status: str) -> str:
        """Return the final status of the transfer at `transfer_path`.

        The completion signal can arrive before the `PullAll` reply is processed, so a
        status recorded by the signal handler is consulted before waiting.

        Raises:
            ContactsError: the phone did not finish within `TRANSFER_TIMEOUT_S`.
        """
        latest_status = self._transfer_status_by_path.pop(transfer_path, initial_status)
        if latest_status in FINAL_TRANSFER_STATUSES:
            return latest_status

        event_loop = asyncio.get_event_loop()
        transfer_done: asyncio.Future[str] = event_loop.create_future()
        self._transfer_waiter_by_path[transfer_path] = transfer_done

        try:
            final_status = await asyncio.wait_for(transfer_done, TRANSFER_TIMEOUT_S)
        except TimeoutError as timeout_error:
            raise ContactsError(TRANSFER_TIMEOUT_TEXT) from timeout_error
        finally:
            self._transfer_waiter_by_path.pop(transfer_path, None)

        return final_status

    async def _remove_session(self, session_path: str) -> None:
        """Close the session; a session obexd already dropped is not an error."""
        try:
            await call_method(
                self._bus,
                OBEX_BUS_NAME,
                OBEX_CLIENT_PATH,
                OBEX_CLIENT_INTERFACE,
                "RemoveSession",
                "o",
                [session_path],
            )
        except DBusRequestError as request_error:
            logger.debug("removing the obex session failed: %s", request_error)

    def _new_target_file(self) -> Path:
        """Return a fresh file path in the cache directory, creating the directory."""
        self._cache_directory.mkdir(mode=CACHE_DIRECTORY_MODE, parents=True, exist_ok=True)
        random_part = secrets.token_hex(VCARD_FILE_RANDOM_BYTES)
        file_name = f"{VCARD_FILE_PREFIX}{random_part}{VCARD_FILE_SUFFIX}"

        return self._cache_directory / file_name

    def _handle_message(self, message: Message) -> None:
        """Track transfer status signals; returns `None` so other handlers run too."""
        if message.message_type != MessageType.SIGNAL:
            return None

        is_obex_client_object = message.path is not None and message.path.startswith(
            OBEX_CLIENT_PATH_PREFIX
        )
        if not is_obex_client_object:
            return None

        if message.interface == PROPERTIES_INTERFACE and message.member == "PropertiesChanged":
            self._on_properties_changed(message.path, message.body)
        elif (
            message.interface == OBJECT_MANAGER_INTERFACE and message.member == "InterfacesRemoved"
        ):
            self._on_interfaces_removed(message.body)

        return None

    def _on_properties_changed(self, object_path: str, signal_body: list[Any]) -> None:
        """Record a transfer's new status and wake the pull waiting for it."""
        changed_interface = signal_body[0]
        if changed_interface != TRANSFER_INTERFACE:
            return

        changed_properties = unwrap_variant(signal_body[1])
        new_status = changed_properties.get("Status")
        if new_status is None:
            return

        self._transfer_status_by_path[object_path] = str(new_status)
        if new_status in FINAL_TRANSFER_STATUSES:
            self._resolve_transfer(object_path, str(new_status))

    def _on_interfaces_removed(self, signal_body: list[Any]) -> None:
        """Treat a transfer or session vanishing without a final status as a failure."""
        object_path = signal_body[0]
        removed_interfaces = signal_body[1]

        if TRANSFER_INTERFACE in removed_interfaces:
            self._transfer_status_by_path.pop(object_path, None)
            self._resolve_transfer(object_path, TRANSFER_ERROR)

        if SESSION_INTERFACE in removed_interfaces:
            session_prefix = object_path + "/"
            for transfer_path in list(self._transfer_waiter_by_path):
                if transfer_path.startswith(session_prefix):
                    self._resolve_transfer(transfer_path, TRANSFER_ERROR)

    def _resolve_transfer(self, transfer_path: str, final_status: str) -> None:
        """Hand `final_status` to the pull waiting on `transfer_path`, if any."""
        transfer_done = self._transfer_waiter_by_path.get(transfer_path)
        is_pending = transfer_done is not None and not transfer_done.done()

        if is_pending:
            transfer_done.set_result(final_status)


def phonebook_cache_directory() -> Path:
    """Return the directory obexd writes phonebook transfers into.

    It is below `XDG_CACHE_HOME` (or `~/.cache`), named after the application ID. Inside
    the Flatpak sandbox that variable points at the app's own cache directory, whose
    absolute path is the same on the host, where obexd runs.
    """
    cache_home_setting = os.environ.get(CACHE_HOME_VARIABLE, "")
    if cache_home_setting:
        cache_home = Path(cache_home_setting)
    else:
        cache_home = Path.home() / DEFAULT_CACHE_HOME_RELATIVE

    return cache_home / APPLICATION_ID
