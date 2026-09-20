"""Tests for building messages from obexd properties."""

from datetime import datetime

from bt_handsfree_kde.messages.message import (
    handle_from_path,
    message_from_properties,
    parse_timestamp,
)

BASE_PROPERTIES = {
    "Folder": "/telecom/msg/inbox",
    "Subject": "hi",
    "Timestamp": "20260920T101500",
    "Sender": "Alice",
    "SenderAddress": "+15550100",
    "Recipient": "",
    "RecipientAddress": "",
    "Type": "sms-gsm",
    "Text": True,
    "Read": False,
}


def test_timestamps_parse_with_and_without_offset() -> None:
    """Both MAP timestamp layouts parse; junk and empty text yield `None`."""
    assert parse_timestamp("20260920T101500") == datetime(2026, 9, 20, 10, 15)
    assert parse_timestamp("20260920T101500+0200") is not None
    assert parse_timestamp("") is None
    assert parse_timestamp("junk") is None


def test_handle_is_the_path_tail() -> None:
    """The phone's handle follows the `message` marker."""
    assert handle_from_path("/org/bluez/obex/client/session6/message18014398509483976") == (
        "18014398509483976"
    )


def test_incoming_message_uses_sender_and_preview() -> None:
    """Inbox messages take the sender as counterpart; a short subject is the whole text."""
    message = message_from_properties("/s/message1", BASE_PROPERTIES)

    assert message is not None
    assert message.is_incoming
    assert message.counterpart_address == "+15550100"
    assert message.text == "hi"
    assert message.is_text_complete
    assert message.handle == "1"


def test_long_subject_is_marked_incomplete() -> None:
    """A subject at the listing limit may be cut, so the full text must be fetched."""
    message = message_from_properties("/s/message2", dict(BASE_PROPERTIES, Subject="x" * 255))

    assert message is not None
    assert not message.is_text_complete


def test_sent_message_uses_recipient() -> None:
    """Sent-folder messages are outgoing and take the recipient as counterpart."""
    properties = dict(BASE_PROPERTIES, Folder="telecom/msg/sent", RecipientAddress="0015550100")
    message = message_from_properties("/s/message3", properties)

    assert message is not None
    assert not message.is_incoming
    assert message.counterpart_address == "0015550100"


def test_other_folders_and_textless_mms() -> None:
    """Drafts are skipped; a multimedia message without text gets a placeholder."""
    assert (
        message_from_properties("/s/message4", dict(BASE_PROPERTIES, Folder="telecom/msg/draft"))
        is None
    )

    mms = message_from_properties(
        "/s/message5", dict(BASE_PROPERTIES, Type="mms", Text=False, Subject="")
    )
    assert mms is not None
    assert mms.text == "(picture message)"
