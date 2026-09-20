"""Tests for the Contacts tab."""

from bt_handsfree_kde.contacts.client import (
    SYNC_STATE_FAILED,
    SYNC_STATE_IDLE,
    SYNC_STATE_SYNCING,
    PhonebookState,
)
from bt_handsfree_kde.ui.contacts_page import NOT_SYNCED_TEXT, SYNCING_TEXT, ContactsPage
from tests.conftest import PHONE_ADDRESS
from tests.widgets.conftest import GATEWAY_PATH


def _visible_rows(page: ContactsPage) -> list[str]:
    """Return the texts of the rows that are not hidden."""
    visible: list[str] = []
    for row_index in range(page._contact_list.count()):
        row_item = page._contact_list.item(row_index)
        if not row_item.isHidden():
            visible.append(row_item.text())

    return visible


def test_status_line_follows_the_sync_state(qt_application, synced_phonebook_state) -> None:
    """Each state has its text and Refresh is disabled while syncing."""
    page = ContactsPage()

    page.update_phone("", None, "No phone connected")
    assert page._status_label.text() == "No phone connected"
    assert not page._refresh_button.isEnabled()

    page.update_phone(
        GATEWAY_PATH, PhonebookState(PHONE_ADDRESS, SYNC_STATE_IDLE, None, None, ""), ""
    )
    assert page._status_label.text() == NOT_SYNCED_TEXT

    page.update_phone(
        GATEWAY_PATH, PhonebookState(PHONE_ADDRESS, SYNC_STATE_SYNCING, None, None, ""), ""
    )
    assert page._status_label.text() == SYNCING_TEXT
    assert not page._refresh_button.isEnabled()

    page.update_phone(GATEWAY_PATH, synced_phonebook_state, "")
    assert page._status_label.text() == "3 contacts, read from the phone at 10:05"
    assert page._contact_list.count() == 3

    failed_state = PhonebookState(
        PHONE_ADDRESS, SYNC_STATE_FAILED, synced_phonebook_state.phonebook, None, "Nope"
    )
    page.update_phone(GATEWAY_PATH, failed_state, "")
    assert page._status_label.text() == "Nope"
    assert page._contact_list.count() == 3


def test_search_matches_names_and_digits(qt_application, synced_phonebook_state) -> None:
    """Typing text filters by name; typing digits filters by number."""
    page = ContactsPage()
    page.update_phone(GATEWAY_PATH, synced_phonebook_state, "")

    page._search_field.setText("ali")
    assert _visible_rows(page) == ["Alice Doe"]
    page._search_field.setText("555 0104")
    assert _visible_rows(page) == ["Carol"]
    page._search_field.setText("")
    assert len(_visible_rows(page)) == 3


def test_call_dials_the_selected_contact_and_refresh_asks_for_a_sync(
    qt_application, synced_phonebook_state
) -> None:
    """A single-number contact dials directly; selection survives a same-phonebook refresh."""
    page = ContactsPage()
    dials: list[tuple[str, str]] = []
    refreshes: list[str] = []
    page.dial_requested.connect(lambda gateway_path, number: dials.append((gateway_path, number)))
    page.refresh_requested.connect(refreshes.append)
    page.update_phone(GATEWAY_PATH, synced_phonebook_state, "")

    assert not page._call_button.isEnabled()
    page._contact_list.setCurrentRow(1)
    assert page._call_button.isEnabled()
    page.update_phone(GATEWAY_PATH, synced_phonebook_state, "")
    assert page._contact_list.currentRow() == 1

    page._call_button.click()
    assert dials == [(GATEWAY_PATH, "5550103")]
    page._refresh_button.click()
    assert refreshes == [GATEWAY_PATH]
    assert page._contact_list.item(0).toolTip() == "Mobile: +15550100\nWork: +1 555 0102"
