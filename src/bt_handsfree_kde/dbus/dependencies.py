"""Check of the services and the library the app depends on, at startup and on every change.

Each item is checked over the bus it lives on (or, for key tones, by loading the
library) and reported with the distro package or desktop component that provides it, in
the words the README uses. Nothing here is fatal: the application keeps running with
whatever works, and the report is refreshed when one of the names appears on or leaves
its bus.
"""

import asyncio
import logging
from dataclasses import dataclass

from dbus_fast import BusType, Message
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.dbus.bluez import BLUEZ_BUS_NAME
from bt_handsfree_kde.dbus.helpers import (
    DBUS_DAEMON_BUS_NAME,
    DBUS_DAEMON_INTERFACE,
    DBUS_DAEMON_PATH,
    DBusRequestError,
    add_signal_match,
    call_method,
    is_name_owner_changed,
    name_has_owner,
    name_owner_changed_match_rule,
)
from bt_handsfree_kde.dbus.notifications import (
    ACTIONS_CAPABILITY,
    NOTIFICATIONS_BUS_NAME,
    NOTIFICATIONS_INTERFACE,
    NOTIFICATIONS_PATH,
)
from bt_handsfree_kde.dbus.obex import OBEX_BUS_NAME
from bt_handsfree_kde.dbus.statusnotifier import WATCHER_BUS_NAME
from bt_handsfree_kde.dbus.telephony import TELEPHONY_BUS_NAME
from bt_handsfree_kde.dtmf import LIBPULSE_SIMPLE_NAME, libpulse_simple_is_available

logger = logging.getLogger(__name__)

# Keys identifying each checked item in a report.
TELEPHONY_KEY = "telephony"
BLUEZ_KEY = "bluez"
OBEXD_KEY = "obexd"
NOTIFICATIONS_KEY = "notifications"
TRAY_KEY = "tray"
KEY_TONES_KEY = "key-tones"
# Short titles, used in the window rows, the tray tooltip and the notification.
TELEPHONY_TITLE = "PipeWire telephony service"
BLUEZ_TITLE = "Bluetooth daemon (BlueZ)"
OBEXD_TITLE = "Bluetooth transfer service (obexd)"
NOTIFICATIONS_TITLE = "Notifications with buttons"
TRAY_TITLE = "System tray"
KEY_TONES_TITLE = "Key tones"
# What provides each item, in the words of the README's requirements and package lists.
TELEPHONY_REMEDY = (
    "PipeWire 1.4 or newer with the native Bluetooth backend (packages pipewire, "
    "wireplumber, libspa-0.2-bluetooth) and its telephony D-Bus service enabled. Calls "
    "cannot be seen or controlled without it."
)
BLUEZ_REMEDY = (
    "The bluez package with its bluetooth service running. Phone names and battery "
    "levels come from it."
)
OBEXD_REMEDY = "The bluez-obexd package. Contacts and messages need it; calls work without it."
NOTIFICATIONS_REMEDY = (
    "A freedesktop notification server that supports actions, such as Plasma's. Without "
    "buttons, ringing calls are shown in a window instead of a notification."
)
TRAY_REMEDY = (
    "A StatusNotifierItem system tray, such as Plasma's. The icon appears as soon as one starts."
)
KEY_TONES_REMEDY = (
    "The libpulse library (package libpulse0 on Debian and Ubuntu, pulseaudio-libs on "
    "Fedora). The dialpad keys are silent without it."
)
# Explanations shown next to an item when it is met or missing.
TELEPHONY_RUNNING_DETAIL = "Running on the session bus."
TELEPHONY_ACTIVATABLE_DETAIL = "Starts on demand."
TELEPHONY_MISSING_DETAIL = f"{TELEPHONY_BUS_NAME} is not on the session bus and cannot be started."
BLUEZ_RUNNING_DETAIL = "Running on the system bus."
BLUEZ_MISSING_DETAIL = f"{BLUEZ_BUS_NAME} is not on the system bus."
SYSTEM_BUS_MISSING_DETAIL = "The system bus could not be reached."
OBEXD_RUNNING_DETAIL = "Running on the session bus."
OBEXD_ACTIVATABLE_DETAIL = "Starts on demand."
OBEXD_MISSING_DETAIL = f"{OBEX_BUS_NAME} is not on the session bus and cannot be started."
NOTIFICATIONS_OK_DETAIL = "The notification server supports buttons."
NOTIFICATIONS_MISSING_DETAIL = (
    f"{NOTIFICATIONS_BUS_NAME} is not on the session bus and cannot be started."
)
NOTIFICATIONS_NO_ACTIONS_DETAIL = "The notification server cannot show buttons."
NOTIFICATIONS_UNANSWERED_DETAIL = "The notification server did not answer."
TRAY_RUNNING_DETAIL = "A StatusNotifierWatcher is on the session bus."
TRAY_MISSING_DETAIL = f"{WATCHER_BUS_NAME} is not on the session bus."
KEY_TONES_OK_DETAIL = f"{LIBPULSE_SIMPLE_NAME} loaded."
KEY_TONES_MISSING_DETAIL = f"{LIBPULSE_SIMPLE_NAME} could not be loaded."
# Session bus names whose ownership changes trigger a new check.
WATCHED_SESSION_NAMES = (
    TELEPHONY_BUS_NAME,
    OBEX_BUS_NAME,
    NOTIFICATIONS_BUS_NAME,
    WATCHER_BUS_NAME,
)
# Wait between a name change and the check it triggers, in seconds, so a service that
# restarts (name lost, then regained) is checked once, in its final state.
RECHECK_DELAY_S = 0.5


