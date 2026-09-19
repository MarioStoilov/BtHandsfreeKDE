"""In-process server for `org.kde.StatusNotifierItem`, the Plasma tray icon protocol.

Implemented directly instead of through `QSystemTrayIcon` so the item can declare
`ItemIsMenu`, which makes Plasma open the menu on left click as well as right click.
"""

import logging
import sys
from array import array

from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal
from PySide6.QtGui import QIcon, QImage

from bt_handsfree_kde.dbus_helpers import DBusRequestError, call_method

logger = logging.getLogger(__name__)

# Interface and object path of the item; hosts expect exactly this path.
STATUS_NOTIFIER_ITEM_INTERFACE = "org.kde.StatusNotifierItem"
STATUS_NOTIFIER_ITEM_PATH = "/StatusNotifierItem"
# The watcher every item registers with (Plasma runs it inside kded).
WATCHER_BUS_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"
# Item statuses defined by the protocol.
STATUS_ACTIVE = "Active"
STATUS_NEEDS_ATTENTION = "NeedsAttention"
# Category reported for an ordinary application tray icon.
APPLICATION_CATEGORY = "ApplicationStatus"
# Pixel sizes rendered for the pixmap fallback when no theme icon can be named.
PIXMAP_SIZES = (22, 32, 48)
# Bytes per pixel of the ARGB32 format the protocol expects.
BYTES_PER_PIXEL = 4


class StatusNotifierItemService(ServiceInterface):
    """Exports the tray icon's identity, icon, tooltip and status."""

    def __init__(self, item_id: str, title: str, menu_path: str) -> None:
        """Create the item.

        Args:
            item_id: Stable identifier, conventionally the application ID.
            title: Human-readable name shown by hosts.
            menu_path: Object path of the exported `com.canonical.dbusmenu` server.
        """
        super().__init__(STATUS_NOTIFIER_ITEM_INTERFACE)
        self._item_id = item_id
        self._title = title
        self._menu_path = menu_path
        self._status = STATUS_ACTIVE
        self._icon_name = ""
        self._icon_pixmaps: list[list] = []
        self._attention_icon_name = ""
        self._tooltip_title = title
        self._tooltip_text = ""

    def set_icon(self, icon_name: str, pixmaps: list[list]) -> None:
        """Change the icon to a theme name, or to pixmaps when `icon_name` is empty."""
        self._icon_name = icon_name
        self._icon_pixmaps = pixmaps
        self.new_icon()

    def set_attention_icon(self, icon_name: str) -> None:
        """Change the icon hosts show while the status is `NeedsAttention`."""
        self._attention_icon_name = icon_name
        self.new_attention_icon()

    def set_status(self, status: str) -> None:
        """Change the status and announce it only when it differs."""
        status_changed = status != self._status
        self._status = status

        if status_changed:
            self.new_status(status)

    def set_tooltip(self, title: str, text: str) -> None:
        """Change the tooltip title and body."""
        self._tooltip_title = title
        self._tooltip_text = text
        self.new_tool_tip()

    @dbus_property(access=PropertyAccess.READ, name="Category")
    def category(self) -> "s":
        """Protocol category of the item."""
        return APPLICATION_CATEGORY

    @dbus_property(access=PropertyAccess.READ, name="Id")
    def item_id(self) -> "s":
        """Stable identifier of the item."""
        return self._item_id

    @dbus_property(access=PropertyAccess.READ, name="Title")
    def title(self) -> "s":
        """Human-readable name of the item."""
        return self._title

    @dbus_property(access=PropertyAccess.READ, name="Status")
    def status(self) -> "s":
        """Current status, `Active` or `NeedsAttention`."""
        return self._status

    @dbus_property(access=PropertyAccess.READ, name="WindowId")
    def window_id(self) -> "i":
        """Window the item belongs to; none."""
        return 0

    @dbus_property(access=PropertyAccess.READ, name="IconName")
    def icon_name(self) -> "s":
        """Theme icon name, empty when pixmaps are used instead."""
        return self._icon_name

    @dbus_property(access=PropertyAccess.READ, name="IconPixmap")
    def icon_pixmap(self) -> "a(iiay)":
        """Rendered icon in several sizes, used when `IconName` is empty."""
        return self._icon_pixmaps

    @dbus_property(access=PropertyAccess.READ, name="OverlayIconName")
    def overlay_icon_name(self) -> "s":
        """Overlay icon; none."""
        return ""

    @dbus_property(access=PropertyAccess.READ, name="AttentionIconName")
    def attention_icon_name(self) -> "s":
        """Icon shown while the status is `NeedsAttention`."""
        return self._attention_icon_name

    @dbus_property(access=PropertyAccess.READ, name="AttentionMovieName")
    def attention_movie_name(self) -> "s":
        """Animated attention icon; none."""
        return ""

    @dbus_property(access=PropertyAccess.READ, name="ToolTip")
    def tool_tip(self) -> "(sa(iiay)ss)":
        """Tooltip as (icon name, icon pixmaps, title, text)."""
        return ["", [], self._tooltip_title, self._tooltip_text]

    @dbus_property(access=PropertyAccess.READ, name="ItemIsMenu")
    def item_is_menu(self) -> "b":
        """Tell hosts the item only has a menu, so any click should open it."""
        return True

    @dbus_property(access=PropertyAccess.READ, name="Menu")
    def menu(self) -> "o":
        """Object path of the dbusmenu server."""
        return self._menu_path

    @method(name="ContextMenu")
    def context_menu(self, x: "i", y: "i") -> None:
        """Right click; Plasma opens the exported menu itself because `ItemIsMenu` is set."""
        logger.debug("host asked for the context menu at %d,%d", x, y)

    @method(name="Activate")
    def activate(self, x: "i", y: "i") -> None:
        """Left click; Plasma opens the exported menu itself because `ItemIsMenu` is set."""
        logger.debug("host activated the item at %d,%d", x, y)

    @method(name="SecondaryActivate")
    def secondary_activate(self, x: "i", y: "i") -> None:
        """Middle click; no separate action is bound to it."""
        logger.debug("host secondary-activated the item at %d,%d", x, y)

    @method(name="Scroll")
    def scroll(self, delta: "i", orientation: "s") -> None:
        """Mouse wheel over the icon; no action is bound to it."""
        logger.debug("host scrolled the item by %d (%s)", delta, orientation)

    @signal(name="NewIcon")
    def new_icon(self) -> None:
        """Announce a changed icon."""

    @signal(name="NewAttentionIcon")
    def new_attention_icon(self) -> None:
        """Announce a changed attention icon."""

    @signal(name="NewToolTip")
    def new_tool_tip(self) -> None:
        """Announce a changed tooltip."""

    @signal(name="NewStatus")
    def new_status(self, status: str) -> "s":
        """Announce a changed status."""
        return status


