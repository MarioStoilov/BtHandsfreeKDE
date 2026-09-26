"""Tests for the main window: chooser, unavailable reasons and routing to the tabs."""

from bt_handsfree_kde.dbus.telephony import AudioGateway
from bt_handsfree_kde.ui.main_window import NO_PHONE_TEXT, SERVICE_UNAVAILABLE_TEXT, MainWindow
from tests.widgets.conftest import GATEWAY_PATH

SECOND_GATEWAY = AudioGateway(
    "/org/pipewire/Telephony/ag2", "AA:BB:CC:DD:EE:02", 15, 15, "idle", 0, False
)


def test_unavailable_reasons_reach_the_tabs(qt_application) -> None:
    """Without the service, then without a phone, the tabs explain why."""
    window = MainWindow()
    assert window._dialpad_page._status_label.text() == SERVICE_UNAVAILABLE_TEXT

    window.update_state(True, [], {}, {}, {}, {}, {})
    assert window._dialpad_page._status_label.text() == NO_PHONE_TEXT
    assert window._contacts_page._status_label.text() == NO_PHONE_TEXT


def test_chooser_appears_with_two_phones_and_keeps_its_choice(
    qt_application, gateway, phone_info_by_address, synced_phonebook_state
) -> None:
    """One phone hides the chooser; two show it with alias and address; selection survives."""
    window = MainWindow()
    dials: list[tuple[str, str]] = []
    window.dial_requested.connect(lambda gateway_path, number: dials.append((gateway_path, number)))

    window.update_state(
        True, [gateway], phone_info_by_address, {}, {GATEWAY_PATH: synced_phonebook_state}, {}, {}
    )
    assert window._phone_chooser.isHidden()
    assert window.selected_gateway_path == GATEWAY_PATH
    assert window._contacts_page._contact_list.count() == 3

    window.update_state(
        True,
        [gateway, SECOND_GATEWAY],
        phone_info_by_address,
        {SECOND_GATEWAY.path: "Alice"},
        {GATEWAY_PATH: synced_phonebook_state},
        {},
        {},
    )
    assert not window._phone_chooser.isHidden()
    assert [window._phone_chooser.itemText(index) for index in range(2)] == [
        "Test phone",
        "AA:BB:CC:DD:EE:02",
    ]

    window._phone_chooser.setCurrentIndex(1)
    assert window._dialpad_page._status_label.text() == "Keys send tones to Alice"
    assert window._contacts_page._contact_list.count() == 0
    window.update_state(
        True,
        [gateway, SECOND_GATEWAY],
        phone_info_by_address,
        {},
        {GATEWAY_PATH: synced_phonebook_state},
        {},
        {},
    )
    assert window.selected_gateway_path == SECOND_GATEWAY.path

    window.update_state(
        True, [gateway], phone_info_by_address, {}, {GATEWAY_PATH: synced_phonebook_state}, {}, {}
    )
    assert window.selected_gateway_path == GATEWAY_PATH
    window.show_dialpad("+15550100")
    window._dialpad_page._emit_dial()
    assert dials == [(GATEWAY_PATH, "+15550100")]


def test_show_methods_switch_tabs(qt_application, gateway, phone_info_by_address) -> None:
    """Each show method selects its tab; show_messages can also pick the conversation."""
    window = MainWindow()
    opened: list[tuple[str, str]] = []
    window.conversation_opened.connect(lambda gateway_path, key: opened.append((gateway_path, key)))
    window.update_state(True, [gateway], phone_info_by_address, {}, {}, {}, {})

    window.show_contacts()
    assert window._tabs.currentWidget() is window._contacts_page
    window.show_messages(GATEWAY_PATH, "")
    assert window._tabs.currentWidget() is window._messages_page
    window.show_dialpad()
    assert window._tabs.currentWidget() is window._dialpad_page
    assert opened == []


def test_handoff_dials_from_the_chosen_phone(
    qt_application, gateway, phone_info_by_address
) -> None:
    """With two phones, a number handed over dials from the phone picked in the chooser."""
    window = MainWindow()
    dials: list[tuple[str, str]] = []
    window.dial_requested.connect(lambda gateway_path, number: dials.append((gateway_path, number)))
    window.update_state(True, [gateway, SECOND_GATEWAY], phone_info_by_address, {}, {}, {}, {})
    window._phone_chooser.setCurrentIndex(1)

    window.show_dialpad("+15550100")
    window._dialpad_page._emit_dial()

    assert dials == [(SECOND_GATEWAY.path, "+15550100")]
