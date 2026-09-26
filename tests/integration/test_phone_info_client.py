"""Tests for the BlueZ client against the fake BlueZ service."""

from bt_handsfree_kde.dbus.bluez import PhoneInfoClient
from tests.conftest import PHONE_ADDRESS, PHONE_ALIAS, wait_until
from tests.fakes.bluez_service import FakeBlueZService


async def test_devices_alias_and_battery_are_tracked(
    private_buses: str, bluez: FakeBlueZService
) -> None:
    """Existing devices load; alias, battery changes and removal arrive through signals."""
    device_path = bluez.add_device(PHONE_ADDRESS, PHONE_ALIAS, battery_percentage=80)
    client = PhoneInfoClient()
    changes = []
    client.phone_info_changed.connect(changes.append)

    await client.start()
    assert client.is_available
    phone_info = client.info_for_address(PHONE_ADDRESS.lower())
    assert (
        phone_info is not None
        and phone_info.alias == PHONE_ALIAS
        and phone_info.battery_percentage == 80
    )

    bluez.set_alias(device_path, "Renamed")
    await wait_until(
        lambda: client.info_for_address(PHONE_ADDRESS).alias == "Renamed", "alias change"
    )
    bluez.set_battery(device_path, 42)
    await wait_until(
        lambda: client.info_for_address(PHONE_ADDRESS).battery_percentage == 42, "battery change"
    )
    bluez.remove_battery(device_path)
    await wait_until(
        lambda: client.info_for_address(PHONE_ADDRESS).battery_percentage is None, "battery gone"
    )

    second_path = bluez.add_device("AA:BB:CC:DD:EE:02", "Other")
    await wait_until(
        lambda: client.info_for_address("AA:BB:CC:DD:EE:02") is not None, "device added"
    )
    bluez.remove_device(second_path)
    await wait_until(lambda: client.info_for_address("AA:BB:CC:DD:EE:02") is None, "device removed")
    assert changes[-1].connected is False


async def test_missing_bluez_leaves_the_client_unavailable(private_buses: str) -> None:
    """Without BlueZ the client reports nothing and does not raise."""
    client = PhoneInfoClient()

    await client.start()

    assert not client.is_available
    assert client.info_for_address(PHONE_ADDRESS) is None


async def test_bluez_restart_drops_and_reloads_the_devices(
    private_buses: str, bluez: FakeBlueZService
) -> None:
    """Losing the name reports every device disconnected; regaining it loads them again."""
    bluez.add_device(PHONE_ADDRESS, PHONE_ALIAS, battery_percentage=80)
    client = PhoneInfoClient()
    changes = []
    client.phone_info_changed.connect(changes.append)
    await client.start()

    await bluez.stop()
    await wait_until(lambda: not client.is_available, "BlueZ gone")
    assert client.info_for_address(PHONE_ADDRESS) is None
    assert changes[-1].connected is False and changes[-1].battery_percentage is None

    await bluez.start()
    await wait_until(lambda: client.is_available, "BlueZ back")
    phone_info = client.info_for_address(PHONE_ADDRESS)
    assert phone_info is not None and phone_info.battery_percentage == 80
    assert changes[-1].address == PHONE_ADDRESS and changes[-1].connected


async def test_bluez_appearing_later_is_picked_up(private_buses: str, connect_bus) -> None:
    """A client started before BlueZ loads the devices when the name appears."""
    client = PhoneInfoClient()
    await client.start()
    assert not client.is_available

    late_bluez = FakeBlueZService(await connect_bus())
    late_bluez.add_device(PHONE_ADDRESS, PHONE_ALIAS)
    await late_bluez.start()

    await wait_until(lambda: client.is_available, "BlueZ appeared")
    assert client.info_for_address(PHONE_ADDRESS).alias == PHONE_ALIAS
