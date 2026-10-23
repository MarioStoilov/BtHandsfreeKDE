"""Application icon lookup shared by windows, tray and notifications."""

from importlib import resources

from PySide6.QtGui import QIcon, QImage

from bt_handsfree_kde import APPLICATION_ID

# Package-relative location of the bundled application icon.
BUNDLED_ICON_RESOURCE = "resources/icon.svg"
# Bytes per pixel of the RGBA format `application_icon_rgba` produces.
RGBA_BYTES_PER_PIXEL = 4


def bundled_icon_path() -> str:
    """Return the absolute path of the SVG shipped inside the package."""
    icon_resource = resources.files("bt_handsfree_kde") / BUNDLED_ICON_RESOURCE

    return str(icon_resource)


def application_icon() -> QIcon:
    """Return the installed theme icon for the app, falling back to the bundled SVG."""
    bundled_icon = QIcon(bundled_icon_path())

    return QIcon.fromTheme(APPLICATION_ID, bundled_icon)


def application_icon_reference() -> str:
    """Return what to hand to other processes as the app icon: theme name or file path.

    Installed builds (Flatpak, packaged) have the icon in the hicolor theme, so the app
    ID resolves. Running from a checkout it does not, and the notification server would
    show a generic icon, so the bundled file's path is passed instead. Inside a sandbox
    neither reference is guaranteed to resolve for the receiving process; callers that
    must show the icon send its pixels too, see `application_icon_rgba`.
    """
    if QIcon.hasThemeIcon(APPLICATION_ID):
        return APPLICATION_ID

    return bundled_icon_path()


def application_icon_rgba(size: int) -> tuple[int, int, int, bytes]:
    """Render the app icon to raw RGBA pixels for processes that cannot load it themselves.

    A Flatpak's icon file lives inside the sandbox and its theme name is only visible to
    the host once Flatpak's export directory is on the host's icon search path, so other
    processes are handed the pixels instead of a reference.

    Args:
        size: Edge length of the square image in pixels.

    Returns:
        Width, height, bytes per row and the pixel data, 8 bits per channel in R, G, B, A
        order, row after row without padding.
    """
    icon_pixmap = application_icon().pixmap(size, size)
    icon_image = icon_pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    width = icon_image.width()
    height = icon_image.height()
    row_length = width * RGBA_BYTES_PER_PIXEL

    rows: list[bytes] = []
    for row_index in range(height):
        row_view = icon_image.constScanLine(row_index)
        row_bytes = bytes(row_view)[:row_length]
        rows.append(row_bytes)
    pixels = b"".join(rows)

    return width, height, row_length, pixels
