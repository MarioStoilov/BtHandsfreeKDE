"""Tray icon and dropdown menu, served over the StatusNotifierItem and dbusmenu protocols."""

import asyncio
import logging
from functools import partial

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon

from bt_handsfree_kde import APPLICATION_ID, APPLICATION_NAME
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.dbus.dbusmenu import MENU_OBJECT_PATH, DBusMenuService, MenuItem
from bt_handsfree_kde.dbus.helpers import (
    DBUS_DAEMON_BUS_NAME,
    DBUS_DAEMON_INTERFACE,
    add_signal_match,
    name_has_owner,
)
from bt_handsfree_kde.dbus.statusnotifier import (
    STATUS_ACTIVE,
    STATUS_NEEDS_ATTENTION,
    STATUS_NOTIFIER_ITEM_PATH,
    WATCHER_BUS_NAME,
    StatusNotifierItemService,
    register_with_watcher,
    render_icon_pixmaps,
)
from bt_handsfree_kde.dbus.telephony import (
    CALL_STATE_ACTIVE,
    CALL_STATE_HELD,
    MAX_VOLUME_LEVEL,
    OUTGOING_PENDING_STATES,
    AudioGateway,
    Call,
)

logger = logging.getLogger(__name__)

# Theme icon names (freedesktop icon naming specification, all present in Breeze).
INCOMING_CALL_ICON = "call-incoming"
ACTIVE_CALL_ICON = "call-start"
ANSWER_ICON = "call-start"
HANGUP_ICON = "call-stop"
HOLD_ICON = "media-playback-pause"
RESUME_ICON = "media-playback-start"
PHONE_ICON = "smartphone"
DIALPAD_ICON = "input-dialpad"
CONTACTS_ICON = "view-pim-contacts"
SPEAKER_ICON = "audio-volume-high"
MICROPHONE_ICON = "audio-input-microphone"
QUIT_ICON = "application-exit"
# Text shown when PipeWire's telephony service is missing.
SERVICE_UNAVAILABLE_TEXT = "PipeWire telephony service not available"
# Text shown when the service runs but no phone is connected over HFP.
NO_PHONE_TEXT = "No phone connected"


