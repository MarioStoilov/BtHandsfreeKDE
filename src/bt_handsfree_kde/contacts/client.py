"""Phonebook sync through obexd's Phonebook Access client.

One pull per phone: an OBEX session to the phone's PBAP server, the internal phonebook
selected, the whole book fetched as vCard into a file in the app's transfer directory,
parsed, and the file deleted. Phonebooks live in memory for the session only.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from dbus_fast import Variant
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.contacts.phonebook import Phonebook
from bt_handsfree_kde.contacts.vcard import parse_contacts
from bt_handsfree_kde.dbus.obex import (
    PBAP_TARGET,
    PHONEBOOK_ACCESS_INTERFACE,
    TRANSFER_COMPLETE,
    ObexClient,
    ObexError,
)

logger = logging.getLogger(__name__)

# Phonebook selected for the pull: the phone's internal memory, main contact list.
PHONEBOOK_LOCATION = "int"
PHONEBOOK_NAME = "pb"
# vCard format and fields requested from the phone; nothing else (photos in particular)
# is transferred. A phone may ignore either, which the parser copes with.
VCARD_FORMAT = "vcard30"
REQUESTED_FIELDS = ["N", "FN", "TEL"]
# Delay between a phone connecting over HFP and the automatic pull, in seconds, so the
# phone is not asked for a second Bluetooth channel while it is still setting up the
# first.
AUTOMATIC_SYNC_DELAY_S = 2.0
# Name pattern of the transfer file inside the transfer directory.
VCARD_FILE_PREFIX = "phonebook-"
VCARD_FILE_SUFFIX = ".vcf"
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

    def __init__(self, obex: ObexClient, parent: QObject | None = None) -> None:
        """Create a client that pulls through the shared obexd client.

        Args:
            obex: Started obexd client shared with the messages feature.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._obex = obex
        self._state_by_address: dict[str, PhonebookState] = {}
        self._sync_task_by_address: dict[str, asyncio.Task[None]] = {}

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
        try:
            session_path = await self._obex.create_session(address, PBAP_TARGET)
        except ObexError as obex_error:
            raise _contacts_error(obex_error, CONNECTION_REFUSED_TEXT) from obex_error
        target_file = self._obex.new_transfer_file(VCARD_FILE_PREFIX, VCARD_FILE_SUFFIX)

        try:
            await self._select_phonebook(session_path)
            await self._pull_all(session_path, str(target_file))
            vcard_text = target_file.read_text(encoding="utf-8", errors="replace")
        finally:
            target_file.unlink(missing_ok=True)
            await self._obex.remove_session(session_path)

        contacts = parse_contacts(vcard_text)

        return Phonebook(tuple(contacts))

    async def _select_phonebook(self, session_path: str) -> None:
        """Select the internal phonebook on the session.

        Raises:
            ContactsError: the phone rejected the selection.
        """
        try:
            await self._obex.call(
                session_path,
                PHONEBOOK_ACCESS_INTERFACE,
                "Select",
                "ss",
                [PHONEBOOK_LOCATION, PHONEBOOK_NAME],
            )
        except ObexError as obex_error:
            raise _contacts_error(obex_error, PHONEBOOK_FORBIDDEN_TEXT) from obex_error

    async def _pull_all(self, session_path: str, target_file: str) -> None:
        """Download the selected phonebook into `target_file` and wait for it to finish.

        Raises:
            ContactsError: the phone refused, the transfer failed or it timed out.
        """
        pull_filters = {
            "Format": Variant("s", VCARD_FORMAT),
            "Fields": Variant("as", REQUESTED_FIELDS),
        }

        try:
            reply_body = await self._obex.call(
                session_path,
                PHONEBOOK_ACCESS_INTERFACE,
                "PullAll",
                "sa{sv}",
                [target_file, pull_filters],
            )
            final_status = await self._obex.wait_for_transfer(reply_body)
        except ObexError as obex_error:
            raise _contacts_error(obex_error, PHONEBOOK_FORBIDDEN_TEXT) from obex_error

        if final_status != TRANSFER_COMPLETE:
            raise ContactsError(TRANSFER_FAILED_TEXT)


def _contacts_error(obex_error: ObexError, refusal_text: str) -> ContactsError:
    """Translate an obexd failure into a user-facing `ContactsError`.

    Args:
        obex_error: The failure as raised by the obexd client.
        refusal_text: Explanation used when the phone refused or forbade the step.
    """
    if obex_error.is_service_missing:
        return ContactsError(OBEXD_MISSING_TEXT)

    is_phone_refusal = obex_error.is_refused or obex_error.is_forbidden
    if is_phone_refusal:
        return ContactsError(refusal_text)

    if not obex_error.error_name:
        return ContactsError(TRANSFER_FAILED_TEXT)

    return ContactsError(str(obex_error))
