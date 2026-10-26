"""Native PipeWire client over ctypes: the node list and the `default` metadata writes.

libpipewire's proxies are called through their method tables, which this module reads
from the `spa_interface` at the start of every proxy, the same way the C inline helpers
do. The registry announces a node with a fixed subset of its properties only, so every
node is bound and its full property set taken from its `info` event. Callbacks arrive
on PipeWire's own thread and are handed to the Qt thread through the running asyncio
loop; nothing here carries audio.
"""

import asyncio
import ctypes
import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde import APPLICATION_NAME
from bt_handsfree_kde.audio.devices import (
    AudioDevice,
    CallAudioNode,
    call_audio_node_from_properties,
    device_from_node_properties,
)

logger = logging.getLogger(__name__)

# Shared library name resolved by the dynamic linker.
LIBPIPEWIRE_NAME = "libpipewire-0.3.so.0"
# Name of the thread libpipewire runs the connection on.
THREAD_NAME = b"bt-handsfree-pipewire"
# Client properties: the Manager category is what WirePlumber requires before it grants
# a sandboxed client write access to metadata; the name shows in `pw-top` and friends.
CLIENT_PROPERTIES = f'{{ media.category = "Manager", application.name = "{APPLICATION_NAME}" }}'
# Interface types as announced by the registry, and the versions this client speaks.
NODE_INTERFACE = b"PipeWire:Interface:Node"
METADATA_INTERFACE = b"PipeWire:Interface:Metadata"
REGISTRY_VERSION = 3
METADATA_VERSION = 3
NODE_VERSION = 3
# Bit of `pw_node_info.change_mask` saying the properties changed.
NODE_CHANGE_MASK_PROPS = 1 << 3
# Name of the metadata object WirePlumber reads routing targets from.
DEFAULT_METADATA_NAME = "default"
METADATA_NAME_PROPERTY = "metadata.name"
# Metadata key WirePlumber's linking reads a node's wanted target from; the value is the
# target's `node.name`, removing the key returns the node to the default device.
TARGET_OBJECT_KEY = b"target.object"
# Bytes reserved for a `spa_hook`, which libpipewire fills in; the struct is six
# pointers long, the rest is slack against growth.
HOOK_SIZE = 64


class _SpaDictItem(ctypes.Structure):
    """Mirror of `spa_dict_item`."""

    _fields_ = [("key", ctypes.c_char_p), ("value", ctypes.c_char_p)]


class _SpaDict(ctypes.Structure):
    """Mirror of `spa_dict`."""

    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("n_items", ctypes.c_uint32),
        ("items", ctypes.POINTER(_SpaDictItem)),
    ]


class _SpaCallbacks(ctypes.Structure):
    """Mirror of `spa_callbacks`: a method table and the object it belongs to."""

    _fields_ = [("funcs", ctypes.c_void_p), ("data", ctypes.c_void_p)]


class _SpaInterface(ctypes.Structure):
    """Mirror of `spa_interface`, the first member of every proxy."""

    _fields_ = [
        ("type", ctypes.c_char_p),
        ("version", ctypes.c_uint32),
        ("cb", _SpaCallbacks),
    ]


# Callback and method prototypes, in the argument order of the C headers.
_GLOBAL_EVENT = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.POINTER(_SpaDict),
)
_GLOBAL_REMOVE_EVENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32)


class _NodeInfo(ctypes.Structure):
    """Mirror of `pw_node_info`."""

    _fields_ = [
        ("id", ctypes.c_uint32),
        ("max_input_ports", ctypes.c_uint32),
        ("max_output_ports", ctypes.c_uint32),
        ("change_mask", ctypes.c_uint64),
        ("n_input_ports", ctypes.c_uint32),
        ("n_output_ports", ctypes.c_uint32),
        ("state", ctypes.c_int),
        ("error", ctypes.c_char_p),
        ("props", ctypes.POINTER(_SpaDict)),
        ("params", ctypes.c_void_p),
        ("n_params", ctypes.c_uint32),
    ]


