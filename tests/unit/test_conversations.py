"""Tests for grouping messages into conversations."""

import pytest

from bt_handsfree_kde.messages.conversations import conversation_key, group_conversations
from bt_handsfree_kde.messages.message import TextMessage, message_from_properties
from tests.unit.test_message import BASE_PROPERTIES

INBOUND = message_from_properties("/s/message1", BASE_PROPERTIES)
SENT = message_from_properties(
    "/s/message3",
    dict(
        BASE_PROPERTIES,
        Folder="telecom/msg/sent",
        RecipientAddress="0015550100",
        Recipient="Alice",
        Timestamp="20260920T102000",
        Read=True,
    ),
)
SERVICE = message_from_properties(
    "/s/message6",
    dict(
        BASE_PROPERTIES,
        SenderAddress="MYBANK",
        Sender="MYBANK",
        Subject="code 1234",
        Timestamp="20260919T090000",
        Read=True,
    ),
)
OTHER = message_from_properties(
    "/s/message7",
    dict(
        BASE_PROPERTIES,
        SenderAddress="+15550177",
        Sender="+15550177",
        Subject="old",
        Timestamp="20260918T090000",
    ),
)


def _lookup_name(address: str) -> str:
    """Phonebook stand-in: only the Alice number has a contact."""
    return "Alice Doe" if "0100" in address else ""


def test_keys_are_digits_or_folded_text() -> None:
    """Numbers key by digits, alphanumeric senders by their folded text."""
    assert conversation_key("+1 (555) 0100") == "15550100"
    assert conversation_key("MyBank") == "mybank"


@pytest.mark.parametrize(
    "order",
    [
        [SERVICE, SENT, INBOUND, OTHER],
        [INBOUND, SENT, SERVICE, OTHER],
        [OTHER, INBOUND, SERVICE, SENT],
    ],
)
def test_forms_of_one_number_merge_regardless_of_order(order: list[TextMessage]) -> None:
    """National and international forms share a conversation keyed by the longer form."""
    conversations = group_conversations(order, _lookup_name)

    assert [conversation.name for conversation in conversations] == [
        "Alice Doe",
        "MYBANK",
        "+15550177",
    ]
    alice = conversations[0]
    assert alice.key == "0015550100"
    assert [message.path for message in alice.messages] == ["/s/message1", "/s/message3"]
    assert alice.unread_count == 1
    assert alice.unread_message_paths == ["/s/message1"]
    assert alice.latest.path == "/s/message3"


def test_names_fall_back_to_phone_name_then_address() -> None:
    """Without a contact, the phone's name is used unless it equals the address."""
    conversations = group_conversations([SERVICE, OTHER], lambda address: "")

    assert [conversation.name for conversation in conversations] == ["MYBANK", "+15550177"]
