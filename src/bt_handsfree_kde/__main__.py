"""Command-line entry point: starts Qt, bridges asyncio onto it and runs the application."""

import argparse
import asyncio
import logging
import sys

import qasync
from PySide6.QtWidgets import QApplication

from bt_handsfree_kde import APPLICATION_ID, APPLICATION_NAME, __version__
from bt_handsfree_kde.app import HandsfreeApplication

# Log line layout for stderr; timestamps help correlate with journal output.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main() -> int:
    """Run the tray application until the user quits it.

    A launch while another instance runs hands over its `tel:` URIs and returns at once.

    Returns:
        Process exit code: 0 on a normal quit or a successful hand-off, 1 when the
        running instance could not be reached.
    """
    argument_parser = argparse.ArgumentParser(
        prog="bt-handsfree-kde",
        description="Bluetooth hands-free call controller for the KDE Plasma system tray.",
    )
    argument_parser.add_argument(
        "--verbose", action="store_true", help="log D-Bus traffic details at DEBUG level"
    )
    argument_parser.add_argument("--version", action="version", version=__version__)
    argument_parser.add_argument(
        "uris",
        nargs="*",
        metavar="URI",
        help="tel: URIs whose number is opened in the dialpad; passed to the running "
        "instance when there is one",
    )
    arguments = argument_parser.parse_args()

    log_level = logging.DEBUG if arguments.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=LOG_FORMAT)

    qt_application = QApplication(sys.argv)
    qt_application.setApplicationName(APPLICATION_NAME)
    qt_application.setApplicationVersion(__version__)
    qt_application.setDesktopFileName(APPLICATION_ID)
    qt_application.setQuitOnLastWindowClosed(False)

    event_loop = qasync.QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    quit_event = asyncio.Event()
    qt_application.aboutToQuit.connect(quit_event.set)

    application = HandsfreeApplication(qt_application, arguments.uris)

    with event_loop:
        event_loop.create_task(application.start())
        event_loop.run_until_complete(quit_event.wait())

    return application.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
