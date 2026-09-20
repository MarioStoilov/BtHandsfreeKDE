"""Tests for the dbusmenu server, driven over the bus like a Plasma host would."""

from typing import Any

from dbus_fast import Message, Variant
from dbus_fast.aio import MessageBus

from bt_handsfree_kde.dbus.dbusmenu import (
    DBUSMENU_INTERFACE,
    MENU_OBJECT_PATH,
    ROOT_ITEM_ID,
    DBusMenuService,
    MenuItem,
)

# Event name a host sends for a click.
CLICKED_EVENT = "clicked"


async def _get_layout(viewer: MessageBus, owner_name: str) -> tuple[int, list[Any]]:
    """Call `GetLayout` for the whole tree and return (revision, layout)."""
    reply = await viewer.call(
        Message(
            destination=owner_name,
            path=MENU_OBJECT_PATH,
            interface=DBUSMENU_INTERFACE,
            member="GetLayout",
            signature="iias",
            body=[ROOT_ITEM_ID, -1, []],
        )
    )

    return reply.body[0], reply.body[1]


def _children(layout: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    """Return (identifier, properties) of a layout's direct children."""
    children: list[tuple[int, dict[str, Any]]] = []
    for child in layout[2]:
        children.append((child.value[0], child.value[1]))

    return children


def _labels(layout: list[Any]) -> list[str]:
    """Return the labels of a layout's direct children, `-` for separators."""
    labels: list[str] = []
    for _, properties in _children(layout):
        label = properties.get("label")
        labels.append(label.value if label is not None else "-")

    return labels


async def _click(viewer: MessageBus, owner_name: str, item_id: int) -> None:
    """Send a click event for `item_id`."""
    await viewer.call(
        Message(
            destination=owner_name,
            path=MENU_OBJECT_PATH,
            interface=DBUSMENU_INTERFACE,
            member="Event",
            signature="isvu",
            body=[item_id, CLICKED_EVENT, Variant("s", ""), 0],
        )
    )


async def test_layout_lists_items_and_dispatches_clicks(
    client_bus: MessageBus, connect_bus
) -> None:
    """Items get identifiers, appear in the layout with their properties, and clicks dispatch."""
    clicks: list[str] = []
    service = DBusMenuService()
    client_bus.export(MENU_OBJECT_PATH, service)
    service.set_items(
        [
            MenuItem("First", on_activated=lambda: clicks.append("first")),
            MenuItem.separator(),
            MenuItem(
                "Second", checkable=True, checked=True, on_activated=lambda: clicks.append("second")
            ),
        ]
    )
    viewer = await connect_bus()

    revision, layout = await _get_layout(viewer, client_bus.unique_name)
    assert revision == 1
    assert _labels(layout) == ["First", "-", "Second"]
    second_id, second_properties = _children(layout)[2]
    assert second_properties["toggle-state"].value == 1

    await _click(viewer, client_bus.unique_name, second_id)
    assert clicks == ["second"]


async def test_same_shape_updates_in_place_without_new_revision(
    client_bus: MessageBus, connect_bus
) -> None:
    """When only a label changes, identifiers and the revision stay; the label updates."""
    service = DBusMenuService()
    client_bus.export(MENU_OBJECT_PATH, service)
    viewer = await connect_bus()
    service.set_items([MenuItem("In call · 00:01", key="call")])
    _, first_layout = await _get_layout(viewer, client_bus.unique_name)
    first_id, _ = _children(first_layout)[0]

    service.set_items([MenuItem("In call · 00:02", key="call")])

    revision, second_layout = await _get_layout(viewer, client_bus.unique_name)
    assert revision == 1
    assert _children(second_layout)[0][0] == first_id
    assert _labels(second_layout) == ["In call · 00:02"]


async def test_new_shape_gets_fresh_identifiers(client_bus: MessageBus, connect_bus) -> None:
    """A changed tree bumps the revision and never reuses an identifier."""
    service = DBusMenuService()
    client_bus.export(MENU_OBJECT_PATH, service)
    viewer = await connect_bus()
    service.set_items([MenuItem("A")])
    _, first_layout = await _get_layout(viewer, client_bus.unique_name)
    first_ids = {item_id for item_id, _ in _children(first_layout)}

    service.set_items([MenuItem("A"), MenuItem("B")])

    revision, second_layout = await _get_layout(viewer, client_bus.unique_name)
    second_ids = {item_id for item_id, _ in _children(second_layout)}
    assert revision == 2
    assert first_ids.isdisjoint(second_ids)