@dataclass(frozen=True)
class DependencyStatus:
    """The outcome of checking one item."""

    # One of the `*_KEY` constants.
    key: str
    # Short name of the item.
    title: str
    # Whether the item is available.
    is_met: bool
    # One sentence saying what was found.
    detail: str
    # What provides the item, in the README's words.
    remedy: str


@dataclass(frozen=True)
class DependencyReport:
    """The outcome of one full check, in a fixed order."""

    statuses: tuple[DependencyStatus, ...]

    @property
    def missing(self) -> list[DependencyStatus]:
        """Return the items that are not available, in report order."""
        missing_statuses: list[DependencyStatus] = []

        for status in self.statuses:
            if not status.is_met:
                missing_statuses.append(status)

        return missing_statuses

    @property
    def all_met(self) -> bool:
        """Tell whether every checked item is available."""
        return not self.missing

    @property
    def missing_titles_text(self) -> str:
        """Return the titles of the missing items joined for a tooltip or a notification."""
        missing_titles: list[str] = []
        for status in self.missing:
            missing_titles.append(status.title)

        return ", ".join(missing_titles)


class DependencyChecker(QObject):
    """Checks what the app depends on and re-checks whenever one of the names changes hands."""

    # Emitted with the new `DependencyReport` when a re-check finds a different outcome.
    report_changed = Signal(object)

    def __init__(self, session_bus: MessageBus, parent: QObject | None = None) -> None:
        """Create the checker bound to the app's session bus; the system bus is opened in `start`.

        Args:
            session_bus: Connected session bus shared with the other clients.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._session_bus = session_bus
        self._system_bus: MessageBus | None = None
        self._report: DependencyReport | None = None
        self._is_recheck_pending = False

    @property
    def report(self) -> DependencyReport:
        """Return the latest report; empty before `start` has run."""
        if self._report is None:
            return DependencyReport(())

        return self._report

    async def start(self) -> DependencyReport:
        """Subscribe to name changes on both buses and run the first check.

        A system bus that cannot be reached is reported through the BlueZ item and
        does not stop the other checks.

        Returns:
            The first report.

        Raises:
            DBusRequestError: the session bus daemon refused a signal subscription.
        """
        try:
            self._system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as connect_failure:
            logger.warning("system bus not reachable: %s", connect_failure)

        # Signal subscriptions come first so a name that changes hands while the
        # first check runs is checked again.
        self._session_bus.add_message_handler(self._handle_message)
        for bus_name in WATCHED_SESSION_NAMES:
            await add_signal_match(self._session_bus, name_owner_changed_match_rule(bus_name))
        if self._system_bus is not None:
            self._system_bus.add_message_handler(self._handle_message)
            await add_signal_match(self._system_bus, name_owner_changed_match_rule(BLUEZ_BUS_NAME))

        report = await self._check()

        return report

    def stop(self) -> None:
        """Close the system bus connection; the shared session bus is left to its owner."""
        if self._system_bus is not None:
            self._system_bus.disconnect()
            self._system_bus = None

    async def _check(self) -> DependencyReport:
        """Run every check, store the report and announce it when it changed.

        Raises:
            DBusRequestError: a bus daemon refused a request.
        """
        activatable_names = await self._activatable_session_names()
        statuses = (
            await self._check_telephony(activatable_names),
            await self._check_bluez(),
            await self._check_obexd(activatable_names),
            await self._check_notifications(activatable_names),
            await self._check_tray(),
            _check_key_tones(),
        )
        report = DependencyReport(statuses)

        is_first_report = self._report is None
        has_changed = report != self._report
        self._report = report
        missing_text = report.missing_titles_text
        if missing_text:
            logger.warning("missing requirements: %s", missing_text)
        else:
            logger.info("every requirement is available")

        if has_changed and not is_first_report:
            self.report_changed.emit(report)

        return report

    async def _activatable_session_names(self) -> set[str]:
        """Return the names the session bus can start on demand; empty when it cannot say."""
        try:
            reply_body = await call_method(
                self._session_bus,
                DBUS_DAEMON_BUS_NAME,
                DBUS_DAEMON_PATH,
                DBUS_DAEMON_INTERFACE,
                "ListActivatableNames",
            )
        except DBusRequestError as request_error:
            logger.warning("activatable names could not be listed: %s", request_error)
            return set()

        return set(reply_body[0])

    async def _session_name_state(
        self, bus_name: str, activatable_names: set[str]
    ) -> tuple[bool, bool]:
        """Return (has an owner now, can be started on demand) for a session bus name."""
        has_owner = await name_has_owner(self._session_bus, bus_name)
        is_activatable = bus_name in activatable_names

        return has_owner, is_activatable

    async def _check_telephony(self, activatable_names: set[str]) -> DependencyStatus:
        """Check PipeWire's telephony service."""
        has_owner, is_activatable = await self._session_name_state(
            TELEPHONY_BUS_NAME, activatable_names
        )

        if has_owner:
            detail = TELEPHONY_RUNNING_DETAIL
        elif is_activatable:
            detail = TELEPHONY_ACTIVATABLE_DETAIL
        else:
            detail = TELEPHONY_MISSING_DETAIL
        is_met = has_owner or is_activatable

        return DependencyStatus(TELEPHONY_KEY, TELEPHONY_TITLE, is_met, detail, TELEPHONY_REMEDY)

    async def _check_bluez(self) -> DependencyStatus:
        """Check the Bluetooth daemon on the system bus."""
        if self._system_bus is None:
            return DependencyStatus(
                BLUEZ_KEY, BLUEZ_TITLE, False, SYSTEM_BUS_MISSING_DETAIL, BLUEZ_REMEDY
            )

        has_owner = await name_has_owner(self._system_bus, BLUEZ_BUS_NAME)
        detail = BLUEZ_RUNNING_DETAIL if has_owner else BLUEZ_MISSING_DETAIL

        return DependencyStatus(BLUEZ_KEY, BLUEZ_TITLE, has_owner, detail, BLUEZ_REMEDY)

    async def _check_obexd(self, activatable_names: set[str]) -> DependencyStatus:
        """Check obexd, which the bus normally starts on demand."""
        has_owner, is_activatable = await self._session_name_state(OBEX_BUS_NAME, activatable_names)

        if has_owner:
            detail = OBEXD_RUNNING_DETAIL
        elif is_activatable:
            detail = OBEXD_ACTIVATABLE_DETAIL
        else:
            detail = OBEXD_MISSING_DETAIL
        is_met = has_owner or is_activatable

        return DependencyStatus(OBEXD_KEY, OBEXD_TITLE, is_met, detail, OBEXD_REMEDY)

    async def _check_notifications(self, activatable_names: set[str]) -> DependencyStatus:
        """Check for a notification server and whether it supports buttons.

        Asking an activatable server for its capabilities starts it, which the app's
        notifier would do a moment later anyway.
        """
        has_owner, is_activatable = await self._session_name_state(
            NOTIFICATIONS_BUS_NAME, activatable_names
        )
        is_reachable = has_owner or is_activatable
        if not is_reachable:
            return DependencyStatus(
                NOTIFICATIONS_KEY,
                NOTIFICATIONS_TITLE,
                False,
                NOTIFICATIONS_MISSING_DETAIL,
                NOTIFICATIONS_REMEDY,
            )

        try:
            reply_body = await call_method(
                self._session_bus,
                NOTIFICATIONS_BUS_NAME,
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_INTERFACE,
                "GetCapabilities",
            )
        except DBusRequestError as request_error:
            logger.warning("notification server capabilities unknown: %s", request_error)
            return DependencyStatus(
                NOTIFICATIONS_KEY,
                NOTIFICATIONS_TITLE,
                False,
                NOTIFICATIONS_UNANSWERED_DETAIL,
                NOTIFICATIONS_REMEDY,
            )

        capabilities = list(reply_body[0])
        supports_actions = ACTIONS_CAPABILITY in capabilities
        detail = NOTIFICATIONS_OK_DETAIL if supports_actions else NOTIFICATIONS_NO_ACTIONS_DETAIL

        return DependencyStatus(
            NOTIFICATIONS_KEY, NOTIFICATIONS_TITLE, supports_actions, detail, NOTIFICATIONS_REMEDY
        )

    async def _check_tray(self) -> DependencyStatus:
        """Check for a StatusNotifierWatcher, which a tray provides."""
        has_owner = await name_has_owner(self._session_bus, WATCHER_BUS_NAME)
        detail = TRAY_RUNNING_DETAIL if has_owner else TRAY_MISSING_DETAIL

        return DependencyStatus(TRAY_KEY, TRAY_TITLE, has_owner, detail, TRAY_REMEDY)

    def _handle_message(self, message: Message) -> None:
        """Schedule a re-check when one of the watched names changes hands."""
        if not is_name_owner_changed(message):
            return None

        changed_name = message.body[0]
        is_watched = changed_name in WATCHED_SESSION_NAMES or changed_name == BLUEZ_BUS_NAME
        if is_watched:
            self._schedule_recheck()

        return None

    def _schedule_recheck(self) -> None:
        """Run the checks again after `RECHECK_DELAY_S`, once for a burst of changes."""
        if self._is_recheck_pending:
            return

        self._is_recheck_pending = True
        asyncio.get_event_loop().create_task(self._recheck_after_delay())

    async def _recheck_after_delay(self) -> None:
        """Wait out the burst, then re-check; a bus refusal is logged, not raised."""
        await asyncio.sleep(RECHECK_DELAY_S)
        self._is_recheck_pending = False

        try:
            await self._check()
        except DBusRequestError as request_error:
            logger.warning("requirements could not be re-checked: %s", request_error)


def _check_key_tones() -> DependencyStatus:
    """Check that libpulse's simple API loads, so the dialpad keys can sound."""
    is_available = libpulse_simple_is_available()
    detail = KEY_TONES_OK_DETAIL if is_available else KEY_TONES_MISSING_DETAIL

    return DependencyStatus(KEY_TONES_KEY, KEY_TONES_TITLE, is_available, detail, KEY_TONES_REMEDY)
