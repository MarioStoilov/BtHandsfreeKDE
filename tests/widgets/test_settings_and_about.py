"""Tests for the settings window and the About window."""

from PySide6.QtWidgets import QLabel, QPushButton

from bt_handsfree_kde import ISSUES_URL, LICENSE_URL, REPOSITORY_URL, __version__
from bt_handsfree_kde.ui.about_window import AboutWindow
from bt_handsfree_kde.ui.settings_window import NO_PHONE_TEXT, SettingsWindow
from tests.widgets.conftest import GATEWAY_PATH


def test_settings_builds_one_group_per_phone(
    qt_application, gateway, phone_info_by_address
) -> None:
    """The placeholder gives way to a titled group whose checkbox reports routing changes."""
    window = SettingsWindow()
    routing: list[tuple[str, bool]] = []
    window.audio_on_computer_changed.connect(
        lambda gateway_path, on_computer: routing.append((gateway_path, on_computer))
    )
    assert window._placeholder_label.text() == NO_PHONE_TEXT

    window.update_gateways([gateway], phone_info_by_address)
    assert len(window._group_boxes) == 1
    assert window._group_boxes[0].title() == "Test phone"
    checkbox = window._group_boxes[0].findChildren(
        type(window._group_boxes[0]).__mro__[0]
    )  # placeholder to keep type lookups simple
    assert checkbox is not None
    from PySide6.QtWidgets import QCheckBox

    audio_checkbox = window._group_boxes[0].findChild(QCheckBox)
    assert audio_checkbox.isChecked()
    audio_checkbox.setChecked(False)
    assert routing == [(GATEWAY_PATH, False)]


def test_about_window_shows_version_links_and_closes(qt_application) -> None:
    """Version, the three links and the disclosure are present; Close hides the window."""
    window = AboutWindow()
    texts = "\n".join(label.text() for label in window.findChildren(QLabel))

    assert f"Version {__version__}" in texts
    assert REPOSITORY_URL in texts and ISSUES_URL in texts and LICENSE_URL in texts
    assert "vibe-coded" in texts
    window.show_and_raise()
    assert window.isVisible()
    window.findChild(QPushButton).click()
    assert not window.isVisible()
