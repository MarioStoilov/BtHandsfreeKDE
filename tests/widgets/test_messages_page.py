"""Tests for the Messages tab."""

from datetime import datetime

from bt_handsfree_kde.messages.client import (
    SYNC_STATE_FAILED,
    SYNC_STATE_IDLE,
    SYNC_STATE_SYNCED,
    SYNC_STATE_SYNCING,
    MessagesState,
)
from bt_handsfree_kde.messages.conversations import group_conversations
from bt_handsfree_kde.messages.message import message_from_properties
from bt_handsfree_kde.ui.messages_page import (
    NO_MESSAGES_TEXT,
    NOT_SYNCED_TEXT,
    SYNCING_TEXT,
    MessagesPage,
)
from tests.conftest import PHONE_ADDRESS
from tests.unit.test_conversations import INBOUND, OTHER, SENT, SERVICE, _lookup_name
from tests.unit.test_message import BASE_PROPERTIES
from tests.widgets.conftest import GATEWAY_PATH

ALL_MESSAGES = (SERVICE, SENT, INBOUND, OTHER)
SYNCED_STATE = MessagesState(
    PHONE_ADDRESS, SYNC_STATE_SYNCED, ALL_MESSAGES, datetime(2026, 9, 20, 10, 5), ""
)


def test_status_line_follows_the_sync_state(qt_application) -> None:
    """Each state has its text; an empty synced state says there are no messages."""
    page = MessagesPage()

    page.update_phone("", None, [], "No phone connected")
    assert page._status_label.text() == "No phone connected"
    page.update_phone(
        GATEWAY_PATH, MessagesState(PHONE_ADDRESS, SYNC_STATE_IDLE, (), None, ""), [], ""
    )
    assert page._status_label.text() == NOT_SYNCED_TEXT
    page.update_phone(
        GATEWAY_PATH, MessagesState(PHONE_ADDRESS, SYNC_STATE_SYNCING, (), None, ""), [], ""
    )
    assert page._status_label.text() == SYNCING_TEXT
    assert not page._refresh_button.isEnabled()
    page.update_phone(
        GATEWAY_PATH, MessagesState(PHONE_ADDRESS, SYNC_STATE_FAILED, (), None, "Nope"), [], ""
    )
    assert page._status_label.text() == "Nope"
    page.update_phone(
        GATEWAY_PATH,
        MessagesState(PHONE_ADDRESS, SYNC_STATE_SYNCED, (), datetime.now(), ""),
        [],
        "",
    )
    assert page._status_label.text() == NO_MESSAGES_TEXT


def test_conversations_list_thread_and_selection(qt_application) -> None:
    """Rows show name and preview, bold while unread; selecting opens the thread once."""
    page = MessagesPage()
    opened: list[tuple[str, str]] = []
    page.conversation_opened.connect(lambda gateway_path, key: opened.append((gateway_path, key)))
    conversations = group_conversations(list(ALL_MESSAGES), _lookup_name)

    page.update_phone(GATEWAY_PATH, SYNCED_STATE, conversations, "")
    assert page._conversation_list.count() == 3
    assert page._conversation_list.item(0).text() == "Alice Doe\nYou: hi"
    assert page._conversation_list.item(0).font().bold()
    assert not page._conversation_list.item(1).font().bold()

    page._conversation_list.setCurrentRow(0)
    assert opened == [(GATEWAY_PATH, "0015550100")]
    assert "Alice Doe" in page._thread_header.text()
    assert "hi" in page._thread_view.toPlainText()

    page.update_phone(GATEWAY_PATH, SYNCED_STATE, conversations, "")
    assert opened == [(GATEWAY_PATH, "0015550100")]
    assert page._conversation_list.currentRow() == 0

    read_inbound = message_from_properties("/s/message1", dict(BASE_PROPERTIES, Read=True))
    read_conversations = group_conversations([SERVICE, SENT, read_inbound, OTHER], _lookup_name)
    page.update_phone(GATEWAY_PATH, SYNCED_STATE, read_conversations, "")
    assert not page._conversation_list.item(0).font().bold()
    assert page._conversation_list.currentRow() == 0

    page.show_conversation("mybank")
    assert opened[-1] == (GATEWAY_PATH, "mybank")
    assert "MYBANK" in page._thread_header.text()


def test_refresh_asks_for_a_new_listing(qt_application) -> None:
    """Refresh emits the gateway path."""
    page = MessagesPage()
    refreshes: list[str] = []
    page.refresh_requested.connect(refreshes.append)
    page.update_phone(GATEWAY_PATH, SYNCED_STATE, [], "")

    page._refresh_button.click()
    assert refreshes == [GATEWAY_PATH]
