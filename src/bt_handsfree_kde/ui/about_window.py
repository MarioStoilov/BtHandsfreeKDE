"""About window: icon, name, version, description, links, disclosure and a Close button."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from bt_handsfree_kde import (
    APPLICATION_DESCRIPTION,
    APPLICATION_NAME,
    COPYRIGHT_HOLDER,
    ISSUES_URL,
    LICENSE_NAME,
    LICENSE_URL,
    REPOSITORY_URL,
    __version__,
)
from bt_handsfree_kde.icons import application_icon

# Pixel size of the application icon at the top of the window.
ICON_SIZE = 96
# Point size of the application name.
NAME_FONT_POINT_SIZE = 18
# Fixed width of the window in pixels, so the wrapped paragraphs read well.
WINDOW_WIDTH = 420
# Text of the links row; the URLs come from the package constants.
SOURCE_LINK_TEXT = "Source code"
ISSUES_LINK_TEXT = "Report a problem"
LICENSE_LINK_TEXT = LICENSE_NAME
# The same disclosure the README makes, so a user of the installed app sees it too.
DISCLOSURE_TEXT = (
    "Full disclosure: this is a vibe-coded app. I wanted my phone's calls on my desktop "
    "and had an AI assistant write it with me, step by step, against a real phone. It "
    "works for me. It may work for you too, but set your expectations accordingly."
)


class AboutWindow(QWidget):
    """Tells the user what the app is, which version runs, where it comes from and its terms."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the window; it stays hidden until shown from the tray."""
        super().__init__(parent)
        self.setWindowTitle(f"About {APPLICATION_NAME}")
        self.setFixedWidth(WINDOW_WIDTH)

        icon_label = QLabel(self)
        icon_pixmap = application_icon().pixmap(ICON_SIZE, ICON_SIZE)
        icon_label.setPixmap(icon_pixmap)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        name_label = QLabel(APPLICATION_NAME, self)
        name_font = QFont()
        name_font.setPointSize(NAME_FONT_POINT_SIZE)
        name_font.setBold(True)
        name_label.setFont(name_font)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        version_label = QLabel(f"Version {__version__}", self)
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        description_label = _paragraph_label(APPLICATION_DESCRIPTION, self)

        links_html = (
            f'<a href="{REPOSITORY_URL}">{SOURCE_LINK_TEXT}</a> · '
            f'<a href="{ISSUES_URL}">{ISSUES_LINK_TEXT}</a> · '
            f'<a href="{LICENSE_URL}">{LICENSE_LINK_TEXT}</a>'
        )
        links_label = QLabel(links_html, self)
        links_label.setTextFormat(Qt.TextFormat.RichText)
        links_label.setOpenExternalLinks(True)
        links_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        copyright_label = _paragraph_label(f"© {COPYRIGHT_HOLDER}. {LICENSE_NAME}.", self)
        disclosure_label = _paragraph_label(DISCLOSURE_TEXT, self)

        close_button = QPushButton("Close", self)
        close_button.setDefault(True)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(close_button)
        button_row.addStretch()

        column = QVBoxLayout(self)
        column.addWidget(icon_label)
        column.addWidget(name_label)
        column.addWidget(version_label)
        column.addWidget(description_label)
        column.addWidget(links_label)
        column.addWidget(copyright_label)
        column.addWidget(disclosure_label)
        column.addLayout(button_row)

        close_button.clicked.connect(self.close)

    def show_and_raise(self) -> None:
        """Show the window and bring it to the front."""
        self.show()
        self.raise_()
        self.activateWindow()


def _paragraph_label(text: str, parent: QWidget) -> QLabel:
    """Create a centred, word-wrapped label for one paragraph."""
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)

    return label
