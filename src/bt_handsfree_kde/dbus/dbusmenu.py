"""In-process server for the `com.canonical.dbusmenu` interface.

Plasma renders a tray icon's menu itself from this interface, so the same menu opens on
left and right click and is positioned correctly on Wayland. The application describes
the menu as a tree of `MenuItem`s; this module numbers them and answers the protocol.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dbus_fast import Variant
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

logger = logging.getLogger(__name__)

# Interface name and the object path under which the menu is exported.
DBUSMENU_INTERFACE = "com.canonical.dbusmenu"
MENU_OBJECT_PATH = "/MenuBar"
# Protocol version reported to hosts; 3 is what libdbusmenu and Plasma implement.
DBUSMENU_PROTOCOL_VERSION = 3
# Identifier of the invisible root whose children are the top-level entries.
ROOT_ITEM_ID = 0
# Event name a host sends when the user activates an entry.
CLICKED_EVENT = "clicked"
# Toggle states for checkable entries.
TOGGLE_STATE_OFF = 0
TOGGLE_STATE_ON = 1
# Recursion depth meaning "the whole subtree" in `GetLayout`.
UNLIMITED_DEPTH = -1
# Key under which separators are matched between rebuilds.
SEPARATOR_KEY = "-"


@dataclass
class MenuItem:
    """One entry of the tray menu, possibly with a submenu.

    `key` identifies the entry across rebuilds so the host can update it in place; it
    defaults to the label, so entries whose label changes (a call with its duration)
    must set an explicit key.
    """

    label: str = ""
    icon_name: str = ""
    key: str = ""
    enabled: bool = True
    is_separator: bool = False
    checkable: bool = False
    checked: bool = False
    children: list["MenuItem"] = field(default_factory=list)
    # Called without arguments when the host reports a click on this entry.
    on_activated: Callable[[], None] | None = None

    @classmethod
    def separator(cls) -> "MenuItem":
        """Return a separator line."""
        return cls(is_separator=True, key=SEPARATOR_KEY)

    @property
    def identity(self) -> str:
        """Return the key used to match this entry with its predecessor."""
        if self.key:
            return self.key

        return self.label


class DBusMenuService(ServiceInterface):
    """Exports the current menu tree and dispatches clicks to the items' callbacks.

    Hosts cache entries by identifier, so an identifier is never reused for a different
    entry: when the tree keeps its shape, entries keep their identifiers and only
    changed properties are announced; when the shape changes, every entry gets a fresh
    identifier and the host refetches the layout.
    """

    def __init__(self) -> None:
        """Create an empty menu; call `set_items` to populate it."""
        super().__init__(DBUSMENU_INTERFACE)
        self._revision = 0
        self._next_free_id = ROOT_ITEM_ID + 1
        self._items_by_id: dict[int, MenuItem] = {}
        self._child_ids_by_id: dict[int, list[int]] = {ROOT_ITEM_ID: []}
        self._shape: list[tuple[int, str]] = []

    def set_items(self, items: list[MenuItem]) -> None:
        """Replace the menu with `items` as top-level entries and notify hosts.

        Callbacks are always taken from the new items. Hosts are told either about the
        changed properties (same shape) or about a new layout (different shape).
        """
        new_shape = _shape_of(items)
        shape_is_unchanged = new_shape == self._shape

        if shape_is_unchanged:
            self._update_in_place(items)
            return

        self._items_by_id = {}
        self._child_ids_by_id = {ROOT_ITEM_ID: []}
        self._number_children(ROOT_ITEM_ID, items)
        self._shape = new_shape

        self._revision += 1
        self.layout_updated(self._revision, ROOT_ITEM_ID)

    def _number_children(self, parent_id: int, children: list[MenuItem]) -> None:
        """Assign never-before-used identifiers to `children` and their subtrees."""
        for child in children:
            child_id = self._next_free_id
            self._next_free_id += 1

            self._items_by_id[child_id] = child
            self._child_ids_by_id[parent_id].append(child_id)
            self._child_ids_by_id[child_id] = []
            self._number_children(child_id, child.children)

    def _update_in_place(self, items: list[MenuItem]) -> None:
        """Swap in new items under the existing identifiers and announce property changes."""
        ordered_ids = self._ids_in_tree_order(ROOT_ITEM_ID)
        ordered_items = _flatten(items)
        updated_properties: list[list[Any]] = []
        removed_properties: list[list[Any]] = []

        for item_id, new_item in zip(ordered_ids, ordered_items, strict=True):
            previous_properties = self._properties_for(item_id)
            self._items_by_id[item_id] = new_item
            current_properties = self._properties_for(item_id)

            changed: dict[str, Variant] = {}
            for property_name, current_value in current_properties.items():
                previous_value = previous_properties.get(property_name)
                if not _variants_equal(previous_value, current_value):
                    changed[property_name] = current_value
            removed: list[str] = []
            for property_name in previous_properties:
                if property_name not in current_properties:
                    removed.append(property_name)

            if changed:
                updated_properties.append([item_id, changed])
            if removed:
                removed_properties.append([item_id, removed])

        has_changes = bool(updated_properties) or bool(removed_properties)
        if has_changes:
            self.items_properties_updated(updated_properties, removed_properties)

    def _ids_in_tree_order(self, parent_id: int) -> list[int]:
        """Return the identifiers below `parent_id` in depth-first order."""
        ordered_ids: list[int] = []

        for child_id in self._child_ids_by_id.get(parent_id, []):
            ordered_ids.append(child_id)
            ordered_ids.extend(self._ids_in_tree_order(child_id))

        return ordered_ids

    def _properties_for(self, item_id: int) -> dict[str, Variant]:
        """Return the dbusmenu properties describing `item_id`."""
        if item_id == ROOT_ITEM_ID:
            return {"children-display": Variant("s", "submenu")}

        item = self._items_by_id[item_id]
        if item.is_separator:
            return {"type": Variant("s", "separator")}

        properties = {
            "label": Variant("s", item.label),
            "enabled": Variant("b", item.enabled),
            "visible": Variant("b", True),
        }
        if item.icon_name:
            properties["icon-name"] = Variant("s", item.icon_name)
        if item.checkable:
            toggle_state = TOGGLE_STATE_ON if item.checked else TOGGLE_STATE_OFF
            properties["toggle-type"] = Variant("s", "checkmark")
            properties["toggle-state"] = Variant("i", toggle_state)
        if item.children:
            properties["children-display"] = Variant("s", "submenu")

        return properties

    def _layout_for(self, item_id: int, remaining_depth: int) -> list[Any]:
        """Build the `(ia{sv}av)` layout struct for `item_id` down to `remaining_depth`."""
        properties = self._properties_for(item_id)
        child_layouts: list[Variant] = []

        include_children = remaining_depth != 0
        if include_children:
            next_depth = remaining_depth - 1 if remaining_depth > 0 else UNLIMITED_DEPTH
            for child_id in self._child_ids_by_id.get(item_id, []):
                child_layout = self._layout_for(child_id, next_depth)
                child_layouts.append(Variant("(ia{sv}av)", child_layout))

        return [item_id, properties, child_layouts]

    @dbus_property(access=PropertyAccess.READ, name="Version")
    def version(self) -> "u":
        """Protocol version implemented by this server."""
        return DBUSMENU_PROTOCOL_VERSION

    @dbus_property(access=PropertyAccess.READ, name="TextDirection")
    def text_direction(self) -> "s":
        """Text direction of the labels."""
        return "ltr"

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def status(self) -> "s":
        """Menu status; `normal` means the host may show it on demand."""
        return "normal"

    @dbus_property(access=PropertyAccess.READ, name="IconThemePath")
    def icon_theme_path(self) -> "as":
        """Extra icon theme directories; none, all icons come from the desktop theme."""
        return []

    @method(name="GetLayout")
    def get_layout(
        self, parent_id: "i", recursion_depth: "i", property_names: "as"
    ) -> "u(ia{sv}av)":
        """Return the revision and the subtree below `parent_id`.

        `property_names` is a filter hint; every property is returned regardless, which
        the protocol permits.
        """
        if parent_id not in self._child_ids_by_id:
            parent_id = ROOT_ITEM_ID

        layout = self._layout_for(parent_id, recursion_depth)

        return [self._revision, layout]

    @method(name="GetGroupProperties")
    def get_group_properties(self, item_ids: "ai", property_names: "as") -> "a(ia{sv})":
        """Return the properties of each requested item; unknown identifiers are skipped."""
        group_properties: list[list[Any]] = []

        for item_id in item_ids:
            is_known = item_id == ROOT_ITEM_ID or item_id in self._items_by_id
            if is_known:
                properties = self._properties_for(item_id)
                group_properties.append([item_id, properties])

        return group_properties

    @method(name="GetProperty")
    def get_property(self, item_id: "i", property_name: "s") -> "v":
        """Return one property of one item, or an empty string when it is not set."""
        properties = self._properties_for(item_id)
        property_value = properties.get(property_name, Variant("s", ""))

        return property_value

    @method(name="Event")
    def event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u") -> None:
        """Handle a host event; only clicks are acted upon."""
        if event_id != CLICKED_EVENT:
            return

        item = self._items_by_id.get(item_id)
        if item is None or item.on_activated is None:
            return

        logger.debug("menu entry activated: %s", item.label)
        item.on_activated()

    @method(name="EventGroup")
    def event_group(self, events: "a(isvu)") -> "ai":
        """Handle several events at once; returns the identifiers that were unknown."""
        unknown_ids: list[int] = []

        for item_id, event_id, data, timestamp in events:
            if item_id in self._items_by_id:
                self.event(item_id, event_id, data, timestamp)
            else:
                unknown_ids.append(item_id)

        return unknown_ids

    @method(name="AboutToShow")
    def about_to_show(self, item_id: "i") -> "b":
        """Tell the host whether it must refetch before showing; the layout is always current."""
        return False

    @method(name="AboutToShowGroup")
    def about_to_show_group(self, item_ids: "ai") -> "aiai":
        """Group form of `AboutToShow`: nothing needs updating, no identifiers are wrong."""
        return [[], []]

    @signal(name="ItemsPropertiesUpdated")
    def items_properties_updated(
        self, updated_properties: list[list[Any]], removed_properties: list[list[Any]]
    ) -> "a(ia{sv})a(ias)":
        """Announce changed and removed properties of existing entries."""
        return [updated_properties, removed_properties]

    @signal(name="LayoutUpdated")
    def layout_updated(self, revision: int, parent_id: int) -> "ui":
        """Announce that the subtree below `parent_id` changed."""
        return [revision, parent_id]


def _flatten(items: list[MenuItem]) -> list[MenuItem]:
    """Return `items` and their subtrees in depth-first order."""
    flattened: list[MenuItem] = []

    for item in items:
        flattened.append(item)
        flattened.extend(_flatten(item.children))

    return flattened


def _shape_of(items: list[MenuItem]) -> list[tuple[int, str]]:
    """Describe the tree as (depth, identity) pairs; equal shapes allow in-place updates."""
    shape: list[tuple[int, str]] = []

    def visit(children: list[MenuItem], depth: int) -> None:
        for child in children:
            shape.append((depth, child.identity))
            visit(child.children, depth + 1)

    visit(items, 0)

    return shape


def _variants_equal(first: Variant | None, second: Variant) -> bool:
    """Compare two variants by signature and payload; `None` never equals a variant."""
    if first is None:
        return False

    return first.signature == second.signature and first.value == second.value
