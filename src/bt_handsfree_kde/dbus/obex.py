"""Client side of obexd (`org.bluez.obex`, session bus): sessions, transfers and their signals.

obexd is the Bluetooth OBEX daemon of BlueZ. The contacts and messages features each open
an OBEX session to the phone through it; this module owns what they have in common:
creating and removing sessions, issuing calls on session objects, waiting for a transfer
to finish, following the objects obexd adds below a session, and the directory obexd
writes transfer files into.

obexd ties a client session to the D-Bus connection that created it and drops the
session as soon as that connection closes, so everything here runs on the app's
long-lived bus connection and never on a throwaway one.
"""

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde import APPLICATION_ID
from bt_handsfree_kde.dbus.helpers import (
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
    DBusRequestError,
    add_signal_match,
    call_method,
    is_name_owner_changed,
    name_owner_changed_match_rule,
    set_property,
    unwrap_variant,
)

logger = logging.getLogger(__name__)

# Well-known bus name of obexd and its client entry point.
OBEX_BUS_NAME = "org.bluez.obex"
OBEX_CLIENT_PATH = "/org/bluez/obex"
OBEX_CLIENT_INTERFACE = "org.bluez.obex.Client1"
# Interfaces on a session object and on the objects obexd creates below it.
SESSION_INTERFACE = "org.bluez.obex.Session1"
TRANSFER_INTERFACE = "org.bluez.obex.Transfer1"
PHONEBOOK_ACCESS_INTERFACE = "org.bluez.obex.PhonebookAccess1"
MESSAGE_ACCESS_INTERFACE = "org.bluez.obex.MessageAccess1"
MESSAGE_INTERFACE = "org.bluez.obex.Message1"
# Prefix of every client session path; server sessions (the phone connecting to obexd)
# live under `/org/bluez/obex/server/` and are not this client's business.
OBEX_CLIENT_PATH_PREFIX = "/org/bluez/obex/client/"
# `CreateSession` targets for the two profiles the app uses.
PBAP_TARGET = "pbap"
MAP_TARGET = "map"
# `Transfer1.Status` values that end a transfer.
TRANSFER_COMPLETE = "complete"
TRANSFER_ERROR = "error"
FINAL_TRANSFER_STATUSES = frozenset({TRANSFER_COMPLETE, TRANSFER_ERROR})
# How long to wait for the phone to finish one transfer, in seconds. A large phonebook
# over a slow link takes tens of seconds; beyond this the transfer counts as failed.
TRANSFER_TIMEOUT_S = 120
# D-Bus error names that callers turn into specific explanations. `NoReply` is what the
# bus daemon answers when obexd left the bus while a request to it was in flight.
SERVICE_UNKNOWN_ERROR = "org.freedesktop.DBus.Error.ServiceUnknown"
NO_REPLY_ERROR = "org.freedesktop.DBus.Error.NoReply"
OBEX_FAILED_ERROR = "org.bluez.obex.Error.Failed"
OBEX_FORBIDDEN_ERROR = "org.bluez.obex.Error.Forbidden"
# Permission bits of the transfer directory: owner only, the files hold personal data.
TRANSFER_DIRECTORY_MODE = 0o700
# Environment variable naming the cache root, and the fallback below the home directory.
CACHE_HOME_VARIABLE = "XDG_CACHE_HOME"
DEFAULT_CACHE_HOME_RELATIVE = ".cache"
# Bytes of randomness in a transfer file name; keeps concurrent transfers apart.
TRANSFER_FILE_RANDOM_BYTES = 8


