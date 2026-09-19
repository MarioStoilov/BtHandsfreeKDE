"""Small helpers shared by the D-Bus clients: variant unwrapping and raw method calls."""

import logging
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

logger = logging.getLogger(__name__)

# Interface name of the standard properties interface every client listens to.
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
# Interface name of the standard object manager interface used for discovery.
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
# Bus name, object path and interface of the message bus daemon itself.
DBUS_DAEMON_BUS_NAME = "org.freedesktop.DBus"
DBUS_DAEMON_PATH = "/org/freedesktop/DBus"
DBUS_DAEMON_INTERFACE = "org.freedesktop.DBus"


class DBusRequestError(Exception):
    """A D-Bus method call was answered with an error or could not be delivered."""


def unwrap_variant(value: Any) -> Any:
    """Return `value` with every `Variant` replaced by its payload, recursively.

    Dictionaries and lists are rebuilt so nested variants inside `a{sv}` and `av`
    containers are unwrapped as well. Plain values are returned unchanged.

    Args:
        value: Any value as delivered by dbus-fast.

    Returns:
        The same structure without `Variant` wrappers.
    """
    if isinstance(value, Variant):
        return unwrap_variant(value.value)

    if isinstance(value, dict):
        unwrapped_mapping: dict[Any, Any] = {}
        for key, nested_value in value.items():
            unwrapped_mapping[key] = unwrap_variant(nested_value)
        return unwrapped_mapping

    if isinstance(value, list):
        unwrapped_items: list[Any] = []
        for nested_value in value:
            unwrapped_items.append(unwrap_variant(nested_value))
        return unwrapped_items

    return value


async def call_method(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: list[Any] | None = None,
) -> list[Any]:
    """Call a D-Bus method by name without introspecting the remote object first.

    Args:
        bus: Connected message bus to send on.
        destination: Bus name of the service.
        path: Object path of the target object.
        interface: Interface that declares the method.
        member: Method name.
        signature: D-Bus signature of the arguments; empty for a method without arguments.
        body: Argument values matching `signature`; `None` for no arguments.

    Returns:
        The reply's body as a list of values, variants left as delivered.

    Raises:
        DBusRequestError: the service answered with an error or the call failed.
    """
    request_body = body if body is not None else []
    request = Message(
        destination=destination,
        path=path,
        interface=interface,
        member=member,
        signature=signature,
        body=request_body,
    )

    try:
        reply = await bus.call(request)
    except Exception as call_failure:
        raise DBusRequestError(
            f"{interface}.{member} on {path} failed: {call_failure}"
        ) from call_failure

    is_error_reply = reply is not None and reply.message_type == MessageType.ERROR
    if is_error_reply:
        error_text = reply.body[0] if reply.body else reply.error_name
        raise DBusRequestError(f"{interface}.{member} on {path}: {reply.error_name}: {error_text}")

    if reply is None:
        return []

    return reply.body


async def set_property(
    bus: MessageBus,
    destination: str,
    path: str,
    interface: str,
    property_name: str,
    value: Variant,
) -> None:
    """Set a D-Bus property through the standard properties interface.

    Args:
        bus: Connected message bus to send on.
        destination: Bus name of the service.
        path: Object path of the target object.
        interface: Interface that declares the property.
        property_name: Name of the property.
        value: New value, already wrapped in a `Variant` with the property's signature.

    Raises:
        DBusRequestError: the service rejected the value or the call failed.
    """
    await call_method(
        bus,
        destination,
        path,
        PROPERTIES_INTERFACE,
        "Set",
        "ssv",
        [interface, property_name, value],
    )


async def add_signal_match(bus: MessageBus, match_rule: str) -> None:
    """Ask the bus daemon to deliver signals matching `match_rule` to this connection.

    Args:
        bus: Connected message bus.
        match_rule: A D-Bus match rule such as `type='signal',sender='org.bluez'`.

    Raises:
        DBusRequestError: the daemon rejected the rule.
    """
    await call_method(
        bus,
        DBUS_DAEMON_BUS_NAME,
        DBUS_DAEMON_PATH,
        DBUS_DAEMON_INTERFACE,
        "AddMatch",
        "s",
        [match_rule],
    )


async def name_has_owner(bus: MessageBus, bus_name: str) -> bool:
    """Tell whether a bus name currently has an owner on `bus`.

    Args:
        bus: Connected message bus.
        bus_name: Well-known bus name to look up.

    Returns:
        True when some connection owns the name right now.

    Raises:
        DBusRequestError: the daemon could not be asked.
    """
    reply_body = await call_method(
        bus,
        DBUS_DAEMON_BUS_NAME,
        DBUS_DAEMON_PATH,
        DBUS_DAEMON_INTERFACE,
        "NameHasOwner",
        "s",
        [bus_name],
    )
    has_owner = bool(reply_body[0])

    return has_owner
