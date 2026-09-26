"""Window listing what the app depends on, with what provides each missing item."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.dbus.dependencies import DependencyReport, DependencyStatus

# Theme icon names for a met and a missing item (both present in Breeze).
MET_ICON = "dialog-ok-apply"
MISSING_ICON = "dialog-error"
# Pixel size of the row icons.
ROW_ICON_SIZE = 22
# Fixed width of the window in pixels, so the wrapped remedies read well.
WINDOW_WIDTH = 520
# Introductions shown above the rows.
SOMETHING_MISSING_TEXT = (
    f"Some of what {APPLICATION_NAME} needs is not available. The app keeps running with "
    "what works; this list is updated as soon as an item appears."
)
ALL_MET_TEXT = f"Everything {APPLICATION_NAME} needs is available."


class DependenciesWindow(QWidget):
    """Shows one row per checked item: icon, name, what was found and, when missing, the fix."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the frame; `update_report` fills in the rows."""
        super().__init__(parent)
        self.setWindowTitle(f"{APPLICATION_NAME} requirements")
        self.setFixedWidth(WINDOW_WIDTH)
        # Detail label of each row by item key, so a re-check updates the text in place.
        self._detail_label_by_key: dict[str, QLabel] = {}
        self._row_widgets: list[QWidget] = []

        self._introduction_label = QLabel(ALL_MET_TEXT, self)
        self._introduction_label.setWordWrap(True)

        self._rows_column = QVBoxLayout()

        close_button = QPushButton("Close", self)
        close_button.setDefault(True)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(close_button)

        column = QVBoxLayout(self)
        column.addWidget(self._introduction_label)
        column.addLayout(self._rows_column)
        column.addLayout(button_row)

        close_button.clicked.connect(self.close)

    def update_report(self, report: DependencyReport) -> None:
        """Rebuild the rows for `report`.

        Args:
            report: The latest check outcome; rows follow its order.
        """
        introduction_text = ALL_MET_TEXT if report.all_met else SOMETHING_MISSING_TEXT
        self._introduction_label.setText(introduction_text)

        for row_widget in self._row_widgets:
            self._rows_column.removeWidget(row_widget)
            row_widget.deleteLater()
        self._row_widgets.clear()
        self._detail_label_by_key.clear()

        for status in report.statuses:
            row_widget = self._build_row(status)
            self._rows_column.addWidget(row_widget)
            self._row_widgets.append(row_widget)

        self.adjustSize()

    def show_and_raise(self) -> None:
        """Show the window and bring it to the front."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _build_row(self, status: DependencyStatus) -> QWidget:
        """Create the row for one item: icon, bold title, detail and the remedy when missing."""
        row_widget = QWidget(self)

        icon_name = MET_ICON if status.is_met else MISSING_ICON
        icon_label = QLabel(row_widget)
        icon_label.setPixmap(QIcon.fromTheme(icon_name).pixmap(ROW_ICON_SIZE, ROW_ICON_SIZE))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)

        title_label = QLabel(status.title, row_widget)
        title_font = QFont()
        title_font.setBold(True)
        title_label.setFont(title_font)

        detail_text = status.detail
        if not status.is_met:
            detail_text = f"{status.detail} {status.remedy}"
        detail_label = QLabel(detail_text, row_widget)
        detail_label.setWordWrap(True)
        self._detail_label_by_key[status.key] = detail_label

        text_column = QVBoxLayout()
        text_column.addWidget(title_label)
        text_column.addWidget(detail_label)

        row = QHBoxLayout(row_widget)
        row.addWidget(icon_label)
        row.addLayout(text_column)

        return row_widget
