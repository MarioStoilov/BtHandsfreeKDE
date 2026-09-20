"""Single-instance hand-off over the app's own bus name.

The first process to start owns `io.github.MarioStoilov.BtHandsfreeKDE` on the session bus
and serves one object on it. A later launch, typically the `tel:` URI handler, finds the
name taken, passes its request to the owner and exits, so there is only ever one tray icon.
"""

import logging
from collections.abc import Callable

from dbus_fast import NameFlag, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde import APPLICATION_ID
from bt_handsfree_kde.dbus_helpers import DBusRequestError, call_method

logger = logging.getLogger(__name__)

# Well-known bus name the running instance owns: the application ID, as convention has it.
INSTANCE_BUS_NAME = APPLICATION_ID
# Object path served on that name: the application ID with dots turned into slashes.
INSTANCE_OBJECT_PATH = "/" + APPLICATION_ID.replace(".", "/")
# Interface of that object.
INSTANCE_INTERFACE = APPLICATION_ID
# Replies to `RequestName` that mean this connection holds the name.
OWNING_REPLIES = frozenset({RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER})


class InstanceService(ServiceInterface):
    """Exports what a later launch may ask of the running instance."""

    def __init__(self, on_show_dialpad: Callable[[str], None]) -> None:
        """Create the service.

        Args:
            on_show_dialpad: Called with the requested dial string, possibly empty, when
                another process asks for the dialpad.
        """
        super().__init__(INSTANCE_INTERFACE)
        self._on_show_dialpad = on_show_dialpad

    @method(name="ShowDialpad")
    def show_dialpad(self, number: "s") -> None:
        """Open the dialpad, prefilled with `number` unless it is empty."""
        logger.info("dialpad requested by another process")
        self._on_show_dialpad(number)


class SingleInstance(QObject):
    """Claims the bus name for this process, or reaches the process that already has it."""

    # Emitted with the dial string (possibly empty) when another launch asks for the dialpad.
    dialpad_requested = Signal(str)

    def __init__(self, bus: MessageBus, parent: QObject | None = None) -> None:
        """Create the claimant bound to an already connected session bus.

        Args:
            bus: Connected session bus shared with the other clients.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._bus = bus
        self._service = InstanceService(self.dialpad_requested.emit)

    async def claim(self) -> bool:
        """Try to become the running instance.

        Returns:
            True when this process now owns the name and serves the object on it; False
            when another process owns the name.

        Raises:
            DBusRequestError: the bus daemon could not be asked.
        """
        try:
            reply = await self._bus.request_name(INSTANCE_BUS_NAME, NameFlag.DO_NOT_QUEUE)
        except Exception as request_failure:
            raise DBusRequestError(
                f"RequestName {INSTANCE_BUS_NAME} failed: {request_failure}"
            ) from request_failure

        is_owner = reply in OWNING_REPLIES
        if is_owner:
            self._bus.export(INSTANCE_OBJECT_PATH, self._service)
            logger.info("running as the single instance on %s", INSTANCE_BUS_NAME)
        else:
            logger.info("another instance owns %s", INSTANCE_BUS_NAME)

        return is_owner

    async def show_dialpad_in_running_instance(self, number: str) -> None:
        """Ask the process that owns the name to open its dialpad.

        Args:
            number: Dial string to prefill; empty to only bring the dialpad up.

        Raises:
            DBusRequestError: the owner did not answer or rejected the request.
        """
        await call_method(
            self._bus,
            INSTANCE_BUS_NAME,
            INSTANCE_OBJECT_PATH,
            INSTANCE_INTERFACE,
            "ShowDialpad",
            "s",
            [number],
        )
