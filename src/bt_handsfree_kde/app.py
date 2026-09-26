"""Wires the D-Bus clients, the tray, the windows and the notifications together."""

import asyncio
import logging
import time
from collections.abc import Coroutine
from dataclasses import replace
from typing import Any

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from bt_handsfree_kde.contacts.client import (
    AUTOMATIC_SYNC_DELAY_S as CONTACTS_SYNC_DELAY_S,
)
from bt_handsfree_kde.contacts.client import ContactsClient, PhonebookState
from bt_handsfree_kde.dbus.bluez import PhoneInfo, PhoneInfoClient
from bt_handsfree_kde.dbus.helpers import DBusRequestError
from bt_handsfree_kde.dbus.instance import SingleInstance
from bt_handsfree_kde.dbus.notifications import (
    ANSWER_ACTION,
    OPEN_MESSAGE_ACTION,
    REJECT_ACTION,
    URGENCY_NORMAL,
    DesktopNotifier,
)
from bt_handsfree_kde.dbus.obex import ObexClient
from bt_handsfree_kde.dbus.telephony import (
    CALL_STATE_ACTIVE,
    CALL_STATE_ALERTING,
    CALL_STATE_DIALING,
    CALL_STATE_DISCONNECTED,
    CALL_STATE_HELD,
    Call,
    TelephonyClient,
)
from bt_handsfree_kde.icons import application_icon
from bt_handsfree_kde.messages.client import (
    AUTOMATIC_SYNC_DELAY_S as MESSAGES_SYNC_DELAY_S,
)
from bt_handsfree_kde.messages.client import MessagesClient, MessagesState
from bt_handsfree_kde.messages.conversations import Conversation, group_conversations
from bt_handsfree_kde.messages.message import TextMessage
from bt_handsfree_kde.phone_numbers import number_from_tel_uri
from bt_handsfree_kde.ui.about_window import AboutWindow
from bt_handsfree_kde.ui.call_window import CallWindow
from bt_handsfree_kde.ui.main_window import MainWindow
from bt_handsfree_kde.ui.settings_window import SettingsWindow
from bt_handsfree_kde.ui.tray import HandsfreeTray

logger = logging.getLogger(__name__)

# How often the duration shown for a call in progress is refreshed.
CALL_STATUS_REFRESH_INTERVAL_MS = 1000
# Which call the window shows when several exist: lower rank wins.
CALL_WINDOW_PRIORITY = {
    CALL_STATE_ACTIVE: 0,
    CALL_STATE_DIALING: 1,
    CALL_STATE_ALERTING: 1,
    CALL_STATE_HELD: 2,
}
# Rank for states not listed above (incoming calls and anything unexpected).
LOWEST_CALL_PRIORITY = 3
# Process exit code when a second launch could not reach the running instance.
HANDOFF_FAILED_EXIT_CODE = 1
# Longest message preview shown in a new-message notification, in characters.
MESSAGE_NOTIFICATION_PREVIEW_LIMIT = 200
# Marks a cut preview.
ELLIPSIS = "…"


