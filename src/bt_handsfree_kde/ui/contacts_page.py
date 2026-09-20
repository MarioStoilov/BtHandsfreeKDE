"""Contacts tab: the selected phone's synced phonebook with search, Call and Refresh."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde.contacts.client import (
    SYNC_STATE_FAILED,
    SYNC_STATE_SYNCED,
    SYNC_STATE_SYNCING,
    PhonebookState,
)
from bt_handsfree_kde.contacts.phonebook import Phonebook
from bt_handsfree_kde.contacts.vcard import Contact
from bt_handsfree_kde.phone_numbers import digits_of

# Theme icon names for the buttons (all present in Breeze).
CALL_ICON = "call-start"
REFRESH_ICON = "view-refresh"
# Pixel size of the Call button icon.
CALL_ICON_SIZE = 24
# Text shown in the empty search field.
SEARCH_PLACEHOLDER_TEXT = "Search by name or number"
# Status texts for the sync states that have no phone-specific wording.
NOT_SYNCED_TEXT = "Contacts have not been read from the phone yet. Press Refresh."
SYNCING_TEXT = "Reading contacts from the phone… If the phone asks, allow contact sharing."
# Item data role under which each row stores its index into the phonebook.
CONTACT_INDEX_ROLE = Qt.ItemDataRole.UserRole
# Minimum number of rows the list shows without scrolling, in rows of the list font.
MINIMUM_VISIBLE_ROWS = 10


class ContactsPage(QWidget):
    """Lists one row per contact; Call dials its number, or asks which one when it has several.

    The list is rebuilt only when the phonebook object changes, so the selection and the
    scroll position survive the periodic state refreshes.
    """

    # Emitted with (gateway path, dial string) when the user asks to call a contact.
    dial_requested = Signal(str, str)
    # Emitted with the gateway path when the user presses Refresh.
    refresh_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the widgets; `update_phone` fills in the phonebook."""
        super().__init__(parent)
        self._gateway_path = ""
        self._shown_phonebook: Phonebook | None = None
        self._is_syncing = False

        self._search_field = QLineEdit(self)
        self._search_field.setPlaceholderText(SEARCH_PLACEHOLDER_TEXT)
        self._search_field.setClearButtonEnabled(True)

        self._contact_list = QListWidget(self)
        row_height = self._contact_list.fontMetrics().height()
        minimum_list_height = row_height * MINIMUM_VISIBLE_ROWS
        self._contact_list.setMinimumHeight(minimum_list_height)

        self._call_button = QPushButton(QIcon.fromTheme(CALL_ICON), "Call", self)
        self._call_button.setIconSize(QSize(CALL_ICON_SIZE, CALL_ICON_SIZE))
        self._call_button.setDefault(True)
        self._refresh_button = QPushButton(QIcon.fromTheme(REFRESH_ICON), "Refresh", self)
        self._refresh_button.setToolTip("Read the contacts from the phone again")

        button_row = QHBoxLayout()
        button_row.addWidget(self._call_button)
        button_row.addWidget(self._refresh_button)

        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)

        column = QVBoxLayout(self)
        column.addWidget(self._search_field)
        column.addWidget(self._contact_list)
        column.addLayout(button_row)
        column.addWidget(self._status_label)

        self._search_field.textChanged.connect(self._apply_filter)
        self._contact_list.itemSelectionChanged.connect(self._update_call_button)
        self._contact_list.itemActivated.connect(self._on_item_activated)
        self._call_button.clicked.connect(self._call_selected_contact)
        self._refresh_button.clicked.connect(self._emit_refresh)

        self._update_call_button()

    def focus_search_field(self) -> None:
        """Give keyboard focus to the search field."""
        self._search_field.setFocus()

    def update_phone(
        self,
        gateway_path: str,
        phonebook_state: PhonebookState | None,
        unavailable_reason: str,
    ) -> None:
        """Show the phonebook and sync state of the selected phone.

        Args:
            gateway_path: Object path of the selected phone; empty when there is none.
            phonebook_state: That phone's sync state, `None` when there is no phone.
            unavailable_reason: Why nothing can be done right now (no service, no phone),
                empty otherwise; shown on the status line instead of the sync state.
        """
        self._gateway_path = gateway_path

        phonebook = phonebook_state.phonebook if phonebook_state is not None else None
        phonebook_changed = phonebook is not self._shown_phonebook
        if phonebook_changed:
            self._fill_list(phonebook)

        self._is_syncing = (
            phonebook_state is not None and phonebook_state.sync_state == SYNC_STATE_SYNCING
        )
        status_text = unavailable_reason or _status_text(phonebook_state)
        self._status_label.setText(status_text)

        can_refresh = bool(gateway_path) and not self._is_syncing
        self._refresh_button.setEnabled(can_refresh)
        self._update_call_button()

    def _fill_list(self, phonebook: Phonebook | None) -> None:
        """Replace the rows with the contacts of `phonebook` and re-apply the search."""
        self._contact_list.clear()
        self._shown_phonebook = phonebook
        if phonebook is None:
            return

        for contact_index, contact in enumerate(phonebook.contacts):
            row_item = QListWidgetItem(contact.name)
            row_item.setData(CONTACT_INDEX_ROLE, contact_index)
            tooltip_lines: list[str] = []
            for phone_number in contact.numbers:
                tooltip_lines.append(f"{phone_number.kind}: {phone_number.number}")
            row_item.setToolTip("\n".join(tooltip_lines))

            self._contact_list.addItem(row_item)

        self._apply_filter()

    def _apply_filter(self) -> None:
        """Hide the rows that do not match the search field."""
        query = self._search_field.text().strip()
        if self._shown_phonebook is None:
            return

        for row_index in range(self._contact_list.count()):
            row_item = self._contact_list.item(row_index)
            contact_index = row_item.data(CONTACT_INDEX_ROLE)
            contact = self._shown_phonebook.contacts[contact_index]
            is_match = _matches_query(contact, query)

            row_item.setHidden(not is_match)

    def _selected_contact(self) -> Contact | None:
        """Return the contact of the selected visible row, or `None`."""
        selected_items = self._contact_list.selectedItems()
        if not selected_items or self._shown_phonebook is None:
            return None

        selected_item = selected_items[0]
        if selected_item.isHidden():
            return None

        contact_index = selected_item.data(CONTACT_INDEX_ROLE)

        return self._shown_phonebook.contacts[contact_index]

    def _update_call_button(self) -> None:
        """Enable Call only when a phone is selected and a contact row is chosen."""
        has_phone = bool(self._gateway_path)
        has_contact = self._selected_contact() is not None

        self._call_button.setEnabled(has_phone and has_contact)

    def _on_item_activated(self, _activated_item: QListWidgetItem) -> None:
        """Double-click or Enter on a row calls that contact."""
        self._call_selected_contact()

    def _call_selected_contact(self) -> None:
        """Dial the selected contact's number, asking which one when it has several."""
        contact = self._selected_contact()
        if contact is None or not self._gateway_path:
            return

        has_single_number = len(contact.numbers) == 1
        if has_single_number:
            only_number = contact.numbers[0]
            self.dial_requested.emit(self._gateway_path, only_number.dial_string)
            return

        number_menu = QMenu(self)
        for phone_number in contact.numbers:
            action_text = f"{phone_number.kind}: {phone_number.number}"
            number_action = QAction(action_text, number_menu)
            number_action.setData(phone_number.dial_string)
            number_menu.addAction(number_action)

        menu_position = self._call_button.mapToGlobal(self._call_button.rect().bottomLeft())
        chosen_action = number_menu.exec(menu_position)
        if chosen_action is not None:
            chosen_dial_string = chosen_action.data()
            self.dial_requested.emit(self._gateway_path, chosen_dial_string)

    def _emit_refresh(self) -> None:
        """Ask the application to read the selected phone's contacts again."""
        if self._gateway_path:
            self.refresh_requested.emit(self._gateway_path)


