"""Tests for the telephony client against the fake PipeWire service."""

import pytest
from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.telephony import (
    CALL_STATE_ACTIVE,
    CALL_STATE_INCOMING,
    Call,
    TelephonyClient,
    TelephonyError,
)
from tests.conftest import PHONE_ADDRESS, wait_until
from tests.fakes.telephony_service import FakeTelephonyService


async def _started_client(client_bus: MessageBus) -> tuple[TelephonyClient, dict]:
    """Start a client and collect its signals into a dictionary of lists."""
    client = TelephonyClient(client_bus)
    events: dict[str, list] = {"availability": [], "added": [], "changed": [], "removed": []}
    client.availability_changed.connect(events["availability"].append)
    client.call_added.connect(events["added"].append)
    client.call_changed.connect(events["changed"].append)
    client.call_removed.connect(events["removed"].append)
    await client.start()

    return client, events


async def test_discovers_gateways_and_follows_calls(
    client_bus: MessageBus, telephony: FakeTelephonyService
) -> None:
    """Existing gateways are found; calls added, changed and removed reach the signals."""
    gateway_path = telephony.add_gateway(PHONE_ADDRESS)
    client, events = await _started_client(client_bus)

    assert client.is_available
    assert [gateway.address for gateway in client.gateways] == [PHONE_ADDRESS]

    call_path = telephony.add_call(gateway_path, CALL_STATE_INCOMING, "+15550100")
    await wait_until(lambda: len(events["added"]) == 1, "call added")
    added_call: Call = events["added"][0]
    assert (
        added_call.path == call_path
        and added_call.is_incoming
        and added_call.gateway_path == gateway_path
    )

    telephony.set_call_state(call_path, CALL_STATE_ACTIVE)
    await wait_until(lambda: len(events["changed"]) == 1, "call changed")
    assert events["changed"][0].state == CALL_STATE_ACTIVE

    telephony.remove_call(call_path)
    await wait_until(lambda: events["removed"] == [call_path], "call removed")
    assert client.calls == []


async def test_actions_reach_the_service(
    client_bus: MessageBus, telephony: FakeTelephonyService
) -> None:
    """Answer, hang up, dial and tones are forwarded to the right objects."""
    gateway_path = telephony.add_gateway(PHONE_ADDRESS)
    client, events = await _started_client(client_bus)
    call_path = telephony.add_call(gateway_path, CALL_STATE_INCOMING, "+15550100")
    await wait_until(lambda: len(events["added"]) == 1, "call added")

    await client.answer(call_path)
    await client.hangup(call_path)
    await client.dial(gateway_path, "+15550100")
    await client.send_tones(gateway_path, "5")

    assert telephony.requests_named("Answer") == [(call_path, "Answer", ())]
    assert telephony.requests_named("Hangup") == [(call_path, "Hangup", ())]
    assert telephony.requests_named("Dial") == [(gateway_path, "Dial", ("+15550100",))]
    assert telephony.requests_named("SendTones") == [(gateway_path, "SendTones", ("5",))]

    with pytest.raises(TelephonyError):
        await client.answer(f"{gateway_path}/call99")


async def test_properties_are_read_back_after_set(
    client_bus: MessageBus, telephony: FakeTelephonyService
) -> None:
    """Volumes update through the announced change; the silent RejectSCO through read-back."""
    gateway_path = telephony.add_gateway(PHONE_ADDRESS)
    client, _ = await _started_client(client_bus)

    await client.set_speaker_volume(gateway_path, 20)
    await client.set_audio_on_computer(gateway_path, False)

    await wait_until(
        lambda: client.gateway_for(gateway_path).speaker_volume == 15, "clamped volume"
    )
    assert client.gateway_for(gateway_path).reject_sco is True
    assert telephony.reject_sco_of(gateway_path) is True


async def test_service_leaving_and_returning(
    client_bus: MessageBus, telephony: FakeTelephonyService
) -> None:
    """Losing the name drops everything; regaining it re-synchronises."""
    gateway_path = telephony.add_gateway(PHONE_ADDRESS)
    client, events = await _started_client(client_bus)
    telephony.add_call(gateway_path, CALL_STATE_ACTIVE, "+15550100")
    await wait_until(lambda: len(events["added"]) == 1, "call added")

    # The client announced availability once when it started; the two transitions
    # under test follow that entry.
    await telephony.stop()
    await wait_until(lambda: events["availability"] == [True, False], "service gone")
    assert client.gateways == [] and len(events["removed"]) == 1

    await telephony.start()
    await wait_until(lambda: events["availability"] == [True, False, True], "service back")
    assert [gateway.path for gateway in client.gateways] == [gateway_path]
    assert len(events["added"]) == 2
