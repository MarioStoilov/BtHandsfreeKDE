"""Fallback window for a call in progress, used when the notification server lacks
support for persistent notifications with buttons."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from bt_handsfree_kde import APPLICATION_NAME


class ActiveCallWindow(QWidget):
    """A small always-on-top window showing the caller, the call status and two buttons."""

    # Emitted with the call path when the user presses Hold / Resume or Hang up.
    hold_requested = Signal(str)
    hangup_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the widgets; the window stays hidden until `show_call` is called."""
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(APPLICATION_NAME)
        self._call_path = ""

        self._caller_label = QLabel(self)
        self._status_label = QLabel(self)
        self._hold_button = QPushButton("Hold", self)
        self._hangup_button = QPushButton("Hang up", self)

        button_row = QHBoxLayout()
        button_row.addWidget(self._hold_button)
        button_row.addWidget(self._hangup_button)

        column = QVBoxLayout(self)
        column.addWidget(self._caller_label)
        column.addWidget(self._status_label)
        column.addLayout(button_row)

        self._hold_button.clicked.connect(self._emit_hold)
        self._hangup_button.clicked.connect(self._emit_hangup)

    def show_call(self, call_path: str, caller_label: str, status_text: str, is_held: bool) -> None:
        """Display `call_path` with the given caller and status, raising the window.

        Args:
            call_path: Object path of the call the buttons act on.
            caller_label: Name or number to display.
            status_text: Second line, typically the elapsed duration.
            is_held: When true the hold button reads Resume.
        """
        self._call_path = call_path
        self._caller_label.setText(caller_label)
        self._status_label.setText(status_text)
        hold_label = "Resume" if is_held else "Hold"
        self._hold_button.setText(hold_label)

        if not self.isVisible():
            self.show()

    def hide_call(self, call_path: str) -> None:
        """Hide the window if it is showing `call_path`."""
        if self._call_path == call_path:
            self._call_path = ""
            self.hide()

    def _emit_hold(self) -> None:
        """Forward the Hold / Resume button to listeners."""
        if self._call_path:
            self.hold_requested.emit(self._call_path)

    def _emit_hangup(self) -> None:
        """Forward the Hang up button to listeners."""
        if self._call_path:
            self.hangup_requested.emit(self._call_path)