def _matches_query(contact: Contact, query: str) -> bool:
    """Tell whether `contact` matches a search: by name text, or by the digits typed."""
    if not query:
        return True

    name_matches = query.casefold() in contact.name.casefold()
    if name_matches:
        return True

    query_digits = digits_of(query)
    if not query_digits:
        return False

    for phone_number in contact.numbers:
        number_digits = digits_of(phone_number.dial_string)
        if query_digits in number_digits:
            return True

    return False


def _status_text(phonebook_state: PhonebookState | None) -> str:
    """Describe the sync state for the status line."""
    if phonebook_state is None:
        return ""

    if phonebook_state.sync_state == SYNC_STATE_SYNCING:
        return SYNCING_TEXT

    if phonebook_state.sync_state == SYNC_STATE_FAILED:
        return phonebook_state.error_text

    is_synced = phonebook_state.sync_state == SYNC_STATE_SYNCED
    has_phonebook = phonebook_state.phonebook is not None and phonebook_state.synced_at is not None
    if is_synced and has_phonebook:
        contact_count = phonebook_state.phonebook.contact_count
        count_text = "1 contact" if contact_count == 1 else f"{contact_count} contacts"
        synced_time_text = phonebook_state.synced_at.strftime("%H:%M")
        return f"{count_text}, read from the phone at {synced_time_text}"

    return NOT_SYNCED_TEXT