_NODE_INFO_EVENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_NodeInfo))
_CORE_ERROR_EVENT = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_char_p
)
_ADD_LISTENER_METHOD = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)
_GET_REGISTRY_METHOD = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t
)
_BIND_METHOD = ctypes.CFUNCTYPE(
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.c_size_t,
)
_SET_PROPERTY_METHOD = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
)


class _RegistryEvents(ctypes.Structure):
    """Mirror of `pw_registry_events` (version 0)."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("global_", _GLOBAL_EVENT),
        ("global_remove", _GLOBAL_REMOVE_EVENT),
    ]


class _NodeEvents(ctypes.Structure):
    """Mirror of `pw_node_events` (version 0); only `info` is used."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("info", _NODE_INFO_EVENT),
        ("param", ctypes.c_void_p),
    ]


class _NodeMethods(ctypes.Structure):
    """Mirror of `pw_node_methods` (version 0); only `add_listener` is used."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("add_listener", _ADD_LISTENER_METHOD),
        ("subscribe_params", ctypes.c_void_p),
        ("enum_params", ctypes.c_void_p),
        ("set_param", ctypes.c_void_p),
        ("send_command", ctypes.c_void_p),
    ]


class _CoreEvents(ctypes.Structure):
    """Mirror of `pw_core_events` at version 0; only `error` is used, the rest stay null."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("info", ctypes.c_void_p),
        ("done", ctypes.c_void_p),
        ("ping", ctypes.c_void_p),
        ("error", _CORE_ERROR_EVENT),
        ("remove_id", ctypes.c_void_p),
        ("bound_id", ctypes.c_void_p),
        ("add_mem", ctypes.c_void_p),
        ("remove_mem", ctypes.c_void_p),
    ]


class _CoreMethods(ctypes.Structure):
    """Mirror of `pw_core_methods` (version 0)."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("add_listener", _ADD_LISTENER_METHOD),
        ("hello", ctypes.c_void_p),
        ("sync", ctypes.c_void_p),
        ("pong", ctypes.c_void_p),
        ("error", ctypes.c_void_p),
        ("get_registry", _GET_REGISTRY_METHOD),
        ("create_object", ctypes.c_void_p),
        ("destroy", ctypes.c_void_p),
    ]


class _RegistryMethods(ctypes.Structure):
    """Mirror of `pw_registry_methods` (version 0)."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("add_listener", _ADD_LISTENER_METHOD),
        ("bind", _BIND_METHOD),
        ("destroy", ctypes.c_void_p),
    ]


class _MetadataMethods(ctypes.Structure):
    """Mirror of `pw_metadata_methods` (version 0)."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("add_listener", _ADD_LISTENER_METHOD),
        ("set_property", _SET_PROPERTY_METHOD),
        ("clear", ctypes.c_void_p),
    ]


def libpipewire_is_available() -> bool:
    """Tell whether libpipewire can be loaded, so devices can be listed and calls routed."""
    try:
        ctypes.CDLL(LIBPIPEWIRE_NAME)
    except OSError:
        return False

    return True


def _load_library() -> ctypes.CDLL:
    """Load libpipewire and declare the prototypes of every function used.

    Raises:
        OSError: the library is not installed.
    """
    library = ctypes.CDLL(LIBPIPEWIRE_NAME)

    library.pw_init.restype = None
    library.pw_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.pw_thread_loop_new.restype = ctypes.c_void_p
    library.pw_thread_loop_new.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    library.pw_thread_loop_get_loop.restype = ctypes.c_void_p
    library.pw_thread_loop_get_loop.argtypes = [ctypes.c_void_p]
    library.pw_thread_loop_start.restype = ctypes.c_int
    library.pw_thread_loop_start.argtypes = [ctypes.c_void_p]
    for function_name in ("pw_thread_loop_stop", "pw_thread_loop_lock", "pw_thread_loop_unlock"):
        function = getattr(library, function_name)
        function.restype = None
        function.argtypes = [ctypes.c_void_p]
    library.pw_thread_loop_destroy.restype = None
    library.pw_thread_loop_destroy.argtypes = [ctypes.c_void_p]
    library.pw_properties_new_string.restype = ctypes.c_void_p
    library.pw_properties_new_string.argtypes = [ctypes.c_char_p]
    library.pw_context_new.restype = ctypes.c_void_p
    library.pw_context_new.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    library.pw_context_connect.restype = ctypes.c_void_p
    library.pw_context_connect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    library.pw_context_destroy.restype = None
    library.pw_context_destroy.argtypes = [ctypes.c_void_p]
    library.pw_core_disconnect.restype = ctypes.c_int
    library.pw_core_disconnect.argtypes = [ctypes.c_void_p]
    library.pw_proxy_destroy.restype = None
    library.pw_proxy_destroy.argtypes = [ctypes.c_void_p]

    return library


def _method_table(proxy: int, methods_type: type[ctypes.Structure]) -> tuple[Any, int]:
    """Return a proxy's method table and the object pointer its methods take first."""
    interface = _SpaInterface.from_address(proxy)
    methods = ctypes.cast(interface.cb.funcs, ctypes.POINTER(methods_type)).contents
    object_pointer = interface.cb.data or 0

    return methods, object_pointer


