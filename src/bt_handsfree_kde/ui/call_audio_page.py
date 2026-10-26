"""Call audio tab of the settings window: the computer's devices, per-phone volumes, routing."""

from functools import partial

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde.audio.devices import SYSTEM_DEFAULT_DEVICE, AudioDevice, CallAudioChoice
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.dbus.telephony import MAX_VOLUME_LEVEL, MIN_VOLUME_LEVEL, AudioGateway

# HFP codec identifiers as reported in `AudioGatewayTransport1.Codec`.
CODEC_NAMES = {1: "CVSD", 2: "mSBC", 3: "LC3-SWB"}
# Codec value while no audio link is open.
NO_CODEC = 0
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"
# The computer's device group: title, the two chooser labels and the entry for the
# system default device.
COMPUTER_GROUP_TITLE = "This computer"
SPEAKER_CHOOSER_LABEL = "Play calls on"
MICROPHONE_CHOOSER_LABEL = "Microphone"
SYSTEM_DEFAULT_TEXT = "System default"
# Appended to a chosen device that PipeWire does not list right now.
NOT_AVAILABLE_SUFFIX = " (not available)"
# Shown under the group while PipeWire cannot be reached; the choosers are disabled then.
PIPEWIRE_UNAVAILABLE_TEXT = "PipeWire not reachable; calls use the system default devices"


