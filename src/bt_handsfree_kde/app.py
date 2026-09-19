"""Wires the D-Bus clients, the tray and the notifications together."""

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
from bt_handsfree_kde.call_window import ActiveCallWindow
from bt_handsfree_kde.notifications import (
    ANSWER_ACTION,
    HANGUP_ACTION,
    HOLD_ACTION,
    REJECT_ACTION,
    RESUME_ACTION,
    URGENCY_NORMAL,
    CallNotifier,
)
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


class HandsfreeApplication(QObject):
    """Top-level coordinator: owns the clients and the UI, and routes events between them."""

    def __init__(self, qt_application: QApplication, parent: QObject | None = None) -> None:
        """Create the UI immediately; D-Bus connections are made in `start`.

        Args:
            qt_application: The running Qt application, used to quit from the tray.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._qt_application = qt_application
        self._session_bus: MessageBus | None = None
        self._telephony: TelephonyClient | None = None
        self._notifier: CallNotifier | None = None
        self._phones = PhoneInfoClient(self)

        # Monotonic timestamp at which each call became active, keyed by call path.
        self._call_started_at: dict[str, float] = {}
        # Addresses of gateways seen so far, to announce connect and disconnect once each.
        self._known_gateway_addresses: set[str] = set()

        self._tray = HandsfreeTray(_application_icon(), self)
        self._call_window = ActiveCallWindow()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(CALL_STATUS_REFRESH_INTERVAL_MS)

        self._tray.quit_requested.connect(self._qt_application.quit)
        self._tray.answer_requested.connect(self._answer_call)
        self._tray.reject_requested.connect(self._hangup_call)
        self._tray.hangup_requested.connect(self._hangup_call)
        self._tray.hold_requested.connect(self._toggle_hold_on_gateway)
        self._tray.audio_on_computer_toggled.connect(self._set_audio_on_computer)
        self._tray.speaker_volume_requested.connect(self._set_speaker_volume)
        self._tray.microphone_volume_requested.connect(self._set_microphone_volume)
        self._call_window.hold_requested.connect(self._toggle_hold_for_call)
        self._call_window.hangup_requested.connect(self._hangup_call)
        self._status_timer.timeout.connect(self._refresh_call_presentations)
        self._phones.phone_info_changed.connect(self._on_phone_info_changed)

    async def start(self) -> None:
        """Connect to both buses, start the clients and show the initial state."""
        self._session_bus = await MessageBus(bus_type=BusType.SESSION).connect()

        self._telephony = TelephonyClient(self._session_bus, self)
        self._telephony.availability_changed.connect(self._on_availability_changed)
        self._telephony.gateways_changed.connect(self._on_gateways_changed)
        self._telephony.call_added.connect(self._on_call_added)
        self._telephony.call_changed.connect(self._on_call_changed)
        self._telephony.call_removed.connect(self._on_call_removed)

        self._notifier = CallNotifier(self._session_bus, self)
        self._notifier.action_invoked.connect(self._on_notification_action)

        # The notifier and BlueZ come up before telephony so the very first call and
        # gateway events can already be presented with names, battery and notifications.
        await self._notifier.start()
        await self._phones.start()
        await self._telephony.start()

        self._refresh_tray()

    def _on_availability_changed(self, is_available: bool) -> None:
        """Redraw the tray and tell the user when the telephony service goes away."""
        self._refresh_tray()

        if not is_available:
            self._run(
                self._notifier.show_information(
                    "Telephony service unavailable",
                    "PipeWire's telephony service is not running; calls cannot be controlled.",
                    URGENCY_NORMAL,
                )
            )

    def _on_gateways_changed(self) -> None:
        """Redraw the tray and announce phones that connected or disconnected over HFP."""
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

        self._refresh_tray()

    def _on_call_added(self, call: Call) -> None:
        """Present a new call and start the status refresh timer."""
        self._note_call_start(call)
        self._present_call(call)
        self._status_timer.start()
        self._refresh_tray()

    def _on_call_changed(self, call: Call) -> None:
        """Present a call whose state changed."""
        self._note_call_start(call)
        self._present_call(call)
        self._refresh_tray()

    def _on_call_removed(self, call_path: str) -> None:
        """Drop everything shown for a finished call."""
        self._call_started_at.pop(call_path, None)
        self._call_window.hide_call(call_path)
        self._run(self._notifier.close_for_call(call_path))

        remaining_calls = self._telephony.calls
        if not remaining_calls:
            self._status_timer.stop()

        self._refresh_tray()

    def _on_phone_info_changed(self, phone_info: PhoneInfo) -> None:
        """Redraw the tray when a phone that has a gateway changes name, state or battery."""
        address_key = phone_info.address.upper()
        if address_key in self._known_gateway_addresses:
            self._refresh_tray()

    def _on_notification_action(self, call_path: str, action_key: str) -> None:
        """Route a pressed notification button to the matching call action."""
        if action_key == ANSWER_ACTION:
            self._answer_call(call_path)
        elif action_key in (REJECT_ACTION, HANGUP_ACTION):
            self._hangup_call(call_path)
        elif action_key in (HOLD_ACTION, RESUME_ACTION):
            self._toggle_hold_for_call(call_path)

    def _refresh_call_presentations(self) -> None:
        """Timer tick: refresh the duration shown for every call in progress."""
        for call in self._telephony.calls:
            if not call.is_incoming:
                self._present_call(call)

    def _present_call(self, call: Call) -> None:
        """Show the right notification or window for `call` in its current state."""
        if call.state == CALL_STATE_DISCONNECTED:
            self._call_window.hide_call(call.path)
            self._run(self._notifier.close_for_call(call.path))
            return

        if call.is_incoming:
            self._call_window.hide_call(call.path)
            if self._notifier.supports_call_notifications:
                self._run(self._notifier.show_incoming_call(call.path, call.caller_label))
            else:
                self._run(
                    self._notifier.show_information(
                        "Incoming call", call.caller_label, URGENCY_NORMAL
                    )
                )
            return

        status_text = self._status_text(call)
        is_held = call.state == CALL_STATE_HELD
        if self._notifier.supports_call_notifications:
            self._run(
                self._notifier.show_active_call(call.path, call.caller_label, status_text, is_held)
            )
        else:
            self._call_window.show_call(call.path, call.caller_label, status_text, is_held)

    def _status_text(self, call: Call) -> str:
        """Describe the progress of a non-incoming call: dialing, ringing or elapsed time."""
        if call.state == CALL_STATE_DIALING:
            return "Dialing…"

        if call.state == CALL_STATE_ALERTING:
            return "Ringing…"

        started_at = self._call_started_at.get(call.path)
        if started_at is None:
            return call.state

        elapsed_seconds = int(time.monotonic() - started_at)
        duration_text = _format_duration(elapsed_seconds)
        if call.state == CALL_STATE_HELD:
            return f"On hold · {duration_text}"

        return duration_text

    def _note_call_start(self, call: Call) -> None:
        """Remember when a call first became active so its duration can be shown."""
        is_first_activation = (
            call.state == CALL_STATE_ACTIVE and call.path not in self._call_started_at
        )
        if is_first_activation:
            self._call_started_at[call.path] = time.monotonic()

    def _refresh_tray(self) -> None:
        """Push the combined telephony and BlueZ state to the tray."""
        phone_info_by_address: dict[str, PhoneInfo] = {}
        for gateway in self._telephony.gateways:
            phone_info = self._phones.info_for_address(gateway.address)
            if phone_info is not None:
                address_key = gateway.address.upper()
                phone_info_by_address[address_key] = phone_info

        self._tray.update_state(
            self._telephony.is_available,
            self._telephony.gateways,
            self._telephony.calls,
            phone_info_by_address,
        )

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
        """Schedule `coroutine` on the event loop and surface a failure in the tray."""
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
            self._tray.show_error(str(failure))


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
