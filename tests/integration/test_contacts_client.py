"""Tests for the contacts client against the fake obexd."""

from dbus_fast.aio import MessageBus

from bt_handsfree_kde.contacts.client import (
    CONNECTION_REFUSED_TEXT,
    OBEXD_MISSING_TEXT,
    PHONEBOOK_FORBIDDEN_TEXT,
    SYNC_STATE_FAILED,
    SYNC_STATE_SYNCED,
    ContactsClient,
)
from bt_handsfree_kde.dbus.obex import ObexClient, obex_transfer_directory
from tests.conftest import PHONE_ADDRESS, wait_until
from tests.fakes.obex_service import FakeObexService
from tests.unit.test_vcard import VCARD_30_TEXT


async def _started_contacts(client_bus: MessageBus) -> ContactsClient:
    """Start the obexd layer and a contacts client on it."""
    obex_client = ObexClient(client_bus)
    await obex_client.start()

    return ContactsClient(obex_client)


async def test_sync_pulls_parses_and_cleans_up(
    client_bus: MessageBus, obex: FakeObexService, monkeypatch, tmp_path
) -> None:
    """The phonebook arrives parsed, the file is deleted and the session removed."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    obex.phonebook_vcard = VCARD_30_TEXT
    contacts = await _started_contacts(client_bus)

    contacts.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: contacts.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_SYNCED, "sync"
    )

    state = contacts.state_for_address(PHONE_ADDRESS)
    assert state.phonebook.contact_count == 4
    assert contacts.lookup_name(PHONE_ADDRESS, "5550100") == "Alice Doe"
    assert obex.records.created_sessions == [(PHONE_ADDRESS, "pbap")]
    assert obex.records.selected_phonebooks == [("int", "pb")]
    assert obex.records.pull_filters[0]["Fields"] == ["N", "FN", "TEL"]
    assert len(obex.records.removed_sessions) == 1
    assert list(obex_transfer_directory().iterdir()) == []


async def test_refusal_and_forbidden_have_their_own_texts(
    client_bus: MessageBus, obex: FakeObexService
) -> None:
    """A refused connection and a forbidden phonebook explain what to do on the phone."""
    contacts = await _started_contacts(client_bus)

    obex.refuse_connections = True
    contacts.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: contacts.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_FAILED, "refusal"
    )
    assert contacts.state_for_address(PHONE_ADDRESS).error_text == CONNECTION_REFUSED_TEXT

    obex.refuse_connections = False
    obex.forbid_phonebook = True
    contacts.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: contacts.state_for_address(PHONE_ADDRESS).error_text == PHONEBOOK_FORBIDDEN_TEXT,
        "forbidden",
    )
    assert len(obex.records.removed_sessions) == 1


async def test_missing_obexd_is_explained(client_bus: MessageBus) -> None:
    """Without obexd on the bus the sync fails with the install hint."""
    contacts = await _started_contacts(client_bus)

    contacts.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: contacts.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_FAILED, "failure"
    )

    assert contacts.state_for_address(PHONE_ADDRESS).error_text == OBEXD_MISSING_TEXT


async def test_forget_drops_the_phonebook(client_bus: MessageBus, obex: FakeObexService) -> None:
    """After forget the state is idle again."""
    obex.phonebook_vcard = VCARD_30_TEXT
    contacts = await _started_contacts(client_bus)
    contacts.start_sync(PHONE_ADDRESS)
    await wait_until(
        lambda: contacts.state_for_address(PHONE_ADDRESS).sync_state == SYNC_STATE_SYNCED, "sync"
    )

    contacts.forget(PHONE_ADDRESS)

    assert contacts.state_for_address(PHONE_ADDRESS).phonebook is None