class CallAudioPage(QWidget):
    """One group of controls per connected phone; rebuilt whenever the gateways change."""

    # Emitted with (gateway path, level 0..15) when a slider is released.
    speaker_volume_changed = Signal(str, int)
    microphone_volume_changed = Signal(str, int)
    # Emitted with (gateway path, audio on this computer) when the checkbox toggles.
    audio_on_computer_changed = Signal(str, bool)
    # Emitted with the device's `node.name` (empty for the system default) when the user
    # picks a speaker or a microphone.
    speaker_device_changed = Signal(str)
    microphone_device_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the device group and the empty phone area; the update methods fill them."""
        super().__init__(parent)
        self._column = QVBoxLayout(self)
        self._group_boxes: list[QGroupBox] = []
        # (name, label) pairs each chooser lists, so it is rebuilt only on change.
        self._speaker_entries: list[tuple[str, str]] = []
        self._microphone_entries: list[tuple[str, str]] = []

        computer_group = QGroupBox(COMPUTER_GROUP_TITLE, self)
        computer_form = QFormLayout(computer_group)
        self._speaker_chooser = QComboBox(computer_group)
        self._microphone_chooser = QComboBox(computer_group)
        computer_form.addRow(SPEAKER_CHOOSER_LABEL, self._speaker_chooser)
        computer_form.addRow(MICROPHONE_CHOOSER_LABEL, self._microphone_chooser)
        self._devices_status_label = QLabel(PIPEWIRE_UNAVAILABLE_TEXT, computer_group)
        self._devices_status_label.setWordWrap(True)
        computer_form.addRow(self._devices_status_label)
        self._column.addWidget(computer_group)

        self._placeholder_label = QLabel(NO_PHONE_TEXT, self)
        self._column.addWidget(self._placeholder_label)
        self._column.addStretch()

        self._speaker_chooser.currentIndexChanged.connect(self._on_speaker_selected)
        self._microphone_chooser.currentIndexChanged.connect(self._on_microphone_selected)
        self.update_devices(False, [], [], CallAudioChoice())

    def update_devices(
        self,
        is_available: bool,
        speakers: list[AudioDevice],
        microphones: list[AudioDevice],
        choice: CallAudioChoice,
    ) -> None:
        """Refill the choosers with the computer's devices and select the stored choice.

        A chooser is rebuilt only when its entries change, so an open popup survives
        unrelated updates. A chosen device that is not listed is added as "(not
        available)" so the choice stays visible while that device is off.

        Args:
            is_available: Whether PipeWire is reachable; when not, the choosers are
                disabled and the status line says so.
            speakers: The computer's sink-like devices, in display order.
            microphones: The computer's source-like devices, in display order.
            choice: The stored devices; empty names select the system default.
        """
        self._devices_status_label.setVisible(not is_available)
        self._speaker_chooser.setEnabled(is_available)
        self._microphone_chooser.setEnabled(is_available)

        speaker_entries = _chooser_entries(speakers, choice.speaker_name)
        if speaker_entries != self._speaker_entries:
            _refill_chooser(self._speaker_chooser, speaker_entries)
            self._speaker_entries = speaker_entries
        _select_entry(self._speaker_chooser, choice.speaker_name)

        microphone_entries = _chooser_entries(microphones, choice.microphone_name)
        if microphone_entries != self._microphone_entries:
            _refill_chooser(self._microphone_chooser, microphone_entries)
            self._microphone_entries = microphone_entries
        _select_entry(self._microphone_chooser, choice.microphone_name)

    def _on_speaker_selected(self, _selected_index: int) -> None:
        """Announce the speaker the user picked."""
        self.speaker_device_changed.emit(_selected_name(self._speaker_chooser))

    def _on_microphone_selected(self, _selected_index: int) -> None:
        """Announce the microphone the user picked."""
        self.microphone_device_changed.emit(_selected_name(self._microphone_chooser))

    def update_gateways(
        self, gateways: list[AudioGateway], phone_info_by_address: dict[str, PhoneInfo]
    ) -> None:
        """Replace the controls with one group per gateway showing its current values.

        Args:
            gateways: Connected phones as seen by the telephony service.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
        """
        for group_box in self._group_boxes:
            self._column.removeWidget(group_box)
            group_box.deleteLater()
        self._group_boxes = []

        has_gateways = bool(gateways)
        self._placeholder_label.setVisible(not has_gateways)

        # Groups go between the placeholder and the trailing stretch, in gateway order.
        placeholder_index = self._column.indexOf(self._placeholder_label)
        for gateway in gateways:
            address_key = gateway.address.upper()
            phone_info = phone_info_by_address.get(address_key)
            group_box = self._build_gateway_group(gateway, phone_info)
            group_index = placeholder_index + 1 + len(self._group_boxes)
            self._group_boxes.append(group_box)
            self._column.insertWidget(group_index, group_box)

    def _build_gateway_group(
        self, gateway: AudioGateway, phone_info: PhoneInfo | None
    ) -> QGroupBox:
        """Create the controls for one gateway."""
        title = gateway.address
        if phone_info is not None and phone_info.alias:
            title = phone_info.alias
        group_box = QGroupBox(title, self)
        form = QFormLayout(group_box)

        speaker_row = self._build_volume_row(
            group_box, gateway.path, gateway.speaker_volume, self.speaker_volume_changed
        )
        form.addRow("Speaker", speaker_row)

        microphone_row = self._build_volume_row(
            group_box, gateway.path, gateway.microphone_volume, self.microphone_volume_changed
        )
        form.addRow("Microphone", microphone_row)

        audio_checkbox = QCheckBox("Play call audio on this computer", group_box)
        audio_on_computer = not gateway.reject_sco
        audio_checkbox.setChecked(audio_on_computer)
        audio_checkbox.toggled.connect(partial(self.audio_on_computer_changed.emit, gateway.path))
        form.addRow(audio_checkbox)

        has_audio_link = gateway.codec != NO_CODEC
        if has_audio_link:
            codec_name = CODEC_NAMES.get(gateway.codec, f"unknown ({gateway.codec})")
            form.addRow("Codec", QLabel(codec_name, group_box))

        return group_box

    def _build_volume_row(
        self, parent: QWidget, gateway_path: str, current_level: int, level_signal: Signal
    ) -> QWidget:
        """Create a slider over the HFP range with a live percentage label beside it."""
        row = QWidget(parent)
        slider = QSlider(Qt.Orientation.Horizontal, row)
        slider.setRange(MIN_VOLUME_LEVEL, MAX_VOLUME_LEVEL)
        slider.setValue(current_level)
        slider.setTracking(False)
        percent_label = QLabel(row)
        percent_label.setText(_percent_text(current_level))

        slider.sliderMoved.connect(lambda level: percent_label.setText(_percent_text(level)))
        slider.valueChanged.connect(lambda level: percent_label.setText(_percent_text(level)))
        slider.valueChanged.connect(partial(level_signal.emit, gateway_path))

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(slider)
        row_layout.addWidget(percent_label)

        return row


def _percent_text(level: int) -> str:
    """Format an HFP gain level as a percentage string."""
    percent = round(level * 100 / MAX_VOLUME_LEVEL)

    return f"{percent} %"


def _chooser_entries(devices: list[AudioDevice], chosen_name: str) -> list[tuple[str, str]]:
    """Build a chooser's (name, label) entries: default, the devices, an absent choice."""
    entries: list[tuple[str, str]] = [(SYSTEM_DEFAULT_DEVICE, SYSTEM_DEFAULT_TEXT)]
    is_choice_listed = chosen_name == SYSTEM_DEFAULT_DEVICE

    for device in devices:
        entries.append((device.name, device.description))
        if device.name == chosen_name:
            is_choice_listed = True

    if not is_choice_listed:
        absent_label = f"{chosen_name}{NOT_AVAILABLE_SUFFIX}"
        entries.append((chosen_name, absent_label))

    return entries


def _refill_chooser(chooser: QComboBox, entries: list[tuple[str, str]]) -> None:
    """Replace a chooser's items without announcing a selection."""
    chooser.blockSignals(True)
    chooser.clear()
    for device_name, label in entries:
        chooser.addItem(label, device_name)
    chooser.blockSignals(False)


def _select_entry(chooser: QComboBox, device_name: str) -> None:
    """Select the entry whose data is `device_name` without announcing it."""
    entry_index = chooser.findData(device_name)
    if entry_index < 0:
        return

    chooser.blockSignals(True)
    chooser.setCurrentIndex(entry_index)
    chooser.blockSignals(False)


def _selected_name(chooser: QComboBox) -> str:
    """Return the device name of the selected entry; empty for the system default."""
    current_data = chooser.currentData()
    if current_data is None:
        return SYSTEM_DEFAULT_DEVICE

    return str(current_data)
