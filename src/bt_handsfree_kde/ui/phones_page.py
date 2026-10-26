"""Phones tab of the settings window: the phones connected now and the ones paired before."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from bt_handsfree_kde.dbus.bluez import DEFAULT_PHONE_ICON_NAME, PhoneInfo
from bt_handsfree_kde.dbus.telephony import AudioGateway

# Pixel size of the icon at the start of each row.
ROW_ICON_SIZE = 32
# Group titles.
CONNECTED_GROUP_TITLE = "Connected now"
PREVIOUS_GROUP_TITLE = "Previously connected"
# Text shown when PipeWire's telephony service is missing.
SERVICE_UNAVAILABLE_TEXT = "PipeWire telephony service not available"
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"
# Text shown in the history group when BlueZ lists no other paired phone.
NO_OTHER_PHONE_TEXT = "No other phone is paired"
# Text shown in the history group while BlueZ cannot be reached.
BLUETOOTH_UNAVAILABLE_TEXT = "Bluetooth service not available"
# Connection state of a paired phone without a gateway, from BlueZ's `Connected`.
CONNECTED_WITHOUT_LINK_TEXT = "Connected, no hands-free link"
NOT_CONNECTED_TEXT = "Not connected"
# Separator between the parts of a row's detail line.
DETAIL_SEPARATOR = " · "


class PhonesPage(QWidget):
    """Lists the phones with a hands-free link and the other phones paired with the computer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the two empty groups; `update_phones` fills them."""
        super().__init__(parent)
        self._connected_group = QGroupBox(CONNECTED_GROUP_TITLE, self)
        self._connected_column = QVBoxLayout(self._connected_group)
        self._connected_rows: list[QWidget] = []
        self._previous_group = QGroupBox(PREVIOUS_GROUP_TITLE, self)
        self._previous_column = QVBoxLayout(self._previous_group)
        self._previous_rows: list[QWidget] = []

        column = QVBoxLayout(self)
        column.addWidget(self._connected_group)
        column.addWidget(self._previous_group)
        column.addStretch()

        self._replace_rows(
            self._connected_column, self._connected_rows, [_empty_row(NO_PHONE_TEXT, self)]
        )
        self._replace_rows(
            self._previous_column, self._previous_rows, [_empty_row(NO_OTHER_PHONE_TEXT, self)]
        )

    def update_phones(
        self,
        is_service_available: bool,
        gateways: list[AudioGateway],
        phone_info_by_address: dict[str, PhoneInfo],
        paired_phones: list[PhoneInfo],
        is_bluetooth_available: bool,
    ) -> None:
        """Rebuild both groups from the current state.

        A paired phone whose address has a gateway is listed under "Connected now"
        only; the rest of the paired phones form the history.

        Args:
            is_service_available: Whether the telephony service is on the bus.
            gateways: Connected phones as seen by the telephony service.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
            paired_phones: Every paired phone BlueZ knows, in display order.
            is_bluetooth_available: Whether BlueZ could be reached; when not, the history
                says so instead of showing an empty list.
        """
        connected_rows: list[QWidget] = []
        gateway_addresses: set[str] = set()
        for gateway in gateways:
            address_key = gateway.address.upper()
            gateway_addresses.add(address_key)
            phone_info = phone_info_by_address.get(address_key)
            connected_rows.append(self._build_connected_row(gateway, phone_info))

        if not connected_rows:
            empty_text = NO_PHONE_TEXT if is_service_available else SERVICE_UNAVAILABLE_TEXT
            connected_rows.append(_empty_row(empty_text, self))

        previous_rows: list[QWidget] = []
        for phone_info in paired_phones:
            address_key = phone_info.address.upper()
            has_gateway = address_key in gateway_addresses
            if not has_gateway:
                previous_rows.append(self._build_previous_row(phone_info))

        if not previous_rows:
            empty_text = (
                NO_OTHER_PHONE_TEXT if is_bluetooth_available else BLUETOOTH_UNAVAILABLE_TEXT
            )
            previous_rows.append(_empty_row(empty_text, self))

        self._replace_rows(self._connected_column, self._connected_rows, connected_rows)
        self._replace_rows(self._previous_column, self._previous_rows, previous_rows)

    def _replace_rows(
        self, column: QVBoxLayout, current_rows: list[QWidget], new_rows: list[QWidget]
    ) -> None:
        """Swap the widgets in one group's column for `new_rows`, in place."""
        for row_widget in current_rows:
            column.removeWidget(row_widget)
            row_widget.deleteLater()
        current_rows.clear()

        for row_widget in new_rows:
            column.addWidget(row_widget)
            current_rows.append(row_widget)

    def _build_connected_row(self, gateway: AudioGateway, phone_info: PhoneInfo | None) -> QWidget:
        """Create the row of a phone with a hands-free link: icon, name, address and battery."""
        has_phone_info = phone_info is not None
        title = gateway.address
        icon_name = ""
        detail_parts = [gateway.address]
        if has_phone_info:
            if phone_info.alias:
                title = phone_info.alias
            icon_name = phone_info.icon_name
            if phone_info.battery_percentage is not None:
                detail_parts.append(f"Battery {phone_info.battery_percentage} %")

        detail_text = DETAIL_SEPARATOR.join(detail_parts)

        return _phone_row(icon_name, title, detail_text, self)

    def _build_previous_row(self, phone_info: PhoneInfo) -> QWidget:
        """Create the row of a paired phone without a hands-free link right now."""
        title = phone_info.alias or phone_info.address
        state_text = CONNECTED_WITHOUT_LINK_TEXT if phone_info.connected else NOT_CONNECTED_TEXT
        detail_text = f"{phone_info.address}{DETAIL_SEPARATOR}{state_text}"

        return _phone_row(phone_info.icon_name, title, detail_text, self)


def _phone_row(icon_name: str, title: str, detail_text: str, parent: QWidget) -> QWidget:
    """Create a row with the phone's icon, its name in bold and a detail line below."""
    row_widget = QWidget(parent)

    fallback_icon = QIcon.fromTheme(DEFAULT_PHONE_ICON_NAME)
    row_icon = QIcon.fromTheme(icon_name, fallback_icon)
    icon_label = QLabel(row_widget)
    icon_label.setPixmap(row_icon.pixmap(ROW_ICON_SIZE, ROW_ICON_SIZE))
    icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)

    title_label = QLabel(title, row_widget)
    title_font = QFont()
    title_font.setBold(True)
    title_label.setFont(title_font)

    detail_label = QLabel(detail_text, row_widget)

    text_column = QVBoxLayout()
    text_column.addWidget(title_label)
    text_column.addWidget(detail_label)

    row = QHBoxLayout(row_widget)
    row.addWidget(icon_label)
    row.addLayout(text_column)
    row.addStretch()

    return row_widget


def _empty_row(text: str, parent: QWidget) -> QWidget:
    """Create the single line a group shows when it has nothing to list."""
    return QLabel(text, parent)