class HandsfreeTray(QObject):
    """Owns the tray item and menu servers and rebuilds the menu from the current state."""

    # Call actions; the argument is the call's object path.
    answer_requested = Signal(str)
    reject_requested = Signal(str)
    hangup_requested = Signal(str)
    # Hold / resume acts on the gateway; the argument is the gateway's object path.
    hold_requested = Signal(str)
    # Clicking a call entry brings the call window to the front for that call path.
    call_focus_requested = Signal(str)
    # Audio routing toggle: (gateway path, audio on this computer).
    audio_on_computer_toggled = Signal(str, bool)
    # The volume entries open the settings window.
    settings_requested = Signal()
    # The Dialpad and Contacts entries open the main window on that tab.
    dialpad_requested = Signal()
    contacts_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self, bus: MessageBus, fallback_icon: QIcon, parent: QObject | None = None
    ) -> None:
        """Create the servers; nothing is visible until `start` registers the item.

        Args:
            bus: Connected session bus the servers are exported on.
            fallback_icon: Bundled icon rendered to pixmaps when the theme lacks the app icon.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._bus = bus
        self._fallback_pixmaps = render_icon_pixmaps(fallback_icon)
        self._is_service_available = False
        self._gateways: list[AudioGateway] = []
        self._calls: list[Call] = []
        self._phone_info_by_address: dict[str, PhoneInfo] = {}
        self._progress_text_by_call_path: dict[str, str] = {}

        self._menu_service = DBusMenuService()
        self._item_service = StatusNotifierItemService(
            APPLICATION_ID, APPLICATION_NAME, MENU_OBJECT_PATH
        )

    async def start(self) -> None:
        """Export both servers, register with the watcher and follow watcher restarts.

        Raises:
            DBusRequestError: the bus daemon refused the signal subscription.
        """
        self._bus.export(MENU_OBJECT_PATH, self._menu_service)
        self._bus.export(STATUS_NOTIFIER_ITEM_PATH, self._item_service)
        self._apply_state()

        self._bus.add_message_handler(self._handle_message)
        await add_signal_match(
            self._bus,
            f"type='signal',sender='{DBUS_DAEMON_BUS_NAME}',interface='{DBUS_DAEMON_INTERFACE}',"
            f"member='NameOwnerChanged',arg0='{WATCHER_BUS_NAME}'",
        )

        watcher_is_running = await name_has_owner(self._bus, WATCHER_BUS_NAME)
        if watcher_is_running:
            await register_with_watcher(self._bus)
        else:
            logger.warning(
                "no StatusNotifierWatcher on the bus; the tray icon appears once one starts"
            )

    def update_state(
        self,
        is_service_available: bool,
        gateways: list[AudioGateway],
        calls: list[Call],
        phone_info_by_address: dict[str, PhoneInfo],
        progress_text_by_call_path: dict[str, str],
    ) -> None:
        """Replace the displayed state and redraw icon, tooltip and menu.

        Args:
            is_service_available: Whether the telephony service is on the bus.
            gateways: Connected phones as seen by the telephony service.
            calls: Current calls across all gateways.
            phone_info_by_address: BlueZ details keyed by upper-case Bluetooth address.
            progress_text_by_call_path: Per call, its elapsed duration or dialing state.
        """
        self._is_service_available = is_service_available
        self._gateways = list(gateways)
        self._calls = list(calls)
        self._phone_info_by_address = dict(phone_info_by_address)
        self._progress_text_by_call_path = dict(progress_text_by_call_path)

        self._apply_state()

    def _handle_message(self, message: Message) -> None:
        """Re-register when the watcher restarts; returns `None` so other handlers run too."""
        is_owner_change = (
            message.message_type == MessageType.SIGNAL
            and message.interface == DBUS_DAEMON_INTERFACE
            and message.member == "NameOwnerChanged"
        )
        if not is_owner_change:
            return None

        changed_name = message.body[0]
        new_owner = message.body[2]
        watcher_appeared = changed_name == WATCHER_BUS_NAME and new_owner != ""
        if watcher_appeared:
            logger.info("StatusNotifierWatcher appeared, registering the tray icon")
            asyncio.get_event_loop().create_task(register_with_watcher(self._bus))

        return None

    def _apply_state(self) -> None:
        """Push menu, icon, status and tooltip derived from the stored state to the servers."""
        menu_items = self._build_menu()
        self._menu_service.set_items(menu_items)

        has_incoming_call = any(call.is_incoming for call in self._calls)
        has_any_call = bool(self._calls)
        if has_incoming_call:
            self._item_service.set_attention_icon(INCOMING_CALL_ICON)
            self._item_service.set_icon(INCOMING_CALL_ICON, [])
            self._item_service.set_status(STATUS_NEEDS_ATTENTION)
        elif has_any_call:
            self._item_service.set_icon(ACTIVE_CALL_ICON, [])
            self._item_service.set_status(STATUS_ACTIVE)
        else:
            self._set_idle_icon()
            self._item_service.set_status(STATUS_ACTIVE)

        tooltip_text = self._tooltip_text()
        self._item_service.set_tooltip(APPLICATION_NAME, tooltip_text)

    def _set_idle_icon(self) -> None:
        """Use the installed theme icon when present, otherwise the bundled pixmaps."""
        theme_has_app_icon = QIcon.hasThemeIcon(APPLICATION_ID)

        if theme_has_app_icon:
            self._item_service.set_icon(APPLICATION_ID, [])
        else:
            self._item_service.set_icon("", self._fallback_pixmaps)

    def _build_menu(self) -> list[MenuItem]:
        """Create the menu tree for the stored state."""
        quit_item = MenuItem("Quit", QUIT_ICON, on_activated=self.quit_requested.emit)

        if not self._is_service_available:
            return [
                MenuItem(SERVICE_UNAVAILABLE_TEXT, enabled=False),
                MenuItem.separator(),
                quit_item,
            ]

        menu_items: list[MenuItem] = []
        if not self._gateways:
            menu_items.append(MenuItem(NO_PHONE_TEXT, enabled=False))
            menu_items.append(MenuItem.separator())

        for gateway in self._gateways:
            header_text = self._phone_header(gateway)
            header_key = f"phone:{gateway.path}"
            menu_items.append(MenuItem(header_text, PHONE_ICON, key=header_key, enabled=False))

            for call in self._calls:
                if call.gateway_path == gateway.path:
                    menu_items.extend(self._call_items(call))

            menu_items.append(MenuItem.separator())
            menu_items.extend(self._gateway_items(gateway))
            menu_items.append(MenuItem.separator())

        # The tabs of the main window are not tied to one phone, so they come after
        # the phone sections.
        dialpad_item = MenuItem("Dialpad", DIALPAD_ICON, on_activated=self.dialpad_requested.emit)
        contacts_item = MenuItem(
            "Contacts", CONTACTS_ICON, on_activated=self.contacts_requested.emit
        )
        menu_items.append(dialpad_item)
        menu_items.append(contacts_item)
        menu_items.append(MenuItem.separator())

        menu_items.append(quit_item)

        return menu_items

    def _call_items(self, call: Call) -> list[MenuItem]:
        """Return the clickable description and the applicable actions for one call.

        Keys carry the call path so the host updates the entries in place while the
        duration ticks, instead of rebuilding the menu every second.
        """
        progress_text = self._progress_text_by_call_path.get(call.path, call.state)
        focus_call = partial(self.call_focus_requested.emit, call.path)
        description_key = f"call:{call.path}"
        hangup_item = MenuItem(
            "Hang up",
            HANGUP_ICON,
            key=f"hangup:{call.path}",
            on_activated=partial(self.hangup_requested.emit, call.path),
        )
        toggle_hold = partial(self.hold_requested.emit, call.gateway_path)

        if call.is_incoming:
            return [
                MenuItem(
                    f"Incoming: {call.caller_label}",
                    INCOMING_CALL_ICON,
                    key=description_key,
                    on_activated=focus_call,
                ),
                MenuItem(
                    "Answer",
                    ANSWER_ICON,
                    key=f"answer:{call.path}",
                    on_activated=partial(self.answer_requested.emit, call.path),
                ),
                MenuItem(
                    "Reject",
                    HANGUP_ICON,
                    key=f"reject:{call.path}",
                    on_activated=partial(self.reject_requested.emit, call.path),
                ),
            ]

        if call.state in OUTGOING_PENDING_STATES:
            description = MenuItem(
                f"Calling: {call.caller_label} · {progress_text}",
                ACTIVE_CALL_ICON,
                key=description_key,
                on_activated=focus_call,
            )
            return [description, hangup_item]

        if call.state == CALL_STATE_HELD:
            description = MenuItem(
                f"On hold: {call.caller_label} · {progress_text}",
                HOLD_ICON,
                key=description_key,
                on_activated=focus_call,
            )
            resume_item = MenuItem(
                "Resume", RESUME_ICON, key=f"hold:{call.path}", on_activated=toggle_hold
            )
            return [description, resume_item, hangup_item]

        if call.state == CALL_STATE_ACTIVE:
            description = MenuItem(
                f"In call: {call.caller_label} · {progress_text}",
                ACTIVE_CALL_ICON,
                key=description_key,
                on_activated=focus_call,
            )
            hold_item = MenuItem(
                "Hold", HOLD_ICON, key=f"hold:{call.path}", on_activated=toggle_hold
            )
            return [description, hold_item, hangup_item]

        description = MenuItem(
            f"{call.state}: {call.caller_label}", key=description_key, on_activated=focus_call
        )
        return [description, hangup_item]

    def _gateway_items(self, gateway: AudioGateway) -> list[MenuItem]:
        """Return the volume entries (which open settings) and the audio routing toggle."""
        speaker_percent = volume_percent(gateway.speaker_volume)
        microphone_percent = volume_percent(gateway.microphone_volume)
        audio_on_computer = not gateway.reject_sco
        toggle_audio = partial(
            self.audio_on_computer_toggled.emit, gateway.path, not audio_on_computer
        )

        return [
            MenuItem(
                f"Speaker {speaker_percent} %",
                SPEAKER_ICON,
                key=f"speaker:{gateway.path}",
                on_activated=self.settings_requested.emit,
            ),
            MenuItem(
                f"Microphone {microphone_percent} %",
                MICROPHONE_ICON,
                key=f"microphone:{gateway.path}",
                on_activated=self.settings_requested.emit,
            ),
            MenuItem(
                "Call audio on this computer",
                key=f"audio:{gateway.path}",
                checkable=True,
                checked=audio_on_computer,
                on_activated=toggle_audio,
            ),
        ]

    def _phone_header(self, gateway: AudioGateway) -> str:
        """Describe a gateway as phone name plus battery, falling back to its address."""
        address_key = gateway.address.upper()
        phone_info = self._phone_info_by_address.get(address_key)

        if phone_info is None or not phone_info.alias:
            return gateway.address

        if phone_info.battery_percentage is None:
            return phone_info.alias

        return f"{phone_info.alias} · {phone_info.battery_percentage} %"

    def _tooltip_text(self) -> str:
        """Summarise service, phones and calls for the tooltip body."""
        tooltip_lines: list[str] = []

        if not self._is_service_available:
            tooltip_lines.append(SERVICE_UNAVAILABLE_TEXT)
        elif not self._gateways:
            tooltip_lines.append(NO_PHONE_TEXT)
        for gateway in self._gateways:
            header_text = self._phone_header(gateway)
            tooltip_lines.append(header_text)
        for call in self._calls:
            tooltip_lines.append(f"{call.state}: {call.caller_label}")

        return "\n".join(tooltip_lines)


def volume_percent(level: int) -> int:
    """Convert an HFP gain level (0..15) to a rounded percentage."""
    percent = round(level * 100 / MAX_VOLUME_LEVEL)

    return percent
