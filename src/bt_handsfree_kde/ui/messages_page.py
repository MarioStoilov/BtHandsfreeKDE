"""Messages tab: the selected phone's conversations on the left, the open thread on the right."""

import html

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from bt_handsfree_kde.messages.client import (
    SYNC_STATE_FAILED,
    SYNC_STATE_SYNCED,
    SYNC_STATE_SYNCING,
    MessagesState,
)
from bt_handsfree_kde.messages.conversations import Conversation
from bt_handsfree_kde.messages.message import TextMessage

# Theme icon name of the Refresh button (present in Breeze).
REFRESH_ICON = "view-refresh"
# Pixel size of the button icon.
BUTTON_ICON_SIZE = 24
# Status texts for the sync states that have no phone-specific wording.
NOT_SYNCED_TEXT = "Messages have not been read from the phone yet. Press Refresh."
SYNCING_TEXT = "Reading messages from the phone… If the phone asks, allow message access."
NO_MESSAGES_TEXT = "No messages in the phone's inbox or sent folder."
# Shown in the thread pane while no conversation is selected.
SELECT_CONVERSATION_TEXT = "Select a conversation"
# Item data role under which each row stores its conversation key.
CONVERSATION_KEY_ROLE = Qt.ItemDataRole.UserRole
# Longest preview of the latest message shown in a conversation row, in characters.
ROW_PREVIEW_LIMIT = 60
# Marks a cut preview.
ELLIPSIS = "…"
# Relative widths of the conversation list and the thread pane.
CONVERSATION_LIST_STRETCH = 1
THREAD_PANE_STRETCH = 2
# Minimum height of the page, in rows of the list font, so a thread is readable.
MINIMUM_VISIBLE_ROWS = 14
# Layout of the time shown under each message and next to each conversation.
MESSAGE_TIME_LAYOUT = "%a %d %b, %H:%M"
# Widest share of the thread pane a message bubble takes.
BUBBLE_MAX_WIDTH_PERCENT = 80