class HandsfreeApplication(QObject):
    """Top-level coordinator: owns the clients and the UI, and routes events between them."""

    def __init__(
        self, qt_application: QApplication, launch_uris: list[str], parent: QObject | None = None
    ) -> None:
        """Create the windows immediately; D-Bus connections are made in `start`.

        Args:
            qt_application: The running Qt application, used to quit from the tray.
            launch_uris: Command-line arguments: `tel:` URIs whose number opens the dialpad.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._qt_application = qt_application
        self._qt_application.setWindowIcon(application_icon())
        self._launch_uris = list(launch_uris)
        # Exit code for the process; set before quitting when a hand-off to the running
        # instance fails, otherwise 0.
        self.exit_code = 0
        self._session_bus: MessageBus | None = None
        self._instance: SingleInstance | None = None
        self._telephony: TelephonyClient | None = None
        self._obex: ObexClient | None = None
        self._contacts: ContactsClient | None = None
        self._messages: MessagesClient | None = None
        self._notifier: DesktopNotifier | None = None
        self._tray: HandsfreeTray | None = None
        self._phones = PhoneInfoClient(self)

        # Monotonic timestamp at which each call became active, keyed by call path.
        self._call_started_at: dict[str, float] = {}
        # Addresses of gateways seen so far, to announce connect and disconnect once each.
        self._known_gateway_addresses: set[str] = set()
        # Call the user picked from the menu; shown in the call window while it exists.
        self._focused_call_path = ""
        # Where a new-message notification's Open button leads: (phone address,
        # conversation key) keyed by the message path the notification was shown for.
        self._message_notification_targets: dict[str, tuple[str, str]] = {}

        self._call_window = CallWindow()
        self._settings_window = SettingsWindow()
        self._main_window = MainWindow()
        self._about_window = AboutWindow()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(CALL_STATUS_REFRESH_INTERVAL_MS)

        self._call_window.answer_requested.connect(self._answer_call)
        self._call_window.hangup_requested.connect(self._hangup_call)
        self._call_window.hold_requested.connect(self._toggle_hold_for_call)
        self._call_window.tone_requested.connect(self._send_tone)
        self._settings_window.speaker_volume_changed.connect(self._set_speaker_volume)
        self._settings_window.microphone_volume_changed.connect(self._set_microphone_volume)
        self._settings_window.audio_on_computer_changed.connect(self._set_audio_on_computer)
        self._main_window.dial_requested.connect(self._dial)
        self._main_window.tone_requested.connect(self._send_tone_on_gateway)
        self._main_window.refresh_contacts_requested.connect(self._refresh_contacts)
        self._main_window.refresh_messages_requested.connect(self._refresh_messages)
        self._main_window.conversation_opened.connect(self._on_conversation_opened)
        self._status_timer.timeout.connect(self._on_status_tick)
        self._phones.phone_info_changed.connect(self._on_phone_info_changed)

    async def start(self) -> None:
        """Connect to both buses, start the clients and the tray, and show the initial state.

        When another process already owns the app's bus name, this one hands it the
        `tel:` number from the command line (or just asks for the dialpad) and quits.
        """
        self._session_bus = await MessageBus(bus_type=BusType.SESSION).connect()

        # The instance check comes first: a second process must quit before it puts a
        # tray icon on the bus or subscribes to anything.
        self._instance = SingleInstance(self._session_bus, self)
        self._instance.dialpad_requested.connect(self._show_dialpad)
        is_single_instance = await self._instance.claim()
        if not is_single_instance:
            await self._hand_off_and_quit()
            return

        self._telephony = TelephonyClient(self._session_bus, self)
        self._telephony.availability_changed.connect(self._on_availability_changed)
        self._telephony.gateways_changed.connect(self._on_gateways_changed)
        self._telephony.call_added.connect(self._on_call_added)
        self._telephony.call_changed.connect(self._on_call_changed)
        self._telephony.call_removed.connect(self._on_call_removed)

        self._obex = ObexClient(self._session_bus, self)
        self._contacts = ContactsClient(self._obex, self)
        self._contacts.phonebook_changed.connect(self._on_phonebook_changed)
        self._messages = MessagesClient(self._obex, self)
        self._messages.messages_changed.connect(self._on_messages_changed)
        self._messages.message_received.connect(self._on_message_received)

        self._notifier = DesktopNotifier(self._session_bus, self)
        self._notifier.action_invoked.connect(self._on_notification_action)

        self._tray = HandsfreeTray(self._session_bus, application_icon(), self)
        self._tray.quit_requested.connect(self._qt_application.quit)
        self._tray.answer_requested.connect(self._answer_call)
        self._tray.reject_requested.connect(self._hangup_call)
        self._tray.hangup_requested.connect(self._hangup_call)
        self._tray.hold_requested.connect(self._toggle_hold_on_gateway)
        self._tray.audio_on_computer_toggled.connect(self._set_audio_on_computer)
        self._tray.settings_requested.connect(self._show_settings)
        self._tray.dialpad_requested.connect(self._show_dialpad)
        self._tray.contacts_requested.connect(self._show_contacts)
        self._tray.messages_requested.connect(self._show_messages)
        self._tray.about_requested.connect(self._about_window.show_and_raise)
        self._tray.call_focus_requested.connect(self._focus_call)

        # The notifier, BlueZ, the tray and the obexd client come up before telephony so
        # the very first call and gateway events can already be presented with names and
        # battery, and the first gateway can start its phonebook and message syncs.
        await self._notifier.start()
        await self._phones.start()
        await self._tray.start()
        await self._obex.start()
        await self._telephony.start()

        self._refresh_views()

        # A tel: URI on the first launch opens the dialpad only now, once the phones are
        # known, so the Call button is enabled right away.
        if self._launch_uris:
            launch_number = self._number_from_launch_uris()
            self._show_dialpad(launch_number)

    def stop(self) -> None:
        """Close the bus connections once the Qt loop has finished.

        obexd drops the app's sessions and the tray icon disappears as a consequence of
        the session connection closing; nothing is sent first.
        """
        self._phones.stop()
        if self._session_bus is not None:
            self._session_bus.disconnect()
            self._session_bus = None

    async def _hand_off_and_quit(self) -> None:
        """Pass the launch request to the running instance, then quit this process."""
        launch_number = self._number_from_launch_uris()

        try:
            await self._instance.show_dialpad_in_running_instance(launch_number)
            logger.info("handed over to the running instance")
        except DBusRequestError as request_error:
            logger.error("the running instance could not be reached: %s", request_error)
            self.exit_code = HANDOFF_FAILED_EXIT_CODE

        self._session_bus.disconnect()
        self._qt_application.quit()

    def _number_from_launch_uris(self) -> str:
        """Return the dial string of the first usable `tel:` URI from the command line.

        Arguments that are not `tel:` URIs are reported without their content, which
        may be personal, and skipped; the result is empty when none is usable.
        """
        for launch_uri in self._launch_uris:
            number = number_from_tel_uri(launch_uri)
            if number:
                return number
            logger.warning("ignoring a command-line argument that is not a tel: URI")

        return ""

    def _on_availability_changed(self, is_available: bool) -> None:
        """Redraw and tell the user when the telephony service goes away."""
        self._refresh_views()

        if not is_available:
            self._run(
                self._notifier.show_information(
                    "Telephony service unavailable",
                    "PipeWire's telephony service is not running; calls cannot be controlled.",
                    URGENCY_NORMAL,
                )
            )

    def _on_gateways_changed(self) -> None:
        """Redraw, announce phones that connected or disconnected, and sync their data.

        A phone that just connected gets its phonebook pulled and its message session
        opened after short delays; a phone that disconnected has both dropped, along
        with the new-message notifications that would open its conversations. Its
        calls were withdrawn by the telephony client before this point, which closed
        their notifications.
        """
        current_addresses: set[str] = set()
        for gateway in self._telephony.gateways:
            current_addresses.add(gateway.address.upper())

        newly_connected = current_addresses - self._known_gateway_addresses
        newly_disconnected = self._known_gateway_addresses - current_addresses
        self._known_gateway_addresses = current_addresses

        for address in newly_connected:
            phone_label = self._phone_label(address)
            self._run(self._notifier.show_information("Phone connected", phone_label))
            self._contacts.start_sync(address, CONTACTS_SYNC_DELAY_S)
            self._messages.start_sync(address, MESSAGES_SYNC_DELAY_S)
        for address in newly_disconnected:
            phone_label = self._phone_label(address)
            self._run(self._notifier.show_information("Phone disconnected", phone_label))
            self._contacts.forget(address)
            self._messages.forget(address)
            self._withdraw_message_notifications(address)

        self._refresh_views()

    def _withdraw_message_notifications(self, address: str) -> None:
        """Close the new-message notifications shown for the phone at `address`."""
        address_key = address.upper()
        withdrawn_message_paths: list[str] = []
        for message_path, target in self._message_notification_targets.items():
            target_address = target[0]
            if target_address == address_key:
                withdrawn_message_paths.append(message_path)

        for message_path in withdrawn_message_paths:
            del self._message_notification_targets[message_path]
            self._run(self._notifier.close_for_key(message_path))

    def _on_phonebook_changed(self, _address: str) -> None:
        """Redraw once a phonebook arrives, so calls and the tabs show the names."""
        self._present_calls()
        self._refresh_views()

    def _on_messages_changed(self, _address: str) -> None:
        """Redraw once messages arrive or change."""
        self._refresh_views()

    def _on_message_received(self, address: str, message: TextMessage) -> None:
        """Notify about a message the phone just received, with a button to open it."""
        sender_label = self._contacts.lookup_name(address, message.counterpart_address)
        if not sender_label:
            has_phone_name = (
                bool(message.counterpart_name)
                and message.counterpart_name != message.counterpart_address
            )
            sender_label = message.counterpart_name if has_phone_name else ""
        if not sender_label:
            sender_label = message.counterpart_address or "Unknown sender"

        preview = _preview_text(message.text, MESSAGE_NOTIFICATION_PREVIEW_LIMIT)
        conversation = self._conversation_containing(address, message.path)
        conversation_key = conversation.key if conversation is not None else ""
        self._message_notification_targets[message.path] = (address.upper(), conversation_key)
        phone_label = self._phone_label_when_several(address)

        self._run(self._notifier.show_new_message(message.path, sender_label, preview, phone_label))

    def _on_conversation_opened(self, gateway_path: str, conversation_key: str) -> None:
        """Mark an opened conversation read on the phone and fetch its long messages."""
        gateway = self._telephony.gateway_for(gateway_path)
        if gateway is None:
            return

        conversation = None
        for candidate in self._conversations_for(gateway.address):
            if candidate.key == conversation_key:
                conversation = candidate
        if conversation is None:
            return

        for message_path in conversation.incomplete_message_paths:
            self._run(self._messages.fetch_full_text(gateway.address, message_path))
        unread_paths = conversation.unread_message_paths
        if unread_paths:
            self._run(self._messages.mark_read(gateway.address, unread_paths))
        for message_path in unread_paths:
            self._message_notification_targets.pop(message_path, None)
            self._run(self._notifier.close_for_key(message_path))

    def _on_call_added(self, call: Call) -> None:
        """Present a new call and start the status refresh timer."""
        self._note_call_start(call)
        self._present_calls()
        self._status_timer.start()
        self._refresh_views()

    def _on_call_changed(self, call: Call) -> None:
        """Present the calls again after one changed state."""
        self._note_call_start(call)
        self._present_calls()
        self._refresh_views()

    def _on_call_removed(self, call_path: str) -> None:
        """Drop everything shown for a finished call."""
        self._call_started_at.pop(call_path, None)
        if self._focused_call_path == call_path:
            self._focused_call_path = ""
        self._run(self._notifier.close_for_call(call_path))
        self._present_calls()

        remaining_calls = self._telephony.calls
        if not remaining_calls:
            self._status_timer.stop()

        self._refresh_views()

    def _on_phone_info_changed(self, phone_info: PhoneInfo) -> None:
        """Redraw when a phone that has a gateway changes name, state or battery."""
        address_key = phone_info.address.upper()
        if address_key in self._known_gateway_addresses:
            self._refresh_views()

    def _on_notification_action(self, subject_key: str, action_key: str) -> None:
        """Route a pressed notification button to the matching call or message action."""
        if action_key == ANSWER_ACTION:
            self._answer_call(subject_key)
        elif action_key == REJECT_ACTION:
            self._hangup_call(subject_key)
        elif action_key == OPEN_MESSAGE_ACTION:
            self._open_message_notification(subject_key)

    def _open_message_notification(self, message_path: str) -> None:
        """Show the conversation a new-message notification was about."""
        target = self._message_notification_targets.pop(message_path, None)
        if target is None:
            self._show_messages()
            return

        address, conversation_key = target
        gateway_path = ""
        for gateway in self._telephony.gateways:
            if gateway.address.upper() == address:
                gateway_path = gateway.path

        self._main_window.show_messages(gateway_path, conversation_key)

    def _on_status_tick(self) -> None:
        """Timer tick: refresh the durations in the call window and the menu."""
        self._present_calls()
        self._refresh_tray()

    def _present_calls(self) -> None:
        """Show the right notification and window content for the current calls.

        Ringing calls get a notification with Answer and Reject when the server supports
        buttons; otherwise the call window shows them. Every other call is shown in the
        call window, the most relevant one when there are several, unless the user
        picked one from the menu.
        """
        live_calls: list[Call] = []
        for call in self._calls_with_contact_names():
            if call.state == CALL_STATE_DISCONNECTED:
                self._run(self._notifier.close_for_call(call.path))
            else:
                live_calls.append(call)

        notifications_supported = self._notifier.supports_call_notifications
        window_candidates: list[Call] = []
        for call in live_calls:
            is_focused = call.path == self._focused_call_path
            if call.is_incoming and notifications_supported:
                phone_label = self._phone_label_for_gateway_when_several(call.gateway_path)
                self._run(
                    self._notifier.show_incoming_call(call.path, call.caller_label, phone_label)
                )
                if is_focused:
                    window_candidates.append(call)
            else:
                self._run(self._notifier.close_for_call(call.path))
                window_candidates.append(call)

        if not window_candidates:
            self._call_window.hide_call()
            return

        window_candidates.sort(key=self._call_window_rank)
        shown_call = window_candidates[0]
        status_text = self._progress_text(shown_call)
        if shown_call.state == CALL_STATE_HELD:
            status_text = f"On hold · {status_text}"
        self._call_window.show_call(shown_call, shown_call.caller_label, status_text)

    def _call_window_rank(self, call: Call) -> int:
        """Sort key choosing which call the window shows: the focused one, then by state."""
        if call.path == self._focused_call_path:
            return -1

        return CALL_WINDOW_PRIORITY.get(call.state, LOWEST_CALL_PRIORITY)

    def _focus_call(self, call_path: str) -> None:
        """Show `call_path` in the call window and bring the window to the front."""
        self._focused_call_path = call_path
        self._present_calls()
        self._call_window.raise_()
        self._call_window.activateWindow()

    def _progress_text(self, call: Call) -> str:
        """Describe the progress of a call: ringing, dialing or elapsed time."""
        if call.is_incoming:
            return "Incoming call"

        if call.state == CALL_STATE_DIALING:
            return "Dialing…"

        if call.state == CALL_STATE_ALERTING:
            return "Ringing…"

        started_at = self._call_started_at.get(call.path)
        if started_at is None:
            return call.state

        elapsed_seconds = int(time.monotonic() - started_at)
        duration_text = _format_duration(elapsed_seconds)

        return duration_text

    def _note_call_start(self, call: Call) -> None:
        """Remember when a call first became active so its duration can be shown."""
        is_first_activation = (
            call.state == CALL_STATE_ACTIVE and call.path not in self._call_started_at
        )
        if is_first_activation:
            self._call_started_at[call.path] = time.monotonic()

    def _refresh_views(self) -> None:
        """Push the combined state to the tray, the settings window and the main window."""
        self._refresh_tray()
        self._refresh_settings()
        self._refresh_main_window()

    def _call_with_contact_name(self, call: Call) -> Call:
        """Fill in the caller's name from the synced phonebook when the phone sent none.

        The hands-free profile carries numbers only on most phones; the name comes from
        the phonebook of the gateway that carries the call, matched by number.
        """
        has_name = bool(call.name)
        has_number = bool(call.line_identification)
        if has_name or not has_number:
            return call

        gateway = self._telephony.gateway_for(call.gateway_path)
        if gateway is None:
            return call

        contact_name = self._contacts.lookup_name(gateway.address, call.line_identification)
        if not contact_name:
            return call

        return replace(call, name=contact_name)

    def _calls_with_contact_names(self) -> list[Call]:
        """Return the current calls with caller names filled in from the phonebooks."""
        named_calls: list[Call] = []

        for call in self._telephony.calls:
            named_calls.append(self._call_with_contact_name(call))

        return named_calls

    def _phone_info_by_address(self) -> dict[str, PhoneInfo]:
        """Collect BlueZ details for every gateway, keyed by upper-case address."""
        phone_info_by_address: dict[str, PhoneInfo] = {}

        for gateway in self._telephony.gateways:
            phone_info = self._phones.info_for_address(gateway.address)
            if phone_info is not None:
                address_key = gateway.address.upper()
                phone_info_by_address[address_key] = phone_info

        return phone_info_by_address

    def _refresh_tray(self) -> None:
        """Redraw the tray icon and menu, including per-call progress texts."""
        named_calls = self._calls_with_contact_names()
        progress_text_by_call_path: dict[str, str] = {}
        for call in named_calls:
            progress_text_by_call_path[call.path] = self._progress_text(call)

        unread_message_count = 0
        for gateway in self._telephony.gateways:
            for conversation in self._conversations_for(gateway.address):
                unread_message_count += conversation.unread_count

        phone_info_by_address = self._phone_info_by_address()
        self._tray.update_state(
            self._telephony.is_available,
            self._telephony.gateways,
            named_calls,
            phone_info_by_address,
            progress_text_by_call_path,
            unread_message_count,
        )

    def _conversations_for(self, address: str) -> list[Conversation]:
        """Group the phone's messages into conversations, named from its phonebook."""
        messages_state = self._messages.state_for_address(address)

        def lookup_name(counterpart_address: str) -> str:
            return self._contacts.lookup_name(address, counterpart_address)

        return group_conversations(list(messages_state.messages), lookup_name)

    def _conversation_containing(self, address: str, message_path: str) -> Conversation | None:
        """Return the phone's conversation that holds the message at `message_path`."""
        for conversation in self._conversations_for(address):
            for message in conversation.messages:
                if message.path == message_path:
                    return conversation

        return None

    def _refresh_settings(self) -> None:
        """Rebuild the settings window for the current gateways."""
        phone_info_by_address = self._phone_info_by_address()
        self._settings_window.update_gateways(self._telephony.gateways, phone_info_by_address)

    def _refresh_main_window(self) -> None:
        """Give the main window the phones, their calls, phonebooks and conversations."""
        active_call_label_by_gateway_path: dict[str, str] = {}
        for call in self._calls_with_contact_names():
            if call.state == CALL_STATE_ACTIVE:
                active_call_label_by_gateway_path[call.gateway_path] = call.caller_label

        phonebook_state_by_gateway_path: dict[str, PhonebookState] = {}
        messages_state_by_gateway_path: dict[str, MessagesState] = {}
        conversations_by_gateway_path: dict[str, list[Conversation]] = {}
        for gateway in self._telephony.gateways:
            phonebook_state = self._contacts.state_for_address(gateway.address)
            phonebook_state_by_gateway_path[gateway.path] = phonebook_state
            messages_state = self._messages.state_for_address(gateway.address)
            messages_state_by_gateway_path[gateway.path] = messages_state
            conversations_by_gateway_path[gateway.path] = self._conversations_for(gateway.address)

        phone_info_by_address = self._phone_info_by_address()
        self._main_window.update_state(
            self._telephony.is_available,
            self._telephony.gateways,
            phone_info_by_address,
            active_call_label_by_gateway_path,
            phonebook_state_by_gateway_path,
            messages_state_by_gateway_path,
            conversations_by_gateway_path,
        )

    def _show_settings(self) -> None:
        """Open or raise the settings window."""
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _show_dialpad(self, number: str = "") -> None:
        """Open or raise the main window on the Dialpad tab, prefilled when a number is given."""
        self._main_window.show_dialpad(number)

    def _show_contacts(self) -> None:
        """Open or raise the main window on the Contacts tab."""
        self._main_window.show_contacts()

    def _show_messages(self) -> None:
        """Open or raise the main window on the Messages tab."""
        self._main_window.show_messages()

    def _refresh_contacts(self, gateway_path: str) -> None:
        """Pull the phonebook of the phone at `gateway_path` again."""
        gateway = self._telephony.gateway_for(gateway_path)
        if gateway is not None:
            self._contacts.start_sync(gateway.address)

    def _refresh_messages(self, gateway_path: str) -> None:
        """List the newest messages of the phone at `gateway_path` again."""
        gateway = self._telephony.gateway_for(gateway_path)
        if gateway is not None:
            self._messages.start_sync(gateway.address)

    def _phone_label(self, address: str) -> str:
        """Return the phone's BlueZ alias, or its address when BlueZ has no entry."""
        phone_info = self._phones.info_for_address(address)
        if phone_info is None or not phone_info.alias:
            return address

        return phone_info.alias

    def _phone_label_when_several(self, address: str) -> str:
        """Return the phone's label for a notification, or empty with a single phone.

        Call and message notifications name their phone only when the user has more
        than one connected, so the common case stays short.
        """
        has_several_phones = len(self._telephony.gateways) > 1
        if not has_several_phones:
            return ""

        return self._phone_label(address)

    def _phone_label_for_gateway_when_several(self, gateway_path: str) -> str:
        """Return the label of the phone at `gateway_path` for a notification, see above."""
        gateway = self._telephony.gateway_for(gateway_path)
        if gateway is None:
            return ""

        return self._phone_label_when_several(gateway.address)

    def _answer_call(self, call_path: str) -> None:
        """Answer `call_path`, holding an active call first when the phone has one."""
        call = self._telephony.call_for(call_path)
        if call is None:
            return

        has_active_call_on_gateway = False
        for other_call in self._telephony.calls:
            is_other_active = (
                other_call.gateway_path == call.gateway_path
                and other_call.path != call_path
                and other_call.state == CALL_STATE_ACTIVE
            )
            if is_other_active:
                has_active_call_on_gateway = True

        if has_active_call_on_gateway:
            self._run(self._telephony.hold_and_answer(call.gateway_path))
        else:
            self._run(self._telephony.answer(call_path))

    def _hangup_call(self, call_path: str) -> None:
        """End or reject `call_path`."""
        self._run(self._telephony.hangup(call_path))

    def _toggle_hold_for_call(self, call_path: str) -> None:
        """Hold or resume the call at `call_path` through its gateway."""
        call = self._telephony.call_for(call_path)
        if call is not None:
            self._toggle_hold_on_gateway(call.gateway_path)

    def _toggle_hold_on_gateway(self, gateway_path: str) -> None:
        """Swap active and held calls on `gateway_path`; with one call this toggles hold."""
        self._run(self._telephony.swap_calls(gateway_path))

    def _dial(self, gateway_path: str, number: str) -> None:
        """Place a call to `number` from the phone at `gateway_path`."""
        self._run(self._telephony.dial(gateway_path, number))

    def _send_tone(self, call_path: str, tone: str) -> None:
        """Send one DTMF tone on the gateway that carries `call_path`."""
        call = self._telephony.call_for(call_path)
        if call is not None:
            self._run(self._telephony.send_tones(call.gateway_path, tone))

    def _send_tone_on_gateway(self, gateway_path: str, tone: str) -> None:
        """Send one DTMF tone to the active call on `gateway_path`."""
        self._run(self._telephony.send_tones(gateway_path, tone))

    def _set_audio_on_computer(self, gateway_path: str, audio_on_computer: bool) -> None:
        """Route call audio to this computer or keep it on the phone."""
        self._run(self._telephony.set_audio_on_computer(gateway_path, audio_on_computer))

    def _set_speaker_volume(self, gateway_path: str, level: int) -> None:
        """Change the phone-side speaker gain."""
        self._run(self._telephony.set_speaker_volume(gateway_path, level))

    def _set_microphone_volume(self, gateway_path: str, level: int) -> None:
        """Change the phone-side microphone gain."""
        self._run(self._telephony.set_microphone_volume(gateway_path, level))

    def _run(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Schedule `coroutine` on the event loop and surface a failure as a notification."""
        event_loop = asyncio.get_event_loop()
        task = event_loop.create_task(coroutine)
        task.add_done_callback(self._report_task_outcome)

    def _report_task_outcome(self, task: asyncio.Task[Any]) -> None:
        """Log and show the error of a background action that failed."""
        if task.cancelled():
            return

        failure = task.exception()
        if failure is not None:
            logger.error("action failed: %s", failure)
            self._run(
                self._notifier.show_information("Action failed", str(failure), URGENCY_NORMAL)
            )


def _preview_text(text: str, limit: int) -> str:
    """Collapse `text` to one line of at most `limit` characters for a notification."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line

    cut_length = limit - len(ELLIPSIS)

    return single_line[:cut_length] + ELLIPSIS


def _format_duration(elapsed_seconds: int) -> str:
    """Format a duration as `MM:SS`, or `H:MM:SS` once it passes an hour."""
    hours, remainder_seconds = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder_seconds, 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"
