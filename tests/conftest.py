"""Shared fixtures: a private message bus, connected fakes and an offscreen Qt application.

The private `dbus-daemon` stands in for both the session and the system bus; the clients
find it through the standard environment variables, so they run unchanged.
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
import pytest_asyncio
from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from tests.fakes.bluez_service import FakeBlueZService
from tests.fakes.notification_service import FakeNotificationService
from tests.fakes.obex_service import FakeObexService
from tests.fakes.telephony_service import FakeTelephonyService
from tests.fakes.watcher_service import FakeWatcherService

# Qt platform plugin that renders nowhere; set before Qt is imported anywhere.
OFFSCREEN_PLATFORM = "offscreen"
# Longest wait for an asynchronous expectation, in seconds.
WAIT_TIMEOUT_S = 3.0
# Poll interval while waiting for an expectation, in seconds.
WAIT_POLL_S = 0.01
# Bluetooth address of the fake phone used across the suite (fictional).
PHONE_ADDRESS = "AA:BB:CC:DD:EE:01"
# Name BlueZ reports for the fake phone.
PHONE_ALIAS = "Test phone"

# Configuration of the private daemon: a session-type bus on a private socket with no
# service directories, so nothing can be activated, and an open default policy.
DAEMON_CONFIG_TEMPLATE = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir={socket_directory}</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

os.environ.setdefault("QT_QPA_PLATFORM", OFFSCREEN_PLATFORM)


@pytest.fixture(scope="session")
def bus_address(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start one private `dbus-daemon` for the whole run and yield its address.

    The daemon gets its own configuration without service directories, so it can never
    activate the machine's real obexd or any other installed service.
    """
    config_directory = tmp_path_factory.mktemp("dbus")
    config_file = config_directory / "session.conf"
    socket_directory = config_directory / "socket"
    socket_directory.mkdir()
    config_file.write_text(DAEMON_CONFIG_TEMPLATE.format(socket_directory=socket_directory))

    daemon = subprocess.Popen(
        [
            "dbus-daemon",
            f"--config-file={config_file}",
            "--nofork",
            "--nopidfile",
            "--print-address=1",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    address = daemon.stdout.readline().strip()

    yield address

    daemon.terminate()
    daemon.wait()


@pytest.fixture
def private_buses(bus_address: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point both bus types at the private daemon for the duration of one test."""
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus_address)
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", bus_address)

    return bus_address


@pytest_asyncio.fixture
async def connect_bus(
    private_buses: str,
) -> AsyncIterator[Callable[[], "asyncio.Future[MessageBus]"]]:
    """Yield a factory for connections to the private bus; all are closed afterwards."""
    connections: list[MessageBus] = []

    async def connect() -> MessageBus:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        connections.append(bus)
        return bus

    yield connect

    for bus in connections:
        bus.disconnect()
    await asyncio.sleep(0)


@pytest_asyncio.fixture
async def client_bus(connect_bus: Callable) -> MessageBus:
    """A connection for the client under test."""
    return await connect_bus()


@pytest_asyncio.fixture
async def telephony(connect_bus: Callable) -> FakeTelephonyService:
    """A started fake telephony service."""
    service = FakeTelephonyService(await connect_bus())
    await service.start()

    return service


@pytest_asyncio.fixture
async def bluez(connect_bus: Callable) -> FakeBlueZService:
    """A started fake BlueZ service."""
    service = FakeBlueZService(await connect_bus())
    await service.start()

    return service


@pytest_asyncio.fixture
async def notifications(connect_bus: Callable) -> FakeNotificationService:
    """A started fake notification server with every capability."""
    service = FakeNotificationService(await connect_bus())
    await service.start()

    return service


@pytest_asyncio.fixture
async def watcher(connect_bus: Callable) -> FakeWatcherService:
    """A started fake StatusNotifierWatcher."""
    service = FakeWatcherService(await connect_bus())
    await service.start()

    return service


@pytest_asyncio.fixture
async def obex(connect_bus: Callable) -> FakeObexService:
    """A started fake obexd."""
    service = FakeObexService(await connect_bus())
    await service.start()

    return service


@pytest.fixture(scope="session")
def qt_application():
    """One offscreen Qt application for the whole run."""
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])

    return application


async def wait_until(condition: Callable[[], bool], description: str) -> None:
    """Poll `condition` until it holds or `WAIT_TIMEOUT_S` passes.

    Args:
        condition: Returns true once the expectation is met.
        description: Named in the failure message.

    Raises:
        AssertionError: the condition did not hold in time.
    """
    event_loop = asyncio.get_event_loop()
    deadline = event_loop.time() + WAIT_TIMEOUT_S

    while not condition():
        if event_loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {description}")
        await asyncio.sleep(WAIT_POLL_S)