class MessagesPage(QWidget):
    """Lists conversations and shows the selected one as a thread.

    The list is rebuilt only when the conversations change in a way the rows show, so
    the selection and the scroll position survive the periodic state refreshes.
    Selecting a conversation announces it, which lets the application mark it read and
    fetch the full text of long messages.
    """

    # Emitted with (gateway path, conversation key) when a conversation is shown.
    conversation_opened = Signal(str, str)
    # Emitted with the gateway path when the user presses Refresh.
    refresh_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the widgets; `update_phone` fills them."""
        super().__init__(parent)
        self._gateway_path = ""
        self._conversations: list[Conversation] = []
        # What the rows last showed, to skip rebuilding the list when nothing changed.
        self._row_signature: list[tuple[str, str, str, int]] = []
        # Path and read state of the messages the thread pane last rendered.
        self._thread_signature: tuple[str, ...] = ()
        self._selected_key = ""

        self._conversation_list = QListWidget(self)
        row_height = self._conversation_list.fontMetrics().height()
        self._conversation_list.setMinimumHeight(row_height * MINIMUM_VISIBLE_ROWS)

        self._thread_header = QLabel(SELECT_CONVERSATION_TEXT, self)
        header_font = QFont()
        header_font.setBold(True)
        self._thread_header.setFont(header_font)
        self._thread_view = QTextBrowser(self)
        self._thread_view.setOpenExternalLinks(False)

        thread_pane = QWidget(self)
        thread_column = QVBoxLayout(thread_pane)
        thread_column.setContentsMargins(0, 0, 0, 0)
        thread_column.addWidget(self._thread_header)
        thread_column.addWidget(self._thread_view)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._conversation_list)
        splitter.addWidget(thread_pane)
        splitter.setStretchFactor(0, CONVERSATION_LIST_STRETCH)
        splitter.setStretchFactor(1, THREAD_PANE_STRETCH)

        self._refresh_button = QPushButton(QIcon.fromTheme(REFRESH_ICON), "Refresh", self)
        self._refresh_button.setIconSize(QSize(BUTTON_ICON_SIZE, BUTTON_ICON_SIZE))
        self._refresh_button.setToolTip("Read the newest messages from the phone again")
        self._status_label = QLabel(self)
        self._status_label.setWordWrap(True)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._status_label, 1)
        bottom_row.addWidget(self._refresh_button)

        column = QVBoxLayout(self)
        column.addWidget(splitter, 1)
        column.addLayout(bottom_row)

        self._conversation_list.currentItemChanged.connect(self._on_current_row_changed)
        self._refresh_button.clicked.connect(self._emit_refresh)

    def focus_conversations(self) -> None:
        """Give keyboard focus to the conversation list."""
        self._conversation_list.setFocus()

    def show_conversation(self, conversation_key: str) -> None:
        """Select the conversation with `conversation_key` if it is listed."""
        for row_index in range(self._conversation_list.count()):
            row_item = self._conversation_list.item(row_index)
            if row_item.data(CONVERSATION_KEY_ROLE) == conversation_key:
                self._conversation_list.setCurrentItem(row_item)
                return

    def update_phone(
        self,
        gateway_path: str,
        messages_state: MessagesState | None,
        conversations: list[Conversation],
        unavailable_reason: str,
    ) -> None:
        """Show the conversations and sync state of the selected phone.

        Args:
            gateway_path: Object path of the selected phone; empty when there is none.
            messages_state: That phone's sync state, `None` when there is no phone.
            conversations: That phone's conversations, most recent first.
            unavailable_reason: Why nothing can be done right now (no service, no phone),
                empty otherwise; shown on the status line instead of the sync state.
        """
        phone_changed = gateway_path != self._gateway_path
        self._gateway_path = gateway_path
        self._conversations = list(conversations)

        row_signature = _row_signature(conversations)
        rows_changed = row_signature != self._row_signature or phone_changed
        if rows_changed:
            self._fill_list(conversations, phone_changed)
            self._row_signature = row_signature

        self._render_selected_thread()

        is_syncing = messages_state is not None and messages_state.sync_state == SYNC_STATE_SYNCING
        status_text = unavailable_reason or _status_text(messages_state, len(conversations))
        self._status_label.setText(status_text)
        can_refresh = bool(gateway_path) and not is_syncing
        self._refresh_button.setEnabled(can_refresh)

    def _fill_list(self, conversations: list[Conversation], phone_changed: bool) -> None:
        """Replace the rows, keeping the selected conversation unless the phone changed."""
        previously_selected_key = "" if phone_changed else self._selected_key

        self._conversation_list.blockSignals(True)
        self._conversation_list.clear()
        for conversation in conversations:
            row_item = QListWidgetItem(_row_text(conversation))
            row_item.setData(CONVERSATION_KEY_ROLE, conversation.key)
            row_item.setToolTip(conversation.address)
            has_unread = conversation.unread_count > 0
            row_font = row_item.font()
            row_font.setBold(has_unread)
            row_item.setFont(row_font)

            self._conversation_list.addItem(row_item)
        self._conversation_list.blockSignals(False)

        if previously_selected_key:
            self.show_conversation(previously_selected_key)
        still_selected = self._conversation_list.currentItem() is not None
        if not still_selected:
            self._selected_key = ""
            self._thread_signature = ()

    def _on_current_row_changed(
        self, current_item: QListWidgetItem | None, _previous_item: QListWidgetItem | None
    ) -> None:
        """Show the newly selected conversation and announce that it was opened."""
        if current_item is None:
            self._selected_key = ""
            self._render_selected_thread()
            return

        self._selected_key = str(current_item.data(CONVERSATION_KEY_ROLE))
        self._thread_signature = ()
        self._render_selected_thread()

        if self._gateway_path:
            self.conversation_opened.emit(self._gateway_path, self._selected_key)

    def _selected_conversation(self) -> Conversation | None:
        """Return the conversation of the selected row, or `None`."""
        for conversation in self._conversations:
            if conversation.key == self._selected_key:
                return conversation

        return None

    def _render_selected_thread(self) -> None:
        """Draw the selected conversation in the thread pane if its content changed."""
        conversation = self._selected_conversation()
        if conversation is None:
            self._thread_header.setText(SELECT_CONVERSATION_TEXT)
            self._thread_view.clear()
            self._thread_signature = ()
            return

        thread_signature = _thread_signature(conversation)
        if thread_signature == self._thread_signature:
            return
        self._thread_signature = thread_signature

        header_text = conversation.name
        if conversation.name != conversation.address:
            header_text = f"{conversation.name} · {conversation.address}"
        self._thread_header.setText(header_text)

        thread_html = _thread_html(conversation, self._thread_view)
        self._thread_view.setHtml(thread_html)
        scroll_bar = self._thread_view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _emit_refresh(self) -> None:
        """Ask the application to list the selected phone's messages again."""
        if self._gateway_path:
            self.refresh_requested.emit(self._gateway_path)