class ObexError(Exception):
    """An obexd request failed; `error_name` is the D-Bus error name when there was one."""

    def __init__(self, message: str, error_name: str = "") -> None:
        """Create the error.

        Args:
            message: Description naming the failed request.
            error_name: D-Bus error name from the reply; empty for timeouts and lost
                transfers.
        """
        super().__init__(message)
        self.error_name = error_name

    @property
    def is_service_missing(self) -> bool:
        """Tell whether obexd is not installed (the bus could not activate it)."""
        return self.error_name == SERVICE_UNKNOWN_ERROR

    @property
    def is_connection_lost(self) -> bool:
        """Tell whether obexd went away while the request was in flight."""
        return self.error_name == NO_REPLY_ERROR

    @property
    def is_refused(self) -> bool:
        """Tell whether obexd reported a plain failure, which for a connection means refusal."""
        return self.error_name == OBEX_FAILED_ERROR

    @property
    def is_forbidden(self) -> bool:
        """Tell whether the phone forbade the operation."""
        return self.error_name == OBEX_FORBIDDEN_ERROR


class ObexClient(QObject):
    """Creates sessions on obexd, runs requests on them and follows their objects.

    Signals about objects below a session are emitted only for sessions this client
    created, so other obexd users on the bus stay invisible.
    """

    # Emitted with (object path, {interface: properties}) when obexd adds an object below
    # one of this client's sessions, transfers excluded.
    object_added = Signal(str, dict)
    # Emitted with (object path, [interface names]) when such an object, or a session
    # itself, is removed.
    object_removed = Signal(str, list)

    def __init__(self, bus: MessageBus, parent: QObject | None = None) -> None:
        """Create the client bound to the app's long-lived session bus connection.

        Args:
            bus: Connected session bus shared with the other clients.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._bus = bus
        self._session_paths: set[str] = set()
        # Latest `Transfer1.Status` per transfer path, for transfers whose completion
        # signal arrives before the reply that names them.
        self._transfer_status_by_path: dict[str, str] = {}
        # Futures resolved with the final status of the transfer at each path.
        self._transfer_waiter_by_path: dict[str, asyncio.Future[str]] = {}
        self._transfer_directory = obex_transfer_directory()
        self._sync_lock = asyncio.Lock()

    async def start(self) -> None:
        """Subscribe to obexd's object and property signals and follow its bus name.

        obexd is started on demand by the bus, so nothing is checked here; a missing
        obexd surfaces as an `ObexError` on the first session. Should obexd leave the
        bus later, every session this client holds is treated as removed, since a
        restarted obexd starts without sessions and never announces the old ones.

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
        await add_signal_match(self._bus, name_owner_changed_match_rule(OBEX_BUS_NAME))

    @property
    def sync_lock(self) -> asyncio.Lock:
        """Return the lock a feature holds while it syncs one phone.

        A phonebook pull and a message listing each hold it for their duration, so the
        syncs of several phones, and of both features, reach obexd one at a time
        instead of opening channels to two phones at once. Fetching a single message
        or marking one read does not take it.
        """
        return self._sync_lock

    def owns_session(self, session_path: str) -> bool:
        """Tell whether `session_path` is a session this client created and still holds."""
        return session_path in self._session_paths

    async def create_session(self, address: str, target: str) -> str:
        """Open an OBEX session to the phone at `address` for `target` (`pbap` or `map`).

        Returns:
            Object path of the new session.

        Raises:
            ObexError: obexd is missing, the phone refused, or the call failed.
        """
        session_arguments = {"Target": Variant("s", target)}
        reply_body = await self.call(
            OBEX_CLIENT_PATH,
            OBEX_CLIENT_INTERFACE,
            "CreateSession",
            "sa{sv}",
            [address, session_arguments],
        )
        session_path = str(reply_body[0])
        self._session_paths.add(session_path)

        return session_path

    async def remove_session(self, session_path: str) -> None:
        """Close `session_path`; a session obexd already dropped is not an error."""
        self._session_paths.discard(session_path)

        try:
            await self.call(
                OBEX_CLIENT_PATH, OBEX_CLIENT_INTERFACE, "RemoveSession", "o", [session_path]
            )
        except ObexError as obex_error:
            logger.debug("removing the obex session failed: %s", obex_error)

    async def call(
        self,
        object_path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> list[Any]:
        """Invoke `member` on an obexd object.

        Returns:
            The reply body, variants left as delivered.

        Raises:
            ObexError: obexd answered with an error or could not be reached.
        """
        try:
            reply_body = await call_method(
                self._bus, OBEX_BUS_NAME, object_path, interface, member, signature, body
            )
        except DBusRequestError as request_error:
            raise ObexError(str(request_error), request_error.error_name) from request_error

        return reply_body

    async def set_property(
        self, object_path: str, interface: str, property_name: str, value: Variant
    ) -> None:
        """Set a property on an obexd object.

        Raises:
            ObexError: obexd rejected the value or could not be reached.
        """
        try:
            await set_property(
                self._bus, OBEX_BUS_NAME, object_path, interface, property_name, value
            )
        except DBusRequestError as request_error:
            raise ObexError(str(request_error), request_error.error_name) from request_error

    async def wait_for_transfer(self, transfer_reply_body: list[Any]) -> str:
        """Wait for the transfer a request just started and return its final status.

        `PullAll`, `Get` and `PushMessage` all reply with the transfer path and its
        properties. The completion signal can arrive before that reply is processed, so
        a status recorded by the signal handler is consulted before waiting.

        Args:
            transfer_reply_body: The reply body of the request that started the transfer.

        Returns:
            `TRANSFER_COMPLETE` or `TRANSFER_ERROR`.

        Raises:
            ObexError: the phone did not finish within `TRANSFER_TIMEOUT_S`.
        """
        transfer_path = str(transfer_reply_body[0])
        transfer_properties = unwrap_variant(transfer_reply_body[1])
        initial_status = str(transfer_properties.get("Status", ""))

        latest_status = self._transfer_status_by_path.pop(transfer_path, initial_status)
        if latest_status in FINAL_TRANSFER_STATUSES:
            return latest_status

        # The session may have gone (obexd left the bus, the phone dropped the link)
        # between the request and this point; its transfer will never finish.
        session_path = transfer_path.rsplit("/", 1)[0]
        session_is_gone = not self.owns_session(session_path)
        if session_is_gone:
            return TRANSFER_ERROR

        event_loop = asyncio.get_event_loop()
        transfer_done: asyncio.Future[str] = event_loop.create_future()
        self._transfer_waiter_by_path[transfer_path] = transfer_done

        try:
            final_status = await asyncio.wait_for(transfer_done, TRANSFER_TIMEOUT_S)
        except TimeoutError as timeout_error:
            raise ObexError(f"transfer {transfer_path} timed out") from timeout_error
        finally:
            self._transfer_waiter_by_path.pop(transfer_path, None)

        return final_status

    def new_transfer_file(self, name_prefix: str, name_suffix: str) -> Path:
        """Return a fresh file path in the transfer directory, creating the directory.

        Args:
            name_prefix: Start of the file name, saying what the file will hold.
            name_suffix: File extension including the dot.
        """
        self._transfer_directory.mkdir(mode=TRANSFER_DIRECTORY_MODE, parents=True, exist_ok=True)
        random_part = secrets.token_hex(TRANSFER_FILE_RANDOM_BYTES)
        file_name = f"{name_prefix}{random_part}{name_suffix}"

        return self._transfer_directory / file_name

    def _handle_message(self, message: Message) -> None:
        """Dispatch obexd signals; returns `None` so other handlers run too."""
        if message.message_type != MessageType.SIGNAL:
            return None

        if is_name_owner_changed(message):
            self._on_name_owner_changed(message.body)
            return None

        # PropertiesChanged is emitted on the object itself; the object manager signals
        # are emitted on obexd's root object and name the object in their body.
        if message.interface == PROPERTIES_INTERFACE and message.member == "PropertiesChanged":
            is_client_object = message.path is not None and message.path.startswith(
                OBEX_CLIENT_PATH_PREFIX
            )
            if is_client_object:
                self._on_properties_changed(message.path, message.body)
            return None

        is_object_manager_signal = message.interface == OBJECT_MANAGER_INTERFACE and (
            message.member in ("InterfacesAdded", "InterfacesRemoved")
        )
        if not is_object_manager_signal:
            return None

        announced_path = str(message.body[0])
        is_client_object = announced_path.startswith(OBEX_CLIENT_PATH_PREFIX)
        if not is_client_object:
            return None

        if message.member == "InterfacesAdded":
            self._on_interfaces_added(message.body)
        else:
            self._on_interfaces_removed(message.body)

        return None

    def _on_name_owner_changed(self, signal_body: list[Any]) -> None:
        """Drop every held session when obexd leaves the bus."""
        changed_name = signal_body[0]
        new_owner = signal_body[2]
        if changed_name != OBEX_BUS_NAME:
            return

        service_left = new_owner == ""
        if not service_left:
            logger.info("obexd appeared on the bus")
            return

        logger.warning("obexd left the bus; dropping %d session(s)", len(self._session_paths))
        self._transfer_status_by_path.clear()
        for session_path in sorted(self._session_paths):
            self._drop_session(session_path)

    def _drop_session(self, session_path: str) -> None:
        """Forget `session_path`, fail its pending transfers and announce its removal."""
        self._session_paths.discard(session_path)

        session_prefix = session_path + "/"
        for transfer_path in list(self._transfer_waiter_by_path):
            if transfer_path.startswith(session_prefix):
                self._resolve_transfer(transfer_path, TRANSFER_ERROR)

        self.object_removed.emit(session_path, [SESSION_INTERFACE])

    def _is_below_own_session(self, object_path: str) -> bool:
        """Tell whether `object_path` is a child of a session this client holds."""
        for session_path in self._session_paths:
            if object_path.startswith(session_path + "/"):
                return True

        return False

    def _on_properties_changed(self, object_path: str, signal_body: list[Any]) -> None:
        """Record a transfer's new status and wake the request waiting for it."""
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

    def _on_interfaces_added(self, signal_body: list[Any]) -> None:
        """Announce a new object below one of this client's sessions, transfers excluded."""
        object_path = signal_body[0]
        interfaces = unwrap_variant(signal_body[1])

        is_transfer = TRANSFER_INTERFACE in interfaces
        if is_transfer:
            return

        interface_names = sorted(interfaces)
        is_own = self._is_below_own_session(object_path)
        logger.debug(
            "obexd added %s with %s (own session: %s)", object_path, interface_names, is_own
        )
        if not is_own:
            return

        self.object_added.emit(object_path, interfaces)

    def _on_interfaces_removed(self, signal_body: list[Any]) -> None:
        """Fail transfers that vanish without a final status and announce other removals."""
        object_path = signal_body[0]
        removed_interfaces = list(signal_body[1])

        if TRANSFER_INTERFACE in removed_interfaces:
            self._transfer_status_by_path.pop(object_path, None)
            self._resolve_transfer(object_path, TRANSFER_ERROR)
            return

        logger.debug("obexd removed %s with %s", object_path, removed_interfaces)
        session_removed = SESSION_INTERFACE in removed_interfaces
        if session_removed and object_path in self._session_paths:
            self._drop_session(object_path)
            return

        if self._is_below_own_session(object_path):
            self.object_removed.emit(object_path, removed_interfaces)

    def _resolve_transfer(self, transfer_path: str, final_status: str) -> None:
        """Hand `final_status` to the request waiting on `transfer_path`, if any."""
        transfer_done = self._transfer_waiter_by_path.get(transfer_path)
        is_pending = transfer_done is not None and not transfer_done.done()

        if is_pending:
            transfer_done.set_result(final_status)


def obex_transfer_directory() -> Path:
    """Return the directory obexd writes the app's transfer files into.

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
