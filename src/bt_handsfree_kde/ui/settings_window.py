"""Settings window: per-phone call volume sliders and audio routing."""

from functools import partial

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.dbus.telephony import MAX_VOLUME_LEVEL, MIN_VOLUME_LEVEL, AudioGateway

# HFP codec identifiers as reported in `AudioGatewayTransport1.Codec`.
CODEC_NAMES = {1: "CVSD", 2: "mSBC", 3: "LC3-SWB"}
# Codec value while no audio link is open.
NO_CODEC = 0
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"


class SettingsWindow(QWidget):
    """One group of controls per connected phone; rebuilt whenever the gateways change."""

    # Emitted with (gateway path, level 0..15) when a slider is released.
    speaker_volume_changed = Signal(str, int)
    microphone_volume_changed = Signal(str, int)
    # Emitted with (gateway path, audio on this computer) when the checkbox toggles.
    audio_on_computer_changed = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the empty window; `update_gateways` fills it."""
        super().__init__(parent)
        self.setWindowTitle(f"{APPLICATION_NAME} settings")
        self._column = QVBoxLayout(self)
        self._group_boxes: list[QGroupBox] = []
        self._placeholder_label = QLabel(NO_PHONE_TEXT, self)
        self._column.addWidget(self._placeholder_label)

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

        for gateway in gateways:
            address_key = gateway.address.upper()
            phone_info = phone_info_by_address.get(address_key)
            group_box = self._build_gateway_group(gateway, phone_info)
            self._group_boxes.append(group_box)
            self._column.addWidget(group_box)

        self.adjustSize()

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