async def register_with_watcher(bus: MessageBus) -> bool:
    """Register this connection's item with the StatusNotifierWatcher.

    Returns:
        True when the watcher accepted the registration, False when it is not running
        or refused; the caller retries when the watcher (re)appears on the bus.
    """
    try:
        await call_method(
            bus,
            WATCHER_BUS_NAME,
            WATCHER_PATH,
            WATCHER_INTERFACE,
            "RegisterStatusNotifierItem",
            "s",
            [bus.unique_name],
        )
    except DBusRequestError as request_error:
        logger.warning("tray icon not registered: %s", request_error)
        return False

    logger.info("tray icon registered with the StatusNotifierWatcher")
    return True


def render_icon_pixmaps(icon: QIcon) -> list[list]:
    """Render `icon` into the `a(iiay)` pixmap list the protocol expects.

    Each entry is (width, height, ARGB32 pixels in network byte order).
    """
    pixmaps: list[list] = []

    for size in PIXMAP_SIZES:
        pixmap = icon.pixmap(size, size)
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        width = image.width()
        height = image.height()
        row_length = width * BYTES_PER_PIXEL

        rows: list[bytes] = []
        for row_index in range(height):
            row_view = image.constScanLine(row_index)
            row_bytes = bytes(row_view)[:row_length]
            rows.append(row_bytes)
        host_order_pixels = b"".join(rows)

        pixel_words = array("I", host_order_pixels)
        if sys.byteorder == "little":
            pixel_words.byteswap()
        network_order_pixels = pixel_words.tobytes()

        pixmaps.append([width, height, network_order_pixels])

    return pixmaps
