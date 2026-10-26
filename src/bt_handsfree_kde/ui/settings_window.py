"""Settings window: the Phones, Call audio and About tabs with a Close button."""

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTabWidget, QVBoxLayout, QWidget

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.audio.devices import AudioDevice, CallAudioChoice
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.dbus.telephony import AudioGateway
from bt_handsfree_kde.ui.about_page import AboutPage
from bt_handsfree_kde.ui.call_audio_page import CallAudioPage
from bt_handsfree_kde.ui.phones_page import PhonesPage

# Theme icon names of the tabs (all present in Breeze).
PHONES_TAB_ICON = "smartphone"
CALL_AUDIO_TAB_ICON = "audio-headset"
ABOUT_TAB_ICON = "help-about"
# Fixed width of the window in pixels, so the About paragraphs wrap well.
WINDOW_WIDTH = 480


class SettingsWindow(QWidget):
    """Hosts the three settings tabs; the application pushes state and picks the tab."""

    # Emitted with (gateway path, level 0..15) when a slider is released.
    speaker_volume_changed = Signal(str, int)
    microphone_volume_changed = Signal(str, int)
    # Emitted with (gateway path, audio on this computer) when the checkbox toggles.
    audio_on_computer_changed = Signal(str, bool)
    # Emitted with the device's `node.name` (empty for the system default) when the user
    # picks the computer's speaker or microphone for calls.
    speaker_device_changed = Signal(str)
    microphone_device_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the tabs; `update_state` and `update_audio_devices` fill them."""
        super().__init__(parent)
        self.setWindowTitle(f"{APPLICATION_NAME} settings")
        self.setFixedWidth(WINDOW_WIDTH)

        self._phones_page = PhonesPage(self)
        self._call_audio_page = CallAudioPage(self)
        self._about_page = AboutPage(self)
        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._phones_page, QIcon.fromTheme(PHONES_TAB_ICON), "Phones")
        self._tabs.addTab(self._call_audio_page, QIcon.fromTheme(CALL_AUDIO_TAB_ICON), "Call audio")
        self._tabs.addTab(self._about_page, QIcon.fromTheme(ABOUT_TAB_ICON), "About")

        close_button = QPushButton("Close", self)
        close_button.setDefault(True)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(close_button)

        column = QVBoxLayout(self)
        column.addWidget(self._tabs)
        column.addLayout(button_row)

        self._call_audio_page.speaker_volume_changed.connect(self.speaker_volume_changed)
        self._call_audio_page.microphone_volume_changed.connect(self.microphone_volume_changed)
        self._call_audio_page.audio_on_computer_changed.connect(self.audio_on_computer_changed)
        self._call_audio_page.speaker_device_changed.connect(self.speaker_device_changed)
        self._call_audio_page.microphone_device_changed.connect(self.microphone_device_changed)
        close_button.clicked.connect(self.close)

    def update_state(
        self,
        is_service_available: bool,
        gateways: list[AudioGateway],
        phone_info_by_address: dict[str, PhoneInfo],
        paired_phones: list[PhoneInfo],
        is_bluetooth_available: bool,
    ) -> None:
        """Give the Phones and Call audio tabs the current phones.

        Args:
            is_service_available: Whether the telephony service is on the bus.
            gateways: Connected phones as seen by the telephony service.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
            paired_phones: Every paired phone BlueZ knows, in display order.
            is_bluetooth_available: Whether BlueZ could be reached.
        """
        self._phones_page.update_phones(
            is_service_available,
            gateways,
            phone_info_by_address,
            paired_phones,
            is_bluetooth_available,
        )
        self._call_audio_page.update_gateways(gateways, phone_info_by_address)
        self.adjustSize()

    def update_audio_devices(
        self,
        is_available: bool,
        speakers: list[AudioDevice],
        microphones: list[AudioDevice],
        choice: CallAudioChoice,
    ) -> None:
        """Give the Call audio tab the computer's devices and the stored choice.

        Args:
            is_available: Whether PipeWire is reachable.
            speakers: The computer's sink-like devices, in display order.
            microphones: The computer's source-like devices, in display order.
            choice: The stored devices; empty names mean the system default.
        """
        self._call_audio_page.update_devices(is_available, speakers, microphones, choice)

    def show_phones(self) -> None:
        """Open the window on the Phones tab."""
        self._tabs.setCurrentWidget(self._phones_page)
        self._bring_to_front()

    def show_call_audio(self) -> None:
        """Open the window on the Call audio tab."""
        self._tabs.setCurrentWidget(self._call_audio_page)
        self._bring_to_front()

    def show_about(self) -> None:
        """Open the window on the About tab."""
        self._tabs.setCurrentWidget(self._about_page)
        self._bring_to_front()

    def _bring_to_front(self) -> None:
        """Show the window and ask the window manager to raise and activate it."""
        self.show()
        self.raise_()
        self.activateWindow()
