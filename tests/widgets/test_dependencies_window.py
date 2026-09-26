"""Tests for the requirements window."""

from bt_handsfree_kde.dbus.dependencies import (
    OBEXD_KEY,
    OBEXD_MISSING_DETAIL,
    OBEXD_REMEDY,
    OBEXD_TITLE,
    TRAY_KEY,
    TRAY_RUNNING_DETAIL,
    TRAY_TITLE,
    DependencyReport,
    DependencyStatus,
)
from bt_handsfree_kde.ui.dependencies_window import (
    ALL_MET_TEXT,
    SOMETHING_MISSING_TEXT,
    DependenciesWindow,
)

MET_TRAY = DependencyStatus(TRAY_KEY, TRAY_TITLE, True, TRAY_RUNNING_DETAIL, "tray remedy")
MISSING_OBEXD = DependencyStatus(OBEXD_KEY, OBEXD_TITLE, False, OBEXD_MISSING_DETAIL, OBEXD_REMEDY)


def test_rows_follow_the_report_and_missing_items_show_the_remedy(qt_application) -> None:
    """A missing item's row carries the detail and the remedy; a met one only the detail."""
    window = DependenciesWindow()

    window.update_report(DependencyReport((MET_TRAY, MISSING_OBEXD)))

    assert window._introduction_label.text() == SOMETHING_MISSING_TEXT
    assert len(window._row_widgets) == 2
    assert window._detail_label_by_key[TRAY_KEY].text() == TRAY_RUNNING_DETAIL
    obexd_text = window._detail_label_by_key[OBEXD_KEY].text()
    assert OBEXD_MISSING_DETAIL in obexd_text and OBEXD_REMEDY in obexd_text

    window.update_report(DependencyReport((MET_TRAY,)))

    assert window._introduction_label.text() == ALL_MET_TEXT
    assert list(window._detail_label_by_key) == [TRAY_KEY]


def test_close_hides_the_window(qt_application) -> None:
    """Show and raise makes the window visible; Close hides it."""
    window = DependenciesWindow()
    window.update_report(DependencyReport((MISSING_OBEXD,)))

    window.show_and_raise()
    assert window.isVisible()
    window.close()
    assert not window.isVisible()
