"""Tests for the settings window and its Phones, Call audio and About tabs."""

from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from bt_handsfree_kde import ISSUES_URL, LICENSE_URL, REPOSITORY_URL, __version__
from bt_handsfree_kde.audio.devices import (
    DEVICE_KIND_SINK,
    DEVICE_KIND_SOURCE,
    AudioDevice,
    CallAudioChoice,
)
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.ui.call_audio_page import (
    NO_PHONE_TEXT,
    NOT_AVAILABLE_SUFFIX,
    SYSTEM_DEFAULT_TEXT,
)
from bt_handsfree_kde.ui.phones_page import (
    BLUETOOTH_UNAVAILABLE_TEXT,
    CONNECTED_WITHOUT_LINK_TEXT,
    NO_OTHER_PHONE_TEXT,
    NOT_CONNECTED_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
)
from bt_handsfree_kde.ui.settings_window import SettingsWindow
from tests.conftest import PHONE_ADDRESS, PHONE_ALIAS, SECOND_PHONE_ADDRESS, SECOND_PHONE_ALIAS
from tests.widgets.conftest import GATEWAY_PATH

# A paired phone without a hands-free link, as the BlueZ client would report it.
SECOND_PHONE_INFO = PhoneInfo(
    "/org/bluez/hci0/dev_y",
    SECOND_PHONE_ADDRESS,
    SECOND_PHONE_ALIAS,
    False,
    None,
    paired=True,
    is_hands_free_capable=True,
    icon_name="phone",
)


def _label_texts(widget) -> str:
    """Join the text of every label below `widget`, one per line."""
    texts: list[str] = []
    for label in widget.findChildren(QLabel):
        texts.append(label.text())

    return "\n".join(texts)


def test_call_audio_tab_builds_one_group_per_phone(
    qt_application, gateway, phone_info_by_address
) -> None:
    """The placeholder gives way to a titled group whose checkbox reports routing changes."""
    window = SettingsWindow()
    routing: list[tuple[str, bool]] = []
    window.audio_on_computer_changed.connect(
        lambda gateway_path, on_computer: routing.append((gateway_path, on_computer))
    )
    call_audio_page = window._call_audio_page
    assert call_audio_page._placeholder_label.text() == NO_PHONE_TEXT

    window.update_state(True, [gateway], phone_info_by_address, [], True)

    assert len(call_audio_page._group_boxes) == 1
    assert call_audio_page._group_boxes[0].title() == PHONE_ALIAS
    audio_checkbox = call_audio_page._group_boxes[0].findChild(QCheckBox)
    assert audio_checkbox.isChecked()
    audio_checkbox.setChecked(False)
    assert routing == [(GATEWAY_PATH, False)]


def test_phones_tab_splits_connected_and_previously_paired(
    qt_application, gateway, phone_info_by_address
) -> None:
    """The gateway's phone is listed with its battery; the other paired phone as history."""
    window = SettingsWindow()
    connected_phone_info = phone_info_by_address[PHONE_ADDRESS]
    paired_phones = [connected_phone_info, SECOND_PHONE_INFO]

    window.update_state(True, [gateway], phone_info_by_address, paired_phones, True)

    connected_texts = _label_texts(window._phones_page._connected_group)
    assert PHONE_ALIAS in connected_texts and "Battery 80 %" in connected_texts
    assert SECOND_PHONE_ALIAS not in connected_texts
    previous_texts = _label_texts(window._phones_page._previous_group)
    assert SECOND_PHONE_ALIAS in previous_texts and NOT_CONNECTED_TEXT in previous_texts
    assert PHONE_ALIAS not in previous_texts


def test_phones_tab_shows_empty_states_and_link_state(qt_application) -> None:
    """Without phones or BlueZ each group explains itself; a linked phone says so."""
    window = SettingsWindow()

    window.update_state(False, [], {}, [], False)
    assert SERVICE_UNAVAILABLE_TEXT in _label_texts(window._phones_page._connected_group)
    assert BLUETOOTH_UNAVAILABLE_TEXT in _label_texts(window._phones_page._previous_group)

    window.update_state(True, [], {}, [], True)
    assert NO_OTHER_PHONE_TEXT in _label_texts(window._phones_page._previous_group)

    linked_phone_info = PhoneInfo(
        "/org/bluez/hci0/dev_y",
        SECOND_PHONE_ADDRESS,
        SECOND_PHONE_ALIAS,
        True,
        None,
        paired=True,
        is_hands_free_capable=True,
    )
    window.update_state(True, [], {}, [linked_phone_info], True)
    assert CONNECTED_WITHOUT_LINK_TEXT in _label_texts(window._phones_page._previous_group)


def test_about_tab_shows_version_and_links_and_close_hides(qt_application) -> None:
    """Version, the three links and the disclosure are present; Close hides the window."""
    window = SettingsWindow()
    texts = _label_texts(window._about_page)

    assert f"Version {__version__}" in texts
    assert REPOSITORY_URL in texts and ISSUES_URL in texts and LICENSE_URL in texts
    assert "vibe-coded" in texts

    window.show_about()
    assert window.isVisible()
    assert window._tabs.currentWidget() is window._about_page
    window.show_call_audio()
    assert window._tabs.currentWidget() is window._call_audio_page
    window.findChild(QPushButton).click()
    assert not window.isVisible()


def test_call_audio_tab_lists_the_computers_devices(qt_application) -> None:
    """The choosers list the devices, keep an absent choice visible and report picks."""
    window = SettingsWindow()
    picks: list[tuple[str, str]] = []
    window.speaker_device_changed.connect(lambda name: picks.append(("speaker", name)))
    window.microphone_device_changed.connect(lambda name: picks.append(("microphone", name)))
    call_audio_page = window._call_audio_page
    assert not call_audio_page._devices_status_label.isHidden()
    assert not call_audio_page._speaker_chooser.isEnabled()

    speakers = [AudioDevice(7, "alsa_output.example", "Example Speakers", DEVICE_KIND_SINK)]
    microphones = [AudioDevice(9, "alsa_input.example", "Example Microphone", DEVICE_KIND_SOURCE)]
    choice = CallAudioChoice("bluez_output.headset", "alsa_input.example")
    window.update_audio_devices(True, speakers, microphones, choice)

    speaker_chooser = call_audio_page._speaker_chooser
    speaker_labels = [speaker_chooser.itemText(index) for index in range(speaker_chooser.count())]
    assert speaker_labels == [
        SYSTEM_DEFAULT_TEXT,
        "Example Speakers",
        f"bluez_output.headset{NOT_AVAILABLE_SUFFIX}",
    ]
    assert speaker_chooser.currentData() == "bluez_output.headset"
    assert call_audio_page._microphone_chooser.currentText() == "Example Microphone"
    assert call_audio_page._devices_status_label.isHidden()
    assert picks == []

    speaker_chooser.setCurrentIndex(1)
    call_audio_page._microphone_chooser.setCurrentIndex(0)
    assert picks == [("speaker", "alsa_output.example"), ("microphone", "")]
