"""Tests for the tray: registration with the watcher and the served menu."""

from dbus_fast import Message
from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.dbusmenu import DBUSMENU_INTERFACE, MENU_OBJECT_PATH
from bt_handsfree_kde.dbus.telephony import AudioGateway
from bt_handsfree_kde.icons import application_icon
from bt_handsfree_kde.ui.tray import HandsfreeTray
from tests.conftest import PHONE_ADDRESS, wait_until
from tests.fakes.watcher_service import FakeWatcherService

GATEWAY = AudioGateway("/org/pipewire/Telephony/ag1", PHONE_ADDRESS, 15, 15, "idle", 0, False)


async def _menu_labels(bus: MessageBus, owner_name: str) -> list[str]:
    """Fetch the tray menu layout over the bus and return its top-level labels."""
    reply = await bus.call(
        Message(
            destination=owner_name,
            path=MENU_OBJECT_PATH,
            interface=DBUSMENU_INTERFACE,
            member="GetLayout",
            signature="iias",
            body=[0, -1, []],
        )
    )
    labels: list[str] = []
    for child in reply.body[1][2]:
        label = child.value[1].get("label")
        if label is not None:
            labels.append(label.value)

    return labels


async def test_tray_registers_and_serves_the_menu(
    qt_application, client_bus: MessageBus, connect_bus, watcher: FakeWatcherService
) -> None:
    """The item registers, the menu lists the phone and the entries, and clicks dispatch."""
    tray = HandsfreeTray(client_bus, application_icon())
    quits: list[bool] = []
    tray.quit_requested.connect(lambda: quits.append(True))
    await tray.start()
    assert watcher.registered_services == [client_bus.unique_name]

    tray.update_state(True, [GATEWAY], [], {}, {}, 3)
    viewer = await connect_bus()
    labels = await _menu_labels(viewer, client_bus.unique_name)
    assert labels[0] == PHONE_ADDRESS
    assert (
        "Messages (3 unread)" in labels
        and "Dialpad" in labels
        and "Contacts" in labels
        and "Quit" in labels
    )

    quit_id = None
    reply = await viewer.call(
        Message(
            destination=client_bus.unique_name,
            path=MENU_OBJECT_PATH,
            interface=DBUSMENU_INTERFACE,
            member="GetLayout",
            signature="iias",
            body=[0, -1, []],
        )
    )
    for child in reply.body[1][2]:
        if child.value[1].get("label") is not None and child.value[1]["label"].value == "Quit":
            quit_id = child.value[0]
    from dbus_fast import Variant

    await viewer.call(
        Message(
            destination=client_bus.unique_name,
            path=MENU_OBJECT_PATH,
            interface=DBUSMENU_INTERFACE,
            member="Event",
            signature="isvu",
            body=[quit_id, "clicked", Variant("s", ""), 0],
        )
    )
    await wait_until(lambda: quits == [True], "quit click")


async def test_tray_re_registers_when_the_watcher_returns(
    qt_application, client_bus: MessageBus, watcher: FakeWatcherService
) -> None:
    """A watcher restart triggers a new registration."""
    tray = HandsfreeTray(client_bus, application_icon())
    await tray.start()

    await watcher.stop()
    await watcher.start()

    await wait_until(lambda: len(watcher.registered_services) == 2, "re-registration")