def _dict_to_properties(dictionary: Any) -> dict[str, str]:
    """Copy a `spa_dict` into a Python dict; the C memory is only valid during the callback."""
    properties: dict[str, str] = {}
    if not dictionary:
        return properties

    item_count = dictionary.contents.n_items
    for item_index in range(item_count):
        item = dictionary.contents.items[item_index]
        if item.key is not None and item.value is not None:
            properties[item.key.decode()] = item.value.decode()

    return properties


class PipeWireClient(QObject):
    """Mirrors PipeWire's nodes and writes routing targets into the `default` metadata.

    `start` connects; until it succeeds, and after PipeWire reports an error on the
    connection, the client is unavailable and both lists are empty.
    """

    # Emitted with `True` once the connection is up, with `False` when it is lost.
    availability_changed = Signal(bool)
    # Emitted after a node appeared or disappeared.
    nodes_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        """Create the client; nothing is loaded or connected until `start` runs."""
        super().__init__(parent)
        self._library: ctypes.CDLL | None = None
        self._thread_loop = 0
        self._context = 0
        self._core = 0
        self._registry = 0
        self._metadata = 0
        self._is_available = False
        self._properties_by_node_id: dict[int, dict[str, str]] = {}
        # Per bound node: its proxy pointer and the ctypes objects its listener needs;
        # touched on PipeWire's thread only, and in `_release` once that thread stopped.
        self._node_listeners_by_id: dict[int, tuple[int, Any, Any]] = {}
        # The asyncio loop of the Qt thread, which PipeWire's thread hands events to.
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # ctypes objects that must outlive the connection: callbacks, event tables, hooks.
        self._kept_alive: list[Any] = []

    @property
    def is_available(self) -> bool:
        """Tell whether the connection to PipeWire is up."""
        return self._is_available

    @property
    def devices(self) -> list[AudioDevice]:
        """Return the computer's current sink-like and source-like devices, by node id."""
        devices: list[AudioDevice] = []
        for node_id, properties in sorted(self._properties_by_node_id.items()):
            device = device_from_node_properties(node_id, properties)
            if device is not None:
                devices.append(device)

        return devices

    @property
    def call_nodes(self) -> list[CallAudioNode]:
        """Return the nodes of every phone audio link currently open, by node id."""
        call_nodes: list[CallAudioNode] = []
        for node_id, properties in sorted(self._properties_by_node_id.items()):
            call_node = call_audio_node_from_properties(node_id, properties)
            if call_node is not None:
                call_nodes.append(call_node)

        return call_nodes

    def start(self) -> None:
        """Load libpipewire, connect as a manager and start following the registry.

        Every failure is logged and leaves the client unavailable for good; the app then
        shows that calls use the system default devices.
        """
        try:
            self._library = _load_library()
        except OSError as load_error:
            logger.warning(
                "call audio devices unavailable: %s could not be loaded (%s)",
                LIBPIPEWIRE_NAME,
                load_error,
            )
            return

        self._event_loop = asyncio.get_event_loop()
        library = self._library
        library.pw_init(None, None)
        self._thread_loop = library.pw_thread_loop_new(THREAD_NAME, None) or 0
        main_loop = library.pw_thread_loop_get_loop(self._thread_loop)
        self._context = library.pw_context_new(main_loop, None, 0) or 0
        thread_started = library.pw_thread_loop_start(self._thread_loop) == 0
        if not self._thread_loop or not self._context or not thread_started:
            logger.warning("call audio devices unavailable: PipeWire loop could not start")
            self._release()
            return

        # The connection and the registry subscription are made with the loop locked,
        # as every libpipewire call from outside its thread must be.
        library.pw_thread_loop_lock(self._thread_loop)
        try:
            client_properties = library.pw_properties_new_string(CLIENT_PROPERTIES.encode())
            self._core = library.pw_context_connect(self._context, client_properties, 0) or 0
            if not self._core:
                logger.warning("call audio devices unavailable: PipeWire socket not reachable")
                return

            self._listen_to_core()
            core_methods, core_object = _method_table(self._core, _CoreMethods)
            self._registry = core_methods.get_registry(core_object, REGISTRY_VERSION, 0) or 0
            self._listen_to_registry()
        finally:
            library.pw_thread_loop_unlock(self._thread_loop)

        if not self._core:
            self._release()
            return

        self._is_available = True
        logger.info("PipeWire reachable; call audio devices can be chosen")
        self.availability_changed.emit(True)

    def stop(self) -> None:
        """Disconnect from PipeWire; no signal is emitted afterwards."""
        self._is_available = False
        self._release()

    def set_target(self, node_id: int, device_name: str) -> bool:
        """Route the node at `node_id` to the device called `device_name`.

        Args:
            node_id: Global id of one of a phone's call nodes.
            device_name: `node.name` of the wanted device; empty removes the entry, which
                returns the node to the system default device.

        Returns:
            Whether the entry could be written; `False` while PipeWire is unavailable or
            its `default` metadata object has not been seen.
        """
        can_write = self._is_available and self._metadata != 0
        if not can_write:
            logger.warning("cannot route call audio: PipeWire metadata not available")
            return False

        value = device_name.encode() if device_name else None
        self._library.pw_thread_loop_lock(self._thread_loop)
        try:
            metadata_methods, metadata_object = _method_table(self._metadata, _MetadataMethods)
            metadata_methods.set_property(metadata_object, node_id, TARGET_OBJECT_KEY, None, value)
        finally:
            self._library.pw_thread_loop_unlock(self._thread_loop)

        return True

    def _listen_to_core(self) -> None:
        """Subscribe to the core's error event, which reports a lost connection."""
        events = _CoreEvents()
        events.version = 0
        events.error = _CORE_ERROR_EVENT(self._on_core_error)
        hook = ctypes.create_string_buffer(HOOK_SIZE)
        self._kept_alive.extend([events, hook])

        core_methods, core_object = _method_table(self._core, _CoreMethods)
        core_methods.add_listener(core_object, hook, ctypes.byref(events), None)

    def _listen_to_registry(self) -> None:
        """Subscribe to the registry, which replays every existing global first."""
        events = _RegistryEvents()
        events.version = 0
        events.global_ = _GLOBAL_EVENT(self._on_global)
        events.global_remove = _GLOBAL_REMOVE_EVENT(self._on_global_remove)
        hook = ctypes.create_string_buffer(HOOK_SIZE)
        self._kept_alive.extend([events, hook])

        registry_methods, registry_object = _method_table(self._registry, _RegistryMethods)
        registry_methods.add_listener(registry_object, hook, ctypes.byref(events), None)

    def _on_global(
        self,
        _data: int,
        global_id: int,
        _permissions: int,
        interface_type: bytes,
        _version: int,
        dictionary: Any,
    ) -> None:
        """Registry callback on PipeWire's thread: bind nodes and the default metadata."""
        properties = _dict_to_properties(dictionary)

        if interface_type == NODE_INTERFACE:
            self._listen_to_node(global_id)
        elif interface_type == METADATA_INTERFACE:
            is_default_metadata = properties.get(METADATA_NAME_PROPERTY) == DEFAULT_METADATA_NAME
            if is_default_metadata and not self._metadata:
                registry_methods, registry_object = _method_table(self._registry, _RegistryMethods)
                self._metadata = (
                    registry_methods.bind(
                        registry_object, global_id, METADATA_INTERFACE, METADATA_VERSION, 0
                    )
                    or 0
                )

    def _on_global_remove(self, _data: int, global_id: int) -> None:
        """Registry callback on PipeWire's thread: release and forget a node that went away."""
        node_listener = self._node_listeners_by_id.pop(global_id, None)
        if node_listener is not None:
            node_proxy = node_listener[0]
            self._library.pw_proxy_destroy(node_proxy)

        self._post(self._on_node_removed, global_id)

    def _listen_to_node(self, node_id: int) -> None:
        """Bind a node and subscribe to its `info` event, which carries all its properties."""
        registry_methods, registry_object = _method_table(self._registry, _RegistryMethods)
        node_proxy = (
            registry_methods.bind(registry_object, node_id, NODE_INTERFACE, NODE_VERSION, 0) or 0
        )
        if not node_proxy:
            return

        events = _NodeEvents()
        events.version = 0
        events.info = _NODE_INFO_EVENT(partial(self._on_node_info, node_id))
        hook = ctypes.create_string_buffer(HOOK_SIZE)
        self._node_listeners_by_id[node_id] = (node_proxy, events, hook)

        node_methods, node_object = _method_table(node_proxy, _NodeMethods)
        node_methods.add_listener(node_object, hook, ctypes.byref(events), None)

    def _on_node_info(self, node_id: int, _data: int, info: Any) -> None:
        """Node callback on PipeWire's thread: record the full properties when they change."""
        is_known = node_id in self._node_listeners_by_id
        if not is_known or not info:
            return

        properties_changed = bool(info.contents.change_mask & NODE_CHANGE_MASK_PROPS)
        if properties_changed:
            properties = _dict_to_properties(info.contents.props)
            self._post(self._on_node_added, node_id, properties)

    def _on_core_error(
        self, _data: int, object_id: int, _sequence: int, result: int, message: bytes
    ) -> None:
        """Core callback on PipeWire's thread: an error on the core itself ends the connection."""
        is_connection_error = object_id == 0
        error_text = message.decode() if message else str(result)
        if is_connection_error:
            self._post(self._on_connection_lost, error_text)

    def _post(self, handler: Callable[..., None], *arguments: Any) -> None:
        """Run `handler` on the Qt thread with `arguments`."""
        if self._event_loop is not None:
            self._event_loop.call_soon_threadsafe(handler, *arguments)

    def _on_node_added(self, node_id: int, properties: dict[str, str]) -> None:
        """Record a node and announce the change (Qt thread)."""
        if not self._is_available:
            return

        self._properties_by_node_id[node_id] = properties
        self.nodes_changed.emit()

    def _on_node_removed(self, node_id: int) -> None:
        """Forget a node and announce the change when it was known (Qt thread)."""
        was_known = node_id in self._properties_by_node_id
        if not self._is_available or not was_known:
            return

        del self._properties_by_node_id[node_id]
        self.nodes_changed.emit()

    def _on_connection_lost(self, error_text: str) -> None:
        """Mark the client unavailable after PipeWire reported a core error (Qt thread)."""
        if not self._is_available:
            return

        logger.warning("PipeWire connection lost: %s", error_text)
        self._is_available = False
        self._properties_by_node_id.clear()
        self.availability_changed.emit(False)

    def _release(self) -> None:
        """Stop the loop and free every PipeWire object, in the reverse order of creation."""
        library = self._library
        if library is None:
            return

        if self._thread_loop:
            library.pw_thread_loop_stop(self._thread_loop)
        for node_listener in self._node_listeners_by_id.values():
            node_proxy = node_listener[0]
            library.pw_proxy_destroy(node_proxy)
        self._node_listeners_by_id.clear()
        if self._metadata:
            library.pw_proxy_destroy(self._metadata)
            self._metadata = 0
        if self._registry:
            library.pw_proxy_destroy(self._registry)
            self._registry = 0
        if self._core:
            library.pw_core_disconnect(self._core)
            self._core = 0
        if self._context:
            library.pw_context_destroy(self._context)
            self._context = 0
        if self._thread_loop:
            library.pw_thread_loop_destroy(self._thread_loop)
            self._thread_loop = 0

        self._properties_by_node_id.clear()
        self._kept_alive.clear()
