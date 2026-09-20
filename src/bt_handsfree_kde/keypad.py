"""The twelve-key telephone keypad shared by the call window and the dialpad window."""

from collections.abc import Callable
from functools import partial

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QToolButton, QWidget

# Keys in display order, three per row, as on a telephone.
KEYPAD_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#")
KEYPAD_COLUMNS = 3
# Point size of the keys and of the fields that show what was typed on them.
KEYPAD_FONT_POINT_SIZE = 16
# Minimum key size in pixels, so the pad is comfortable to hit with a mouse.
KEYPAD_KEY_SIZE = 48


def keypad_font() -> QFont:
    """Return the font used for the keys and for the text they produce."""
    font = QFont()
    font.setPointSize(KEYPAD_FONT_POINT_SIZE)

    return font


def build_keypad(parent: QWidget, on_key_pressed: Callable[[str], None]) -> QGridLayout:
    """Create the key grid; `on_key_pressed` receives the key's character on each click.

    Args:
        parent: Widget that owns the key buttons.
        on_key_pressed: Called with one of `KEYPAD_KEYS` when the user clicks that key.

    Returns:
        A grid layout holding the twelve keys, ready to be added to a parent layout.
    """
    key_font = keypad_font()
    key_grid = QGridLayout()

    for key_index, key in enumerate(KEYPAD_KEYS):
        row, column = divmod(key_index, KEYPAD_COLUMNS)
        key_button = QToolButton(parent)
        key_button.setText(key)
        key_button.setFont(key_font)
        key_button.setMinimumSize(KEYPAD_KEY_SIZE, KEYPAD_KEY_SIZE)
        key_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        key_button.clicked.connect(partial(on_key_pressed, key))

        key_grid.addWidget(key_button, row, column)

    return key_grid
