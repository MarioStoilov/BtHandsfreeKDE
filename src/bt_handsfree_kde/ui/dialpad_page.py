"""Dialpad tab: type or paste a number and call it; the keys send DTMF during a call."""

from PySide6.QtCore import QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde.dtmf import DtmfTonePlayer
from bt_handsfree_kde.phone_numbers import NUMBER_FIELD_PATTERN, dial_string_from_text
from bt_handsfree_kde.ui.keypad import build_keypad, keypad_font

# Theme icon names for the buttons (all present in Breeze).
CALL_ICON = "call-start"
BACKSPACE_ICON = "edit-clear-locationbar-rtl"
# Pixel size of the Call button icon.
CALL_ICON_SIZE = 24
# Text shown in the empty number field.
NUMBER_PLACEHOLDER_TEXT = "Enter number"


class DialpadPage(QWidget):
    """Places calls from a typed number on the phone the main window has selected.

    Each key press plays the key's tone locally and inserts the key into the number
    field; while the selected phone has an active call the key is also sent to it as a
    DTMF tone, the way a phone's in-call dialpad behaves, and the status line says so.
    """

    # Emitted with (gateway path, dial string) when the user asks to call.
    dial_requested = Signal(str, str)
    # Emitted with (gateway path, single DTMF tone) for key presses during an active call.
    tone_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the widgets; `update_phone` tells the page which phone it acts on."""
        super().__init__(parent)
        self._tone_player = DtmfTonePlayer()
        self._gateway_path = ""
        self._active_call_label: str | None = None
        self._unavailable_reason = ""

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
        column.addLayout(number_row)
        column.addLayout(key_grid)
        column.addWidget(self._call_button)
        column.addWidget(self._status_label)

        self._number_field.textChanged.connect(self._update_call_button)
        self._number_field.returnPressed.connect(self._emit_dial)
        self._backspace_button.clicked.connect(self._number_field.backspace)
        self._call_button.clicked.connect(self._emit_dial)

        self._update_status()

    def set_number(self, number: str) -> None:
        """Replace the number field's content and put the cursor after it."""
        self._number_field.setText(number)
        self._number_field.end(False)

    def focus_number_field(self) -> None:
        """Give keyboard focus to the number field."""
        self._number_field.setFocus()

    def update_phone(
        self, gateway_path: str, active_call_label: str | None, unavailable_reason: str
    ) -> None:
        """Tell the page which phone it dials from and whether that phone is in a call.

        Args:
            gateway_path: Object path of the selected phone; empty when there is none.
            active_call_label: Caller label of the phone's active call, `None` without one.
            unavailable_reason: Why calling is impossible right now, empty when it is
                possible; shown on the status line.
        """
        self._gateway_path = gateway_path
        self._active_call_label = active_call_label
        self._unavailable_reason = unavailable_reason

        self._update_status()

    def _update_status(self) -> None:
        """Show why calling is impossible, or that keys go to a call, and update the button."""
        if self._unavailable_reason:
            status_text = self._unavailable_reason
        elif self._active_call_label is not None:
            status_text = f"Keys send tones to {self._active_call_label}"
        else:
            status_text = ""

        self._status_label.setText(status_text)
        self._update_call_button()

    def _update_call_button(self) -> None:
        """Enable Call only when a phone is selected and the field holds something dialable."""
        dial_string = dial_string_from_text(self._number_field.text())
        has_number = bool(dial_string)
        has_phone = bool(self._gateway_path)
        can_dial = has_phone and has_number

        self._call_button.setEnabled(can_dial)

    def _press_key(self, key: str) -> None:
        """Play the key's tone, insert it into the number and send it as DTMF during a call."""
        self._tone_player.play(key)
        self._number_field.insert(key)

        has_active_call = bool(self._gateway_path) and self._active_call_label is not None
        if has_active_call:
            self.tone_requested.emit(self._gateway_path, key)

    def _emit_dial(self) -> None:
        """Ask the application to call the number in the field from the selected phone."""
        dial_string = dial_string_from_text(self._number_field.text())
        has_number = bool(dial_string)
        has_phone = bool(self._gateway_path)

        if has_phone and has_number:
            self.dial_requested.emit(self._gateway_path, dial_string)
