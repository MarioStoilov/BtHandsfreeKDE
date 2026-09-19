"""Wires the D-Bus clients, the tray, the windows and the notifications together."""

import asyncio
import logging
import time
from collections.abc import Coroutine
from importlib import resources
from typing import Any

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from bt_handsfree_kde import APPLICATION_ID
from bt_handsfree_kde.bluez import PhoneInfo, PhoneInfoClient
from bt_handsfree_kde.call_window import CallWindow
from bt_handsfree_kde.notifications import (
    ANSWER_ACTION,
    REJECT_ACTION,
    URGENCY_NORMAL,
    CallNotifier,
)
from bt_handsfree_kde.settings_window import SettingsWindow
from bt_handsfree_kde.telephony import (
    CALL_STATE_ACTIVE,
    CALL_STATE_ALERTING,
    CALL_STATE_DIALING,
    CALL_STATE_DISCONNECTED,
    CALL_STATE_HELD,
    Call,
    TelephonyClient,
)
from bt_handsfree_kde.tray import HandsfreeTray

logger = logging.getLogger(__name__)

# How often the duration shown for a call in progress is refreshed.
CALL_STATUS_REFRESH_INTERVAL_MS = 1000
# Package-relative location of the bundled application icon.
BUNDLED_ICON_RESOURCE = "resources/icon.svg"
# Which call the window shows when several exist: lower rank wins.
CALL_WINDOW_PRIORITY = {
    CALL_STATE_ACTIVE: 0,
    CALL_STATE_DIALING: 1,
    CALL_STATE_ALERTING: 1,
    CALL_STATE_HELD: 2,
}
# Rank for states not listed above (incoming calls and anything unexpected).
LOWEST_CALL_PRIORITY = 3


