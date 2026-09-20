"""Grouping of messages into conversations with the other party."""

from collections.abc import Callable
from dataclasses import dataclass

from bt_handsfree_kde.messages.message import TextMessage
from bt_handsfree_kde.phone_numbers import digits_of, same_number

# Name shown for a conversation whose other party has no address at all.
UNKNOWN_PARTY_NAME = "Unknown sender"


@dataclass(frozen=True)
class Conversation:
    """All messages exchanged with one other party, oldest first."""

    # Identifier stable across refreshes: the party's digits, or the address folded.
    key: str
    # The party's address as the phone last sent it.
    address: str
    # Contact name, else the name the phone attached, else the address.
    name: str
    messages: tuple[TextMessage, ...]

    @property
    def latest(self) -> TextMessage:
        """Return the most recent message."""
        return self.messages[-1]

    @property
    def unread_count(self) -> int:
        """Return how many incoming messages are unread."""
        unread_count = 0

        for message in self.messages:
            is_unread_incoming = message.is_incoming and not message.is_read
            if is_unread_incoming:
                unread_count += 1

        return unread_count

    @property
    def unread_message_paths(self) -> list[str]:
        """Return the object paths of the unread incoming messages."""
        unread_paths: list[str] = []

        for message in self.messages:
            is_unread_incoming = message.is_incoming and not message.is_read
            if is_unread_incoming:
                unread_paths.append(message.path)

        return unread_paths

    @property
    def incomplete_message_paths(self) -> list[str]:
        """Return the object paths of messages whose full text has not been fetched."""
        incomplete_paths: list[str] = []

        for message in self.messages:
            if not message.is_text_complete:
                incomplete_paths.append(message.path)

        return incomplete_paths


def conversation_key(address: str) -> str:
    """Return the grouping key for a party address: its digits, else the folded text."""
    digits = digits_of(address)
    if digits:
        return digits

    return address.strip().casefold()


def group_conversations(
    messages: list[TextMessage], lookup_name: Callable[[str], str]
) -> list[Conversation]:
    """Group `messages` by the other party, most recently active conversation first.

    A number in national form and the same number in international form land in one
    conversation (see `same_number`). Within a conversation messages are oldest first.

    Args:
        messages: Messages of one phone, any order.
        lookup_name: Returns the contact name for an address, or an empty string.
    """
    messages_by_key: dict[str, list[TextMessage]] = {}
    key_aliases: dict[str, str] = {}

    for message in messages:
        raw_key = conversation_key(message.counterpart_address)
        merged_key = key_aliases.get(raw_key)
        if merged_key is None:
            merged_key = _matching_numeric_key(raw_key, messages_by_key)
            key_aliases[raw_key] = merged_key

        # Of two forms of one number the longer (international) one names the
        # conversation, whichever form the phone listed first, so keys are stable.
        is_longer_form = merged_key != raw_key and len(raw_key) > len(merged_key)
        if is_longer_form:
            messages_by_key[raw_key] = messages_by_key.pop(merged_key)
            for aliased_key, target_key in key_aliases.items():
                if target_key == merged_key:
                    key_aliases[aliased_key] = raw_key
            merged_key = raw_key

        messages_by_key.setdefault(merged_key, []).append(message)

    conversations: list[Conversation] = []
    for key, grouped_messages in messages_by_key.items():
        grouped_messages.sort(key=_message_sort_key)
        latest_message = grouped_messages[-1]
        name = _conversation_name(grouped_messages, lookup_name)

        conversations.append(
            Conversation(key, latest_message.counterpart_address, name, tuple(grouped_messages))
        )

    conversations.sort(key=_conversation_recency_key, reverse=True)

    return conversations


def _matching_numeric_key(raw_key: str, messages_by_key: dict[str, list[TextMessage]]) -> str:
    """Return an existing key that denotes the same number as `raw_key`, else `raw_key`."""
    is_numeric = raw_key.isdigit()
    if not is_numeric:
        return raw_key

    for known_key in messages_by_key:
        is_same = known_key.isdigit() and same_number(raw_key, known_key)
        if is_same:
            return known_key

    return raw_key


def _conversation_name(
    grouped_messages: list[TextMessage], lookup_name: Callable[[str], str]
) -> str:
    """Pick the display name: contact name, then the phone's name, then the address."""
    latest_message = grouped_messages[-1]
    address = latest_message.counterpart_address

    contact_name = lookup_name(address) if address else ""
    if contact_name:
        return contact_name

    for message in reversed(grouped_messages):
        phone_name = message.counterpart_name.strip()
        is_real_name = bool(phone_name) and phone_name != message.counterpart_address
        if is_real_name:
            return phone_name

    if address:
        return address

    return UNKNOWN_PARTY_NAME


def _message_sort_key(message: TextMessage) -> tuple:
    """Sort key ordering messages oldest first."""
    return message.sort_key


def _conversation_recency_key(conversation: Conversation) -> tuple:
    """Sort key ordering conversations by their latest message."""
    return conversation.latest.sort_key
