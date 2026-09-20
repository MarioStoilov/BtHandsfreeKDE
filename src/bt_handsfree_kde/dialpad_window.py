"""Dialpad window: type or paste a number and call it; the keys send DTMF during a call."""

from PySide6.QtCore import QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.bluez import PhoneInfo
from bt_handsfree_kde.dtmf import DtmfTonePlayer
from bt_handsfree_kde.keypad import build_keypad, keypad_font
from bt_handsfree_kde.phone_numbers import NUMBER_FIELD_PATTERN, dial_string_from_text
from bt_handsfree_kde.telephony import AudioGateway

# Theme icon names for the buttons (all present in Breeze).
CALL_ICON = "call-start"
BACKSPACE_ICON = "edit-clear-locationbar-rtl"
# Pixel size of the Call button icon.
CALL_ICON_SIZE = 24
# Text shown when PipeWire's telephony service is missing.
SERVICE_UNAVAILABLE_TEXT = "PipeWire telephony service not available"
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"
# Text shown in the empty number field.
NUMBER_PLACEHOLDER_TEXT = "Enter number"


class DialpadWindow(QWidget):
    """Places calls from a typed number; while the chosen phone is in a call, keys send DTMF.

    The phone chooser is shown only when several phones are connected. Each key press
    plays the key's tone locally and inserts the key into the number field; when the
    selected phone has an active call the key is also sent to it as a DTMF tone, the
    way a phone's in-call dialpad behaves, and the status line says so.
    """

    # Emitted with (gateway path, dial string) when the user asks to call.
    dial_requested = Signal(str, str)
    # Emitted with (gateway path, single DTMF tone) for key presses during an active call.
    tone_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the widgets; `update_state` fills in the phones."""
        super().__init__(parent)
        self.setWindowTitle(f"{APPLICATION_NAME} dialpad")
        self._tone_player = DtmfTonePlayer()
        self._is_service_available = False
        self._active_call_label_by_gateway_path: dict[str, str] = {}
        # (gateway path, label) pairs the chooser lists, so it is rebuilt only on change.
        self._chooser_entries: list[tuple[str, str]] = []

        self._phone_chooser = QComboBox(self)
        self._phone_chooser.setVisible(False)

        number_font = keypad_font()
        self._number_field = QLineEdit(self)
        self._number_field.setFont(number_font)
        self._number_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._number_field.setPlaceholderText(NUMBER_PLACEHOLDER_TEXT)
        number_pattern = QRegularExpression(NUMBER_FIELD_PATTERN)
        self._number_field.setValidator(QRegularExpressionValidator(number_pattern, self))
        self._backspace_button = QToolButton(self)
        self._backspace_button.setIcon(QIcon.fromTheme(BACKSPACE_ICON))
        self._backspace_button.setToolTip("Delete the last character")
        self._backspace_button.setAutoRaise(True)

        number_row = QHBoxLayout()
        number_row.addWidget(self._number_field)
        number_row.addWidget(self._backspace_button)

        key_grid = build_keypad(self, self._press_key)

        self._call_button = QPushButton(QIcon.fromTheme(CALL_ICON), "Call", self)
        self._call_button.setIconSize(QSize(CALL_ICON_SIZE, CALL_ICON_SIZE))
        self._call_button.setDefault(True)
        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)

        column = QVBoxLayout(self)
        column.addWidget(self._phone_chooser)
        column.addLayout(number_row)
        column.addLayout(key_grid)
        column.addWidget(self._call_button)
        column.addWidget(self._status_label)

        self._number_field.textChanged.connect(self._update_call_button)
        self._number_field.returnPressed.connect(self._emit_dial)
        self._backspace_button.clicked.connect(self._number_field.backspace)
        self._call_button.clicked.connect(self._emit_dial)
        self._phone_chooser.currentIndexChanged.connect(self._on_phone_selected)

        self._update_status()

    @property
    def selected_gateway_path(self) -> str:
        """Return the object path of the phone the window acts on; empty without phones."""
        current_data = self._phone_chooser.currentData()
        if current_data is None:
            return ""

        return str(current_data)

    def set_number(self, number: str) -> None:
        """Replace the number field's content and put the cursor after it."""
        self._number_field.setText(number)
        self._number_field.end(False)
        self._number_field.setFocus()

    def update_state(
        self,
        is_service_available: bool,
        gateways: list[AudioGateway],
        phone_info_by_address: dict[str, PhoneInfo],
        active_call_label_by_gateway_path: dict[str, str],
    ) -> None:
        """Replace what the window knows about the service, the phones and their calls.

        The chooser keeps its selection when the same phones are still connected.

        Args:
            is_service_available: Whether the telephony service is on the bus.
            gateways: Connected phones as seen by the telephony service.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
            active_call_label_by_gateway_path: Caller label of the active call per gateway
                path; gateways without an active call are absent.
        """
        self._is_service_available = is_service_available
        self._active_call_label_by_gateway_path = dict(active_call_label_by_gateway_path)

        chooser_entries: list[tuple[str, str]] = []
        for gateway in gateways:
            phone_label = _phone_label(gateway, phone_info_by_address)
            chooser_entries.append((gateway.path, phone_label))

        entries_changed = chooser_entries != self._chooser_entries
        if entries_changed:
            self._rebuild_chooser(chooser_entries)

        has_several_phones = len(chooser_entries) > 1
        self._phone_chooser.setVisible(has_several_phones)
        self._update_status()

    def _rebuild_chooser(self, chooser_entries: list[tuple[str, str]]) -> None:
        """Refill the phone chooser, keeping the selected phone when it is still listed."""
        previously_selected_path = self.selected_gateway_path

        self._phone_chooser.clear()
        for gateway_path, phone_label in chooser_entries:
            self._phone_chooser.addItem(phone_label, gateway_path)

        previous_index = self._phone_chooser.findData(previously_selected_path)
        if previous_index >= 0:
            self._phone_chooser.setCurrentIndex(previous_index)
        self._chooser_entries = list(chooser_entries)

    def _on_phone_selected(self, _selected_index: int) -> None:
        """Refresh the status line and the Call button for the newly selected phone."""
        self._update_status()

    def _update_status(self) -> None:
        """Explain why calling is impossible, or that keys go to a call, and update the button."""
        gateway_path = self.selected_gateway_path
        active_call_label = self._active_call_label_by_gateway_path.get(gateway_path)

        if not self._is_service_available:
            status_text = SERVICE_UNAVAILABLE_TEXT
        elif not self._chooser_entries:
            status_text = NO_PHONE_TEXT
        elif active_call_label is not None:
            status_text = f"Keys send tones to {active_call_label}"
        else:
            status_text = ""

        self._status_label.setText(status_text)
        self._update_call_button()

    def _update_call_button(self) -> None:
        """Enable Call only when a phone is selected and the field holds something dialable."""
        dial_string = dial_string_from_text(self._number_field.text())
        has_number = bool(dial_string)
        has_phone = bool(self.selected_gateway_path)
        can_dial = has_phone and has_number

        self._call_button.setEnabled(can_dial)

    def _press_key(self, key: str) -> None:
        """Play the key's tone, insert it into the number and send it as DTMF during a call."""
        self._tone_player.play(key)
        self._number_field.insert(key)

        gateway_path = self.selected_gateway_path
        has_active_call = gateway_path in self._active_call_label_by_gateway_path
        if has_active_call:
            self.tone_requested.emit(gateway_path, key)

    def _emit_dial(self) -> None:
        """Ask the application to call the number in the field from the selected phone."""
        gateway_path = self.selected_gateway_path
        dial_string = dial_string_from_text(self._number_field.text())
        has_number = bool(dial_string)
        has_phone = bool(gateway_path)

        if has_phone and has_number:
            self.dial_requested.emit(gateway_path, dial_string)


def _phone_label(gateway: AudioGateway, phone_info_by_address: dict[str, PhoneInfo]) -> str:
    """Return the phone's BlueZ alias, or its address when BlueZ has no entry."""
    address_key = gateway.address.upper()
    phone_info = phone_info_by_address.get(address_key)

    if phone_info is None or not phone_info.alias:
        return gateway.address

    return phone_info.alias
