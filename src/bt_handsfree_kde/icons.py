"""Application icon lookup shared by windows, tray and notifications."""

from importlib import resources

from PySide6.QtGui import QIcon

from bt_handsfree_kde import APPLICATION_ID

# Package-relative location of the bundled application icon.
BUNDLED_ICON_RESOURCE = "resources/icon.svg"


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
    show a generic icon, so the bundled file's path is passed instead.
    """
    if QIcon.hasThemeIcon(APPLICATION_ID):
        return APPLICATION_ID

    return bundled_icon_path()
