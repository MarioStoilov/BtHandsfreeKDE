"""Always-on-top window for the call in progress: caller, duration, icon buttons, dialpad."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.dtmf import DtmfTonePlayer
from bt_handsfree_kde.keypad import build_keypad, keypad_font
from bt_handsfree_kde.telephony import CALL_STATE_HELD, Call

# Theme icon names for the buttons (all present in Breeze).
ANSWER_ICON = "call-start"
HANGUP_ICON = "call-stop"
HOLD_ICON = "media-playback-pause"
RESUME_ICON = "media-playback-start"
DIALPAD_ICON = "input-dialpad"
CLEAR_ICON = "edit-clear"
# Pixel size of the button icons.
BUTTON_ICON_SIZE = 32
# Point size of the caller name.
CALLER_FONT_POINT_SIZE = 14


class CallWindow(QWidget):
    """Shows one call with the actions its state allows.

    For a ringing call the buttons are Answer and Reject. For any other call they are
    Hold (or Resume), Hang up and a Dialpad toggle that reveals a DTMF keypad. Each key
    press plays the key's tone locally and appends it to the display above the keys,
    the way a phone does; the tone itself is sent through the phone.
    """

    # Emitted with the call path.
    answer_requested = Signal(str)
    hangup_requested = Signal(str)
    hold_requested = Signal(str)
    # Emitted with (call path, single DTMF tone).
    tone_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the widgets; the window stays hidden until `show_call` is called."""
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(APPLICATION_NAME)
        self._call_path = ""
        self._tone_player = DtmfTonePlayer()

        self._caller_label = QLabel(self)
        caller_font = QFont()
        caller_font.setPointSize(CALLER_FONT_POINT_SIZE)
        caller_font.setBold(True)
        self._caller_label.setFont(caller_font)
        self._caller_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._answer_button = _icon_button(self, ANSWER_ICON, "Answer")
        self._hold_button = _icon_button(self, HOLD_ICON, "Hold")
        self._hangup_button = _icon_button(self, HANGUP_ICON, "Hang up")
        self._dialpad_button = _icon_button(self, DIALPAD_ICON, "Dialpad")
        self._dialpad_button.setCheckable(True)

        button_row = QHBoxLayout()
        button_row.addWidget(self._answer_button)
        button_row.addWidget(self._hold_button)
        button_row.addWidget(self._hangup_button)
        button_row.addWidget(self._dialpad_button)

        self._dialpad = self._build_dialpad()
        self._dialpad.setVisible(False)

        column = QVBoxLayout(self)
        column.addWidget(self._caller_label)
        column.addWidget(self._status_label)
        column.addLayout(button_row)
        column.addWidget(self._dialpad)

        self._answer_button.clicked.connect(self._emit_answer)
        self._hold_button.clicked.connect(self._emit_hold)
        self._hangup_button.clicked.connect(self._emit_hangup)
        self._dialpad_button.toggled.connect(self._set_dialpad_visible)

    @property
    def shown_call_path(self) -> str:
        """Return the path of the call currently displayed, empty when hidden."""
        return self._call_path

    def show_call(self, call: Call, caller_label: str, status_text: str) -> None:
        """Display `call` with the given caller and status, raising the window.

        Switching to a different call clears the tones typed for the previous one.

        Args:
            call: The call the buttons act on.
            caller_label: Name or number to display.
            status_text: Second line: ringing state, dialing state or elapsed duration.
        """
        call_changed = call.path != self._call_path
        if call_changed:
            self._call_path = call.path
            self._sent_tones.clear()
            self._dialpad_button.setChecked(False)

        self._caller_label.setText(caller_label)
        self._status_label.setText(status_text)

        is_incoming = call.is_incoming
        self._answer_button.setVisible(is_incoming)
        self._hold_button.setVisible(not is_incoming)
        self._dialpad_button.setVisible(not is_incoming)
        hangup_text = "Reject" if is_incoming else "Hang up"
        self._hangup_button.setText(hangup_text)

        is_held = call.state == CALL_STATE_HELD
        hold_icon_name = RESUME_ICON if is_held else HOLD_ICON
        hold_text = "Resume" if is_held else "Hold"
        self._hold_button.setIcon(QIcon.fromTheme(hold_icon_name))
        self._hold_button.setText(hold_text)

        if not self.isVisible():
            self.adjustSize()
            self.show()

    def hide_call(self) -> None:
        """Hide the window and forget the displayed call."""
        self._call_path = ""
        self._sent_tones.clear()
        self.hide()

    def _build_dialpad(self) -> QWidget:
        """Create the DTMF keypad with a phone-style display of the keys pressed so far."""
        dialpad = QWidget(self)
        display_font = keypad_font()

        self._sent_tones = QLineEdit(dialpad)
        self._sent_tones.setReadOnly(True)
        self._sent_tones.setFont(display_font)
        self._sent_tones.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sent_tones.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clear_button = QToolButton(dialpad)
        clear_button.setIcon(QIcon.fromTheme(CLEAR_ICON))
        clear_button.setToolTip("Clear")
        clear_button.setAutoRaise(True)
        clear_button.clicked.connect(self._sent_tones.clear)

        display_row = QHBoxLayout()
        display_row.addWidget(self._sent_tones)
        display_row.addWidget(clear_button)

        key_grid = build_keypad(dialpad, self._send_tone)

        dialpad_column = QVBoxLayout(dialpad)
        dialpad_column.addLayout(display_row)
        dialpad_column.addLayout(key_grid)

        return dialpad

    def _set_dialpad_visible(self, is_visible: bool) -> None:
        """Expand or collapse the keypad and shrink the window when collapsing."""
        self._dialpad.setVisible(is_visible)
        self.adjustSize()

    def _send_tone(self, tone: str) -> None:
        """Play the key's tone, echo it in the display and ask the application to send it."""
        if not self._call_path:
            return

        self._tone_player.play(tone)
        current_text = self._sent_tones.text()
        self._sent_tones.setText(current_text + tone)
        self.tone_requested.emit(self._call_path, tone)

    def _emit_answer(self) -> None:
        """Forward the Answer button to listeners."""
        if self._call_path:
            self.answer_requested.emit(self._call_path)

    def _emit_hold(self) -> None:
        """Forward the Hold / Resume button to listeners."""
        if self._call_path:
            self.hold_requested.emit(self._call_path)

    def _emit_hangup(self) -> None:
        """Forward the Hang up / Reject button to listeners."""
        if self._call_path:
            self.hangup_requested.emit(self._call_path)


def _icon_button(parent: QWidget, icon_name: str, text: str) -> QToolButton:
    """Create a tool button with a large theme icon above its text."""
    button = QToolButton(parent)
    button.setIcon(QIcon.fromTheme(icon_name))
    button.setIconSize(QSize(BUTTON_ICON_SIZE, BUTTON_ICON_SIZE))
    button.setText(text)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)

    return button
