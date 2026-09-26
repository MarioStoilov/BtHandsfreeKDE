"""Tests for the messages client against the fake obexd."""

from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.obex import ObexClient
from bt_handsfree_kde.messages.client import (
    CONNECTION_LOST_TEXT,
    CONNECTION_REFUSED_TEXT,
    SYNC_STATE_FAILED,
    SYNC_STATE_SYNCED,
    MessagesClient,
)
from tests.conftest import PHONE_ADDRESS, wait_until
from tests.fakes.obex_service import FakeMessage, FakeObexService
from tests.unit.test_bmessage import INCOMING_BMESSAGE

INBOX_MESSAGE = FakeMessage(
    "101",
    {
        "Folder": "telecom/msg/inbox",
        "Subject": "hi",
        "Timestamp": "20260920T101500",
        "Sender": "Alice",
        "SenderAddress": "+15550100",
        "Recipient": "",
        "RecipientAddress": "",
        "Type": "sms-gsm",
        "Size": 2,
        "Text": True,
        "Status": "complete",
        "Priority": False,
        "Read": False,
        "Sent": False,
        "Protected": False,
    },
    INCOMING_BMESSAGE,
)
LONG_MESSAGE = FakeMessage(
    "102", dict(INBOX_MESSAGE.properties, Subject="x" * 255), INCOMING_BMESSAGE
)
SENT_MESSAGE = FakeMessage(
    "201",
    dict(
        INBOX_MESSAGE.properties,
        Folder="telecom/msg/sent",
        RecipientAddress="+15550100",
        Recipient="Alice",
        Read=True,
        Sent=True,
    ),
    "",
)
PUSHED_MESSAGE = FakeMessage(
    "303",
    {"Folder": "telecom/msg/inbox", "Type": "sms-gsm", "Subject": "", "Read": False},
    INCOMING_BMESSAGE,
)


async def _started_messages(client_bus: MessageBus) -> MessagesClient:
    """Start the obexd layer and a messages client on it."""
    obex_client = ObexClient(client_bus)
    await obex_client.start()

    return MessagesClient(obex_client)


async def _synced_client(client_bus: MessageBus, obex: FakeObexService) -> MessagesClient:
    """A client that has listed the fake folders."""
    obex.messages_by_folder = {"inbox": [INBOX_MESSAGE, LONG_MESSAGE], "sent": [SENT_MESSAGE]}
    messages = await _started_messages(client_bus)
    messages.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: messages.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_SYNCED, "sync"
    )

    return messages


async def test_sync_lists_both_folders_and_keeps_the_session(
    client_bus: MessageBus, obex: FakeObexService
) -> None:
    """Inbox and sent are listed with the count limit; the MAP session stays open."""
    messages = await _synced_client(client_bus, obex)

    state = messages.state_for_address(PHONE_ADDRESS)
    assert sorted(message.handle for message in state.messages) == ["101", "102", "201"]
    assert obex.records.set_folders == ["telecom", "msg"]
    assert [folder for folder, _ in obex.records.listed_folders] == ["inbox", "sent"]
    assert obex.records.listed_folders[0][1]["MaxCount"] == 25
    assert len(obex.open_map_session_paths) == 1
    assert obex.records.removed_sessions == []


async def test_pushed_message_is_fetched_and_announced(
    client_bus: MessageBus, obex: FakeObexService, monkeypatch, tmp_path
) -> None:
    """A message object appearing outside a listing is completed from its bMessage."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    messages = await _synced_client(client_bus, obex)
    received = []
    messages.message_received.connect(lambda address, message: received.append((address, message)))

    obex.push_message(PUSHED_MESSAGE)
    await wait_until(lambda: len(received) == 1, "pushed message")

    address, message = received[0]
    assert address == PHONE_ADDRESS
    assert message.counterpart_address == "+15550100"
    assert message.text == "Hello there:\nsecond line"
    assert message.is_incoming and not message.is_read
    assert len(obex.records.fetched_message_paths) == 1


async def test_mark_read_and_full_text(
    client_bus: MessageBus, obex: FakeObexService, monkeypatch, tmp_path
) -> None:
    """Marking read writes the property; fetching completes the cut text."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    messages = await _synced_client(client_bus, obex)
    state = messages.state_for_address(PHONE_ADDRESS)
    long_message = next(message for message in state.messages if message.handle == "102")
    short_message = next(message for message in state.messages if message.handle == "101")
    assert not long_message.is_text_complete

    await messages.mark_read(PHONE_ADDRESS, [short_message.path, long_message.path])
    await messages.fetch_full_text(PHONE_ADDRESS, long_message.path)

    updated = messages.state_for_address(PHONE_ADDRESS)
    assert all(message.is_read for message in updated.messages if message.is_incoming)
    assert [flag for _, flag in obex.records.read_flags_set] == [True, True]
    fetched_long = next(message for message in updated.messages if message.handle == "102")
    assert fetched_long.is_text_complete and fetched_long.text == "Hello there:\nsecond line"


async def test_lost_session_and_forget(client_bus: MessageBus, obex: FakeObexService) -> None:
    """A dropped session is reported; forget closes the session and clears the state."""
    messages = await _synced_client(client_bus, obex)
    session_path = obex.open_map_session_paths[0]

    obex.drop_session(session_path)
    await wait_until(
        lambda: messages.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_FAILED, "lost"
    )
    assert messages.state_for_address(PHONE_ADDRESS).error_text == CONNECTION_LOST_TEXT

    messages.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: messages.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_SYNCED, "resync"
    )
    assert len(obex.open_map_session_paths) == 1

    messages.forget(PHONE_ADDRESS)
    await wait_until(lambda: obex.open_map_session_paths == [], "session closed")
    assert messages.state_for_address(PHONE_ADDRESS).messages == ()


async def test_obexd_restart_loses_the_session_and_refresh_reopens_it(
    client_bus: MessageBus, obex: FakeObexService
) -> None:
    """obexd leaving the bus is reported as a lost connection; the next sync reconnects."""
    messages = await _synced_client(client_bus, obex)

    await obex.stop()
    await wait_until(
        lambda: messages.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_FAILED, "lost"
    )
    assert messages.state_for_address(PHONE_ADDRESS).error_text == CONNECTION_LOST_TEXT
    assert len(messages.state_for_address(PHONE_ADDRESS).messages) == 3

    await obex.start()
    messages.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: messages.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_SYNCED, "resync"
    )
    assert len(obex.records.created_sessions) == 2
    assert len(obex.open_map_session_paths) == 1


async def test_refused_connection_is_explained(
    client_bus: MessageBus, obex: FakeObexService
) -> None:
    """A phone refusing the MAP connection yields the permission hint."""
    obex.refuse_connections = True
    messages = await _started_messages(client_bus)

    messages.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: messages.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_FAILED, "refusal"
    )

    assert messages.state_for_address(PHONE_ADDRESS).error_text == CONNECTION_REFUSED_TEXT
