"""Tests for the dependency checker against the fakes and an empty bus."""

from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.dependencies import (
    BLUEZ_KEY,
    BLUEZ_MISSING_DETAIL,
    KEY_TONES_KEY,
    NOTIFICATIONS_KEY,
    NOTIFICATIONS_NO_ACTIONS_DETAIL,
    OBEXD_KEY,
    OBEXD_MISSING_DETAIL,
    TELEPHONY_KEY,
    TELEPHONY_MISSING_DETAIL,
    TRAY_KEY,
    TRAY_MISSING_DETAIL,
    DependencyChecker,
    DependencyReport,
)
from tests.conftest import wait_until
from tests.fakes.bluez_service import FakeBlueZService
from tests.fakes.notification_service import FakeNotificationService
from tests.fakes.obex_service import FakeObexService
from tests.fakes.telephony_service import FakeTelephonyService
from tests.fakes.watcher_service import FakeWatcherService

# Items checked over a bus; key tones depend on the machine's libraries, not the bus.
BUS_KEYS = [TELEPHONY_KEY, BLUEZ_KEY, OBEXD_KEY, NOTIFICATIONS_KEY, TRAY_KEY]


def _met_keys(report: DependencyReport) -> list[str]:
    """Return the keys of the report's available items, bus items only."""
    met_keys: list[str] = []
    for status in report.statuses:
        if status.is_met and status.key in BUS_KEYS:
            met_keys.append(status.key)

    return met_keys


def _detail_of(report: DependencyReport, key: str) -> str:
    """Return the detail text of the item at `key`."""
    for status in report.statuses:
        if status.key == key:
            return status.detail

    raise AssertionError(f"no status for {key}")


async def test_everything_present_is_reported_met(
    client_bus: MessageBus,
    telephony: FakeTelephonyService,
    bluez: FakeBlueZService,
    obex: FakeObexService,
    notifications: FakeNotificationService,
    watcher: FakeWatcherService,
) -> None:
    """With every fake on the bus the five bus items are met and the order is fixed."""
    checker = DependencyChecker(client_bus)

    report = await checker.start()

    assert _met_keys(report) == BUS_KEYS
    assert [status.key for status in report.statuses] == BUS_KEYS + [KEY_TONES_KEY]
    checker.stop()


async def test_empty_bus_reports_each_missing_item_with_its_detail(
    client_bus: MessageBus,
) -> None:
    """Without any service every bus item is missing and says so in its detail."""
    checker = DependencyChecker(client_bus)

    report = await checker.start()

    assert _met_keys(report) == []
    assert _detail_of(report, TELEPHONY_KEY) == TELEPHONY_MISSING_DETAIL
    assert _detail_of(report, BLUEZ_KEY) == BLUEZ_MISSING_DETAIL
    assert _detail_of(report, OBEXD_KEY) == OBEXD_MISSING_DETAIL
    assert _detail_of(report, TRAY_KEY) == TRAY_MISSING_DETAIL
    assert not report.all_met
    assert "obexd" in report.missing_titles_text
    checker.stop()


async def test_notification_server_without_actions_is_not_enough(
    client_bus: MessageBus, connect_bus
) -> None:
    """A server that cannot show buttons leaves the notifications item missing."""
    server = FakeNotificationService(await connect_bus(), capabilities=["body"])
    await server.start()
    checker = DependencyChecker(client_bus)

    report = await checker.start()

    assert _detail_of(report, NOTIFICATIONS_KEY) == NOTIFICATIONS_NO_ACTIONS_DETAIL
    checker.stop()


async def test_name_appearing_and_leaving_triggers_a_new_report(
    client_bus: MessageBus, connect_bus
) -> None:
    """obexd starting after the check flips its item; stopping flips it back."""
    checker = DependencyChecker(client_bus)
    reports: list[DependencyReport] = []
    checker.report_changed.connect(reports.append)
    first_report = await checker.start()
    assert OBEXD_KEY not in _met_keys(first_report)

    late_obex = FakeObexService(await connect_bus())
    await late_obex.start()
    await wait_until(lambda: len(reports) == 1, "report after obexd appeared")
    assert _met_keys(reports[0]) == [OBEXD_KEY]

    await late_obex.stop()
    await wait_until(lambda: len(reports) == 2, "report after obexd left")
    assert _met_keys(reports[1]) == []
    assert checker.report == reports[1]
    checker.stop()
