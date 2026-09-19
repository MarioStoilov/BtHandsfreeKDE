"""System tray icon and its dropdown menu."""

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.bluez import PhoneInfo
from bt_handsfree_kde.telephony import (
    CALL_STATE_ACTIVE,
    CALL_STATE_HELD,
    MAX_VOLUME_LEVEL,
    MIN_VOLUME_LEVEL,
    OUTGOING_PENDING_STATES,
    AudioGateway,
    Call,
)

# Theme icon names for the tray, from the freedesktop icon naming specification. Each
# falls back to the next one, and finally to the bundled application icon.
INCOMING_CALL_ICON_NAME = "call-incoming"
ACTIVE_CALL_ICON_NAME = "call-start"
# Text shown when PipeWire's telephony service is missing.
SERVICE_UNAVAILABLE_TEXT = "PipeWire telephony service not available"
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"


class HandsfreeTray(QObject):
    """Owns the tray icon and rebuilds its menu from the current call and phone state."""

    # Call actions; the argument is the call's object path.
    answer_requested = Signal(str)
    reject_requested = Signal(str)
    hangup_requested = Signal(str)
    # Hold / resume acts on the gateway; the argument is the gateway's object path.
    hold_requested = Signal(str)
    # Gateway settings: (gateway path, new value).
    audio_on_computer_toggled = Signal(str, bool)
    speaker_volume_requested = Signal(str, int)
    microphone_volume_requested = Signal(str, int)
    quit_requested = Signal()

    def __init__(self, fallback_icon: QIcon, parent: QObject | None = None) -> None:
        """Create and show the tray icon with an empty menu.

        Args:
            fallback_icon: Icon used when the theme lacks the call icons and when idle.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._fallback_icon = fallback_icon
        self._is_service_available = False
        self._gateways: list[AudioGateway] = []
        self._calls: list[Call] = []
        self._phone_info_by_address: dict[str, PhoneInfo] = {}

        self._menu = QMenu()
        self._tray_icon = QSystemTrayIcon(fallback_icon, self)
        self._tray_icon.setToolTip(APPLICATION_NAME)
        self._tray_icon.setContextMenu(self._menu)
        self._tray_icon.activated.connect(self._on_activated)

        self._rebuild_menu()
        self._tray_icon.show()

    def update_state(
        self,
        is_service_available: bool,
        gateways: list[AudioGateway],
        calls: list[Call],
        phone_info_by_address: dict[str, PhoneInfo],
    ) -> None:
        """Replace the displayed state and redraw icon, tooltip and menu.

        Args:
            is_service_available: Whether the telephony service is on the bus.
            gateways: Connected phones as seen by the telephony service.
            calls: Current calls across all gateways.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
        """
        self._is_service_available = is_service_available
        self._gateways = list(gateways)
        self._calls = list(calls)
        self._phone_info_by_address = dict(phone_info_by_address)

        self._rebuild_menu()
        self._update_icon_and_tooltip()

    def show_error(self, text: str) -> None:
        """Show a transient warning balloon from the tray icon."""
        self._tray_icon.showMessage(APPLICATION_NAME, text, QSystemTrayIcon.MessageIcon.Warning)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Open the menu on a plain click; the right-click context menu is handled by Qt."""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            cursor_position = QCursor.pos()
            self._menu.popup(cursor_position)

    def _rebuild_menu(self) -> None:
        """Recreate every menu entry from the stored state."""
        self._menu.clear()

        if not self._is_service_available:
            self._add_label(self._menu, SERVICE_UNAVAILABLE_TEXT)
            self._menu.addSeparator()
            self._add_action(self._menu, "Quit", self.quit_requested.emit)
            return

        if not self._gateways:
            self._add_label(self._menu, NO_PHONE_TEXT)
            self._menu.addSeparator()

        for gateway in self._gateways:
            header_text = self._phone_header(gateway)
            self._add_label(self._menu, header_text)

            for call in self._calls:
                if call.gateway_path == gateway.path:
                    self._add_call_entries(call)

            self._add_gateway_settings(gateway)
            self._menu.addSeparator()

        self._add_action(self._menu, "Quit", self.quit_requested.emit)

    def _add_call_entries(self, call: Call) -> None:
        """Add the description and the applicable buttons for one call."""
        if call.is_incoming:
            self._add_label(self._menu, f"Incoming: {call.caller_label}")
            self._add_action(self._menu, "Answer", partial(self.answer_requested.emit, call.path))
            self._add_action(self._menu, "Reject", partial(self.reject_requested.emit, call.path))
            return

        if call.state in OUTGOING_PENDING_STATES:
            self._add_label(self._menu, f"Calling: {call.caller_label}")
        elif call.state == CALL_STATE_HELD:
            self._add_label(self._menu, f"On hold: {call.caller_label}")
            self._add_action(
                self._menu, "Resume", partial(self.hold_requested.emit, call.gateway_path)
            )
        elif call.state == CALL_STATE_ACTIVE:
            self._add_label(self._menu, f"In call: {call.caller_label}")
            self._add_action(
                self._menu, "Hold", partial(self.hold_requested.emit, call.gateway_path)
            )
        else:
            self._add_label(self._menu, f"{call.state}: {call.caller_label}")

        self._add_action(self._menu, "Hang up", partial(self.hangup_requested.emit, call.path))

    def _add_gateway_settings(self, gateway: AudioGateway) -> None:
        """Add the audio routing toggle and the two volume submenus for one gateway."""
        audio_on_computer = not gateway.reject_sco
        audio_action = QAction("Call audio on this computer", self._menu)
        audio_action.setCheckable(True)
        audio_action.setChecked(audio_on_computer)
        audio_action.toggled.connect(partial(self.audio_on_computer_toggled.emit, gateway.path))
        self._menu.addAction(audio_action)

        self._add_volume_submenu(
            "Speaker volume", gateway.path, gateway.speaker_volume, self.speaker_volume_requested
        )
        self._add_volume_submenu(
            "Microphone volume",
            gateway.path,
            gateway.microphone_volume,
            self.microphone_volume_requested,
        )

    def _add_volume_submenu(
        self, title: str, gateway_path: str, current_level: int, level_signal: Signal
    ) -> None:
        """Add a submenu showing the current level with Louder and Quieter entries."""
        volume_menu = self._menu.addMenu(title)
        level_text = f"Level {current_level} / {MAX_VOLUME_LEVEL}"
        self._add_label(volume_menu, level_text)

        louder_level = current_level + 1
        quieter_level = current_level - 1
        can_go_louder = current_level < MAX_VOLUME_LEVEL
        can_go_quieter = current_level > MIN_VOLUME_LEVEL

        louder_action = self._add_action(
            volume_menu, "Louder", partial(level_signal.emit, gateway_path, louder_level)
        )
        louder_action.setEnabled(can_go_louder)
        quieter_action = self._add_action(
            volume_menu, "Quieter", partial(level_signal.emit, gateway_path, quieter_level)
        )
        quieter_action.setEnabled(can_go_quieter)

    def _phone_header(self, gateway: AudioGateway) -> str:
        """Describe a gateway as phone name plus battery, falling back to its address."""
        address_key = gateway.address.upper()
        phone_info = self._phone_info_by_address.get(address_key)

        if phone_info is None or not phone_info.alias:
            return gateway.address

        if phone_info.battery_percentage is None:
            return phone_info.alias

        return f"{phone_info.alias} · {phone_info.battery_percentage} %"

    def _update_icon_and_tooltip(self) -> None:
        """Pick the icon for the current call state and summarise the state in the tooltip."""
        has_incoming_call = any(call.is_incoming for call in self._calls)
        has_any_call = bool(self._calls)

        active_icon = QIcon.fromTheme(ACTIVE_CALL_ICON_NAME, self._fallback_icon)
        if has_incoming_call:
            icon = QIcon.fromTheme(INCOMING_CALL_ICON_NAME, active_icon)
        elif has_any_call:
            icon = active_icon
        else:
            icon = self._fallback_icon
        self._tray_icon.setIcon(icon)

        tooltip_lines = [APPLICATION_NAME]
        if not self._is_service_available:
            tooltip_lines.append(SERVICE_UNAVAILABLE_TEXT)
        elif not self._gateways:
            tooltip_lines.append(NO_PHONE_TEXT)
        for gateway in self._gateways:
            header_text = self._phone_header(gateway)
            tooltip_lines.append(header_text)
        for call in self._calls:
            tooltip_lines.append(f"{call.state}: {call.caller_label}")

        tooltip_text = "\n".join(tooltip_lines)
        self._tray_icon.setToolTip(tooltip_text)

    @staticmethod
    def _add_label(menu: QMenu, text: str) -> QAction:
        """Add a non-clickable entry used as a heading or status line."""
        label_action = menu.addAction(text)
        label_action.setEnabled(False)

        return label_action

    @staticmethod
    def _add_action(menu: QMenu, text: str, handler: Callable[[], None]) -> QAction:
        """Add a clickable entry that calls `handler` without arguments."""
        action = menu.addAction(text)
        action.triggered.connect(lambda _checked=False: handler())

        return action
