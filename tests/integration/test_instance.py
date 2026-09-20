"""Tests for the single-instance hand-off."""

from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.instance import SingleInstance
from tests.conftest import wait_until


async def test_second_claim_fails_and_hands_the_number_over(
    client_bus: MessageBus, connect_bus
) -> None:
    """The first process owns the name; the second reaches its ShowDialpad."""
    first = SingleInstance(client_bus)
    requested: list[str] = []
    first.dialpad_requested.connect(requested.append)
    assert await first.claim()

    second = SingleInstance(await connect_bus())
    assert not await second.claim()

    await second.show_dialpad_in_running_instance("+15550100")
    await wait_until(lambda: requested == ["+15550100"], "hand-off")
