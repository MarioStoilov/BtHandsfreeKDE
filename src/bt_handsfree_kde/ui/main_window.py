"""The app's main window: a phone chooser above the Dialpad and Contacts tabs."""

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QComboBox, QTabWidget, QVBoxLayout, QWidget

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.contacts.client import PhonebookState
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.dbus.telephony import AudioGateway
from bt_handsfree_kde.ui.contacts_page import ContactsPage
from bt_handsfree_kde.ui.dialpad_page import DialpadPage

# Theme icon names of the tabs (all present in Breeze).
DIALPAD_TAB_ICON = "input-dialpad"
CONTACTS_TAB_ICON = "view-pim-contacts"
# Text shown when PipeWire's telephony service is missing.
SERVICE_UNAVAILABLE_TEXT = "PipeWire telephony service not available"
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"


class MainWindow(QWidget):
    """Hosts the tabs that act on one selected phone.

    The chooser is shown only when several phones are connected; with one phone it is
    hidden and that phone is selected. Every tab receives the selected phone and the
    state that concerns it whenever the application state or the selection changes.
    """

    # Emitted with (gateway path, dial string) when a tab asks to call.
    dial_requested = Signal(str, str)
    # Emitted with (gateway path, single DTMF tone) for dialpad keys during an active call.
    tone_requested = Signal(str, str)
    # Emitted with the gateway path when the Contacts tab asks for a new sync.
    refresh_contacts_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the chooser and the tabs; `update_state` fills them."""
        super().__init__(parent)
        self.setWindowTitle(APPLICATION_NAME)
        self._is_service_available = False
        # (gateway path, label) pairs the chooser lists, so it is rebuilt only on change.
        self._chooser_entries: list[tuple[str, str]] = []
        self._active_call_label_by_gateway_path: dict[str, str] = {}
        self._phonebook_state_by_gateway_path: dict[str, PhonebookState] = {}

        self._phone_chooser = QComboBox(self)
        self._phone_chooser.setVisible(False)

        self._dialpad_page = DialpadPage(self)
        self._contacts_page = ContactsPage(self)
        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._dialpad_page, QIcon.fromTheme(DIALPAD_TAB_ICON), "Dialpad")
        self._tabs.addTab(self._contacts_page, QIcon.fromTheme(CONTACTS_TAB_ICON), "Contacts")

        column = QVBoxLayout(self)
        column.addWidget(self._phone_chooser)
        column.addWidget(self._tabs)

        self._dialpad_page.dial_requested.connect(self.dial_requested)
        self._dialpad_page.tone_requested.connect(self.tone_requested)
        self._contacts_page.dial_requested.connect(self.dial_requested)
        self._contacts_page.refresh_requested.connect(self.refresh_contacts_requested)
        self._phone_chooser.currentIndexChanged.connect(self._on_phone_selected)

        self._push_state_to_pages()

    @property
    def selected_gateway_path(self) -> str:
        """Return the object path of the phone the tabs act on; empty without phones."""
        current_data = self._phone_chooser.currentData()
        if current_data is None:
            return ""

        return str(current_data)

    def update_state(
        self,
        is_service_available: bool,
        gateways: list[AudioGateway],
        phone_info_by_address: dict[str, PhoneInfo],
        active_call_label_by_gateway_path: dict[str, str],
        phonebook_state_by_gateway_path: dict[str, PhonebookState],
    ) -> None:
        """Replace what the window knows and pass the selected phone's share to the tabs.

        The chooser keeps its selection when the same phones are still connected.

        Args:
            is_service_available: Whether the telephony service is on the bus.
            gateways: Connected phones as seen by the telephony service.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
            active_call_label_by_gateway_path: Caller label of the active call per gateway
                path; gateways without an active call are absent.
            phonebook_state_by_gateway_path: Contacts sync state per gateway path.
        """
        self._is_service_available = is_service_available
        self._active_call_label_by_gateway_path = dict(active_call_label_by_gateway_path)
        self._phonebook_state_by_gateway_path = dict(phonebook_state_by_gateway_path)

        chooser_entries: list[tuple[str, str]] = []
        for gateway in gateways:
            phone_label = _phone_label(gateway, phone_info_by_address)
            chooser_entries.append((gateway.path, phone_label))

        entries_changed = chooser_entries != self._chooser_entries
        if entries_changed:
            self._rebuild_chooser(chooser_entries)

        has_several_phones = len(chooser_entries) > 1
        self._phone_chooser.setVisible(has_several_phones)
        self._push_state_to_pages()

    def show_dialpad(self, number: str = "") -> None:
        """Open the window on the Dialpad tab, replacing its number when one is given."""
        if number:
            self._dialpad_page.set_number(number)

        self._tabs.setCurrentWidget(self._dialpad_page)
        self._bring_to_front()
        self._dialpad_page.focus_number_field()

    def show_contacts(self) -> None:
        """Open the window on the Contacts tab with the search field focused."""
        self._tabs.setCurrentWidget(self._contacts_page)
        self._bring_to_front()
        self._contacts_page.focus_search_field()

    def _bring_to_front(self) -> None:
        """Show the window and ask the window manager to raise and activate it."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _rebuild_chooser(self, chooser_entries: list[tuple[str, str]]) -> None:
        """Refill the phone chooser, keeping the selected phone when it is still listed."""
        previously_selected_path = self.selected_gateway_path

        self._phone_chooser.blockSignals(True)
        self._phone_chooser.clear()
        for gateway_path, phone_label in chooser_entries:
            self._phone_chooser.addItem(phone_label, gateway_path)
        previous_index = self._phone_chooser.findData(previously_selected_path)
        if previous_index >= 0:
            self._phone_chooser.setCurrentIndex(previous_index)
        self._phone_chooser.blockSignals(False)

        self._chooser_entries = list(chooser_entries)

    def _on_phone_selected(self, _selected_index: int) -> None:
        """Hand the newly selected phone to the tabs."""
        self._push_state_to_pages()

    def _push_state_to_pages(self) -> None:
        """Give each tab the selected phone and the part of the state it shows."""
        gateway_path = self.selected_gateway_path
        if not self._is_service_available:
            unavailable_reason = SERVICE_UNAVAILABLE_TEXT
        elif not gateway_path:
            unavailable_reason = NO_PHONE_TEXT
        else:
            unavailable_reason = ""

        active_call_label = self._active_call_label_by_gateway_path.get(gateway_path)
        phonebook_state = self._phonebook_state_by_gateway_path.get(gateway_path)

        self._dialpad_page.update_phone(gateway_path, active_call_label, unavailable_reason)
        self._contacts_page.update_phone(gateway_path, phonebook_state, unavailable_reason)


def _phone_label(gateway: AudioGateway, phone_info_by_address: dict[str, PhoneInfo]) -> str:
    """Return the phone's BlueZ alias, or its address when BlueZ has no entry."""
    address_key = gateway.address.upper()
    phone_info = phone_info_by_address.get(address_key)

    if phone_info is None or not phone_info.alias:
        return gateway.address

    return phone_info.alias