class HandsfreeApplication(QObject):
    """Top-level coordinator: owns the clients and the UI, and routes events between them."""

    def __init__(self, qt_application: QApplication, parent: QObject | None = None) -> None:
        """Create the windows immediately; D-Bus connections are made in `start`.

        Args:
            qt_application: The running Qt application, used to quit from the tray.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._qt_application = qt_application
        self._qt_application.setWindowIcon(_application_icon())
        self._session_bus: MessageBus | None = None
        self._telephony: TelephonyClient | None = None
        self._notifier: CallNotifier | None = None
        self._tray: HandsfreeTray | None = None
        self._phones = PhoneInfoClient(self)

        # Monotonic timestamp at which each call became active, keyed by call path.
        self._call_started_at: dict[str, float] = {}
        # Addresses of gateways seen so far, to announce connect and disconnect once each.
        self._known_gateway_addresses: set[str] = set()
        # Call the user picked from the menu; shown in the call window while it exists.
        self._focused_call_path = ""

        self._call_window = CallWindow()
        self._settings_window = SettingsWindow()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(CALL_STATUS_REFRESH_INTERVAL_MS)

        self._call_window.answer_requested.connect(self._answer_call)
        self._call_window.hangup_requested.connect(self._hangup_call)
        self._call_window.hold_requested.connect(self._toggle_hold_for_call)
        self._call_window.tone_requested.connect(self._send_tone)
        self._settings_window.speaker_volume_changed.connect(self._set_speaker_volume)
        self._settings_window.microphone_volume_changed.connect(self._set_microphone_volume)
        self._settings_window.audio_on_computer_changed.connect(self._set_audio_on_computer)
        self._status_timer.timeout.connect(self._on_status_tick)
        self._phones.phone_info_changed.connect(self._on_phone_info_changed)

    async def start(self) -> None:
        """Connect to both buses, start the clients and the tray, and show the initial state."""
        self._session_bus = await MessageBus(bus_type=BusType.SESSION).connect()

        self._telephony = TelephonyClient(self._session_bus, self)
        self._telephony.availability_changed.connect(self._on_availability_changed)
        self._telephony.gateways_changed.connect(self._on_gateways_changed)
        self._telephony.call_added.connect(self._on_call_added)
        self._telephony.call_changed.connect(self._on_call_changed)
        self._telephony.call_removed.connect(self._on_call_removed)

        self._notifier = CallNotifier(self._session_bus, self)
        self._notifier.action_invoked.connect(self._on_notification_action)

        self._tray = HandsfreeTray(self._session_bus, _application_icon(), self)
        self._tray.quit_requested.connect(self._qt_application.quit)
        self._tray.answer_requested.connect(self._answer_call)
        self._tray.reject_requested.connect(self._hangup_call)
        self._tray.hangup_requested.connect(self._hangup_call)
        self._tray.hold_requested.connect(self._toggle_hold_on_gateway)
        self._tray.audio_on_computer_toggled.connect(self._set_audio_on_computer)
        self._tray.settings_requested.connect(self._show_settings)
        self._tray.call_focus_requested.connect(self._focus_call)

        # The notifier, BlueZ and the tray come up before telephony so the very first
        # call and gateway events can already be presented with names and battery.
        await self._notifier.start()
        await self._phones.start()
        await self._tray.start()
        await self._telephony.start()

        self._refresh_views()

    def _on_availability_changed(self, is_available: bool) -> None:
        """Redraw and tell the user when the telephony service goes away."""
        self._refresh_views()

        if not is_available:
            self._run(
                self._notifier.show_information(
                    "Telephony service unavailable",
                    "PipeWire's telephony service is not running; calls cannot be controlled.",
                    URGENCY_NORMAL,
                )
            )

    def _on_gateways_changed(self) -> None:
        """Redraw and announce phones that connected or disconnected over HFP."""
        current_addresses: set[str] = set()
        for gateway in self._telephony.gateways:
            current_addresses.add(gateway.address.upper())

        newly_connected = current_addresses - self._known_gateway_addresses
        newly_disconnected = self._known_gateway_addresses - current_addresses
        self._known_gateway_addresses = current_addresses

        for address in newly_connected:
            phone_label = self._phone_label(address)
            self._run(self._notifier.show_information("Phone connected", phone_label))
        for address in newly_disconnected:
            phone_label = self._phone_label(address)
            self._run(self._notifier.show_information("Phone disconnected", phone_label))

        self._refresh_views()

    def _on_call_added(self, call: Call) -> None:
        """Present a new call and start the status refresh timer."""
        self._note_call_start(call)
        self._present_calls()
        self._status_timer.start()
        self._refresh_views()

    def _on_call_changed(self, call: Call) -> None:
        """Present the calls again after one changed state."""
        self._note_call_start(call)
        self._present_calls()
        self._refresh_views()

    def _on_call_removed(self, call_path: str) -> None:
        """Drop everything shown for a finished call."""
        self._call_started_at.pop(call_path, None)
        if self._focused_call_path == call_path:
            self._focused_call_path = ""
        self._run(self._notifier.close_for_call(call_path))
        self._present_calls()

        remaining_calls = self._telephony.calls
        if not remaining_calls:
            self._status_timer.stop()

        self._refresh_views()

    def _on_phone_info_changed(self, phone_info: PhoneInfo) -> None:
        """Redraw when a phone that has a gateway changes name, state or battery."""
        address_key = phone_info.address.upper()
        if address_key in self._known_gateway_addresses:
            self._refresh_views()

    def _on_notification_action(self, call_path: str, action_key: str) -> None:
        """Route a pressed notification button to the matching call action."""
        if action_key == ANSWER_ACTION:
            self._answer_call(call_path)
        elif action_key == REJECT_ACTION:
            self._hangup_call(call_path)

    def _on_status_tick(self) -> None:
        """Timer tick: refresh the durations in the call window and the menu."""
        self._present_calls()
        self._refresh_tray()

    def _present_calls(self) -> None:
        """Show the right notification and window content for the current calls.

        Ringing calls get a notification with Answer and Reject when the server supports
        buttons; otherwise the call window shows them. Every other call is shown in the
        call window, the most relevant one when there are several, unless the user
        picked one from the menu.
        """
        live_calls: list[Call] = []
        for call in self._telephony.calls:
            if call.state == CALL_STATE_DISCONNECTED:
                self._run(self._notifier.close_for_call(call.path))
            else:
                live_calls.append(call)

        notifications_supported = self._notifier.supports_call_notifications
        window_candidates: list[Call] = []
        for call in live_calls:
            is_focused = call.path == self._focused_call_path
            if call.is_incoming and notifications_supported:
                self._run(self._notifier.show_incoming_call(call.path, call.caller_label))
                if is_focused:
                    window_candidates.append(call)
            else:
                self._run(self._notifier.close_for_call(call.path))
                window_candidates.append(call)

        if not window_candidates:
            self._call_window.hide_call()
            return

        window_candidates.sort(key=self._call_window_rank)
        shown_call = window_candidates[0]
        status_text = self._progress_text(shown_call)
        if shown_call.state == CALL_STATE_HELD:
            status_text = f"On hold · {status_text}"
        self._call_window.show_call(shown_call, shown_call.caller_label, status_text)

    def _call_window_rank(self, call: Call) -> int:
        """Sort key choosing which call the window shows: the focused one, then by state."""
        if call.path == self._focused_call_path:
            return -1

        return CALL_WINDOW_PRIORITY.get(call.state, LOWEST_CALL_PRIORITY)

    def _focus_call(self, call_path: str) -> None:
        """Show `call_path` in the call window and bring the window to the front."""
        self._focused_call_path = call_path
        self._present_calls()
        self._call_window.raise_()
        self._call_window.activateWindow()

    def _progress_text(self, call: Call) -> str:
        """Describe the progress of a call: ringing, dialing or elapsed time."""
        if call.is_incoming:
            return "Incoming call"

        if call.state == CALL_STATE_DIALING:
            return "Dialing…"

        if call.state == CALL_STATE_ALERTING:
            return "Ringing…"

        started_at = self._call_started_at.get(call.path)
        if started_at is None:
            return call.state

        elapsed_seconds = int(time.monotonic() - started_at)
        duration_text = _format_duration(elapsed_seconds)

        return duration_text

    def _note_call_start(self, call: Call) -> None:
        """Remember when a call first became active so its duration can be shown."""
        is_first_activation = (
            call.state == CALL_STATE_ACTIVE and call.path not in self._call_started_at
        )
        if is_first_activation:
            self._call_started_at[call.path] = time.monotonic()

    def _refresh_views(self) -> None:
        """Push the combined telephony and BlueZ state to the tray and the settings window."""
        self._refresh_tray()
        self._refresh_settings()

    def _phone_info_by_address(self) -> dict[str, PhoneInfo]:
        """Collect BlueZ details for every gateway, keyed by upper-case address."""
        phone_info_by_address: dict[str, PhoneInfo] = {}

        for gateway in self._telephony.gateways:
            phone_info = self._phones.info_for_address(gateway.address)
            if phone_info is not None:
                address_key = gateway.address.upper()
                phone_info_by_address[address_key] = phone_info

        return phone_info_by_address

    def _refresh_tray(self) -> None:
        """Redraw the tray icon and menu, including per-call progress texts."""
        progress_text_by_call_path: dict[str, str] = {}
        for call in self._telephony.calls:
            progress_text_by_call_path[call.path] = self._progress_text(call)

        phone_info_by_address = self._phone_info_by_address()
        self._tray.update_state(
            self._telephony.is_available,
            self._telephony.gateways,
            self._telephony.calls,
            phone_info_by_address,
            progress_text_by_call_path,
        )

    def _refresh_settings(self) -> None:
        """Rebuild the settings window for the current gateways."""
        phone_info_by_address = self._phone_info_by_address()
        self._settings_window.update_gateways(self._telephony.gateways, phone_info_by_address)

    def _show_settings(self) -> None:
        """Open or raise the settings window."""
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _phone_label(self, address: str) -> str:
        """Return the phone's BlueZ alias, or its address when BlueZ has no entry."""
        phone_info = self._phones.info_for_address(address)
        if phone_info is None or not phone_info.alias:
            return address

        return phone_info.alias

    def _answer_call(self, call_path: str) -> None:
        """Answer `call_path`, holding an active call first when the phone has one."""
        call = self._telephony.call_for(call_path)
        if call is None:
            return

        has_active_call_on_gateway = False
        for other_call in self._telephony.calls:
            is_other_active = (
                other_call.gateway_path == call.gateway_path
                and other_call.path != call_path
                and other_call.state == CALL_STATE_ACTIVE
            )
            if is_other_active:
                has_active_call_on_gateway = True

        if has_active_call_on_gateway:
            self._run(self._telephony.hold_and_answer(call.gateway_path))
        else:
            self._run(self._telephony.answer(call_path))

    def _hangup_call(self, call_path: str) -> None:
        """End or reject `call_path`."""
        self._run(self._telephony.hangup(call_path))

    def _toggle_hold_for_call(self, call_path: str) -> None:
        """Hold or resume the call at `call_path` through its gateway."""
        call = self._telephony.call_for(call_path)
        if call is not None:
            self._toggle_hold_on_gateway(call.gateway_path)

    def _toggle_hold_on_gateway(self, gateway_path: str) -> None:
        """Swap active and held calls on `gateway_path`; with one call this toggles hold."""
        self._run(self._telephony.swap_calls(gateway_path))

    def _send_tone(self, call_path: str, tone: str) -> None:
        """Send one DTMF tone on the gateway that carries `call_path`."""
        call = self._telephony.call_for(call_path)
        if call is not None:
            self._run(self._telephony.send_tones(call.gateway_path, tone))

    def _set_audio_on_computer(self, gateway_path: str, audio_on_computer: bool) -> None:
        """Route call audio to this computer or keep it on the phone."""
        self._run(self._telephony.set_audio_on_computer(gateway_path, audio_on_computer))

    def _set_speaker_volume(self, gateway_path: str, level: int) -> None:
        """Change the phone-side speaker gain."""
        self._run(self._telephony.set_speaker_volume(gateway_path, level))

    def _set_microphone_volume(self, gateway_path: str, level: int) -> None:
        """Change the phone-side microphone gain."""
        self._run(self._telephony.set_microphone_volume(gateway_path, level))

    def _run(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Schedule `coroutine` on the event loop and surface a failure as a notification."""
        event_loop = asyncio.get_event_loop()
        task = event_loop.create_task(coroutine)
        task.add_done_callback(self._report_task_outcome)

    def _report_task_outcome(self, task: asyncio.Task[Any]) -> None:
        """Log and show the error of a background action that failed."""
        if task.cancelled():
            return

        failure = task.exception()
        if failure is not None:
            logger.error("action failed: %s", failure)
            self._run(
                self._notifier.show_information("Action failed", str(failure), URGENCY_NORMAL)
            )


def _application_icon() -> QIcon:
    """Return the installed theme icon for the app, falling back to the bundled SVG."""
    icon_resource = resources.files("bt_handsfree_kde") / BUNDLED_ICON_RESOURCE
    bundled_icon = QIcon(str(icon_resource))

    return QIcon.fromTheme(APPLICATION_ID, bundled_icon)


def _format_duration(elapsed_seconds: int) -> str:
    """Format a duration as `MM:SS`, or `H:MM:SS` once it passes an hour."""
    hours, remainder_seconds = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder_seconds, 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"