def _row_signature(conversations: list[Conversation]) -> list[tuple[str, str, str, int]]:
    """Describe what the rows show, so an unchanged list is not rebuilt."""
    signature: list[tuple[str, str, str, int]] = []

    for conversation in conversations:
        signature.append(
            (
                conversation.key,
                conversation.name,
                conversation.latest.path,
                conversation.unread_count,
            )
        )

    return signature


def _thread_signature(conversation: Conversation) -> tuple[str, ...]:
    """Describe what the thread pane shows for `conversation`."""
    signature_parts: list[str] = []

    for message in conversation.messages:
        signature_parts.append(f"{message.path}:{message.is_read}:{message.is_text_complete}")

    return tuple(signature_parts)


def _row_text(conversation: Conversation) -> str:
    """Return the two-line row text: name, then a preview of the latest message."""
    latest_message = conversation.latest
    preview = _one_line_preview(latest_message.text, ROW_PREVIEW_LIMIT)
    direction_mark = "" if latest_message.is_incoming else "You: "

    return f"{conversation.name}\n{direction_mark}{preview}"


def _one_line_preview(text: str, limit: int) -> str:
    """Collapse `text` to one line of at most `limit` characters."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line

    cut_length = limit - len(ELLIPSIS)

    return single_line[:cut_length] + ELLIPSIS


def _time_text(message: TextMessage) -> str:
    """Format a message's time for display; empty when the phone gave none."""
    if message.timestamp is None:
        return ""

    return message.timestamp.strftime(MESSAGE_TIME_LAYOUT)


def _thread_html(conversation: Conversation, view: QWidget) -> str:
    """Render the conversation as bubbles: incoming on the left, outgoing on the right."""
    palette = view.palette()
    incoming_background = palette.alternateBase().color().name()
    outgoing_background = palette.highlight().color().name()
    outgoing_foreground = palette.highlightedText().color().name()
    incoming_foreground = palette.text().color().name()
    faint_foreground = palette.placeholderText().color().name()

    bubble_rows: list[str] = []
    for message in conversation.messages:
        escaped_text = html.escape(message.text).replace("\n", "<br>")
        time_text = html.escape(_time_text(message))
        if message.is_incoming:
            alignment = "left"
            background = incoming_background
            foreground = incoming_foreground
        else:
            alignment = "right"
            background = outgoing_background
            foreground = outgoing_foreground
        unread_mark = "" if message.is_read or not message.is_incoming else " ●"

        bubble_rows.append(
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr><td align="{alignment}">'
            f'<table width="{BUBBLE_MAX_WIDTH_PERCENT}%" cellspacing="0" cellpadding="6" '
            f'bgcolor="{background}"><tr><td style="color:{foreground}">{escaped_text}</td></tr>'
            f'<tr><td align="right" style="color:{faint_foreground}"><small>{time_text}'
            f"{unread_mark}</small></td></tr></table></td></tr></table><br>"
        )

    return "".join(bubble_rows)


def _status_text(messages_state: MessagesState | None, conversation_count: int) -> str:
    """Describe the sync state for the status line."""
    if messages_state is None:
        return ""

    if messages_state.sync_state == SYNC_STATE_SYNCING:
        return SYNCING_TEXT

    if messages_state.sync_state == SYNC_STATE_FAILED:
        return messages_state.error_text

    is_synced = messages_state.sync_state == SYNC_STATE_SYNCED
    if is_synced and messages_state.synced_at is not None:
        if conversation_count == 0:
            return NO_MESSAGES_TEXT
        synced_time_text = messages_state.synced_at.strftime("%H:%M")
        return f"Newest messages read from the phone at {synced_time_text}"

    return NOT_SYNCED_TEXT
