"""Applies the chosen call audio devices to every phone audio link as it appears."""

import logging

from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.audio.devices import (
    DEVICE_KIND_SINK,
    DEVICE_KIND_SOURCE,
    AudioDevice,
    CallAudioChoice,
    target_device_name,
)
from bt_handsfree_kde.audio.pipewire import PipeWireClient
from bt_handsfree_kde.audio.preferences import CallAudioPreferences

logger = logging.getLogger(__name__)


class CallAudioRouter(QObject):
    """Owns the PipeWire client and the stored choice, and keeps the call nodes routed.

    A phone's call nodes exist only while an audio link is open, and PipeWire drops a
    node's metadata with the node, so the choice is written again for every node that
    appears, and rewritten for the nodes present when the choice changes.
    """

    # Emitted after the devices, the availability or the choice changed.
    state_changed = Signal()

    def __init__(
        self,
        client: PipeWireClient,
        preferences: CallAudioPreferences,
        parent: QObject | None = None,
    ) -> None:
        """Create the router over `client` and `preferences`; `start` connects.

        Args:
            client: The PipeWire client, not yet started.
            preferences: Where the choice is read from and written to.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._client = client
        self._preferences = preferences
        self._choice = CallAudioChoice()
        # Ids of the call nodes the current choice has been written for.
        self._routed_node_ids: set[int] = set()

        self._client.availability_changed.connect(self._on_availability_changed)
        self._client.nodes_changed.connect(self._on_nodes_changed)

    @property
    def is_available(self) -> bool:
        """Tell whether PipeWire is reachable, so devices can be listed and chosen."""
        return self._client.is_available

    @property
    def choice(self) -> CallAudioChoice:
        """Return the stored choice."""
        return self._choice

    @property
    def speakers(self) -> list[AudioDevice]:
        """Return the computer's sink-like devices, sorted by description."""
        return self._devices_of_kind(DEVICE_KIND_SINK)

    @property
    def microphones(self) -> list[AudioDevice]:
        """Return the computer's source-like devices, sorted by description."""
        return self._devices_of_kind(DEVICE_KIND_SOURCE)

    def start(self) -> None:
        """Load the stored choice and connect the client."""
        self._choice = self._preferences.load()
        self._client.start()

    def stop(self) -> None:
        """Disconnect the client."""
        self._client.stop()

    def set_speaker(self, device_name: str) -> None:
        """Store `device_name` as the speaker and route the open audio links to it.

        Args:
            device_name: `node.name` of the device; empty for the system default.
        """
        self._choice = CallAudioChoice(device_name, self._choice.microphone_name)
        self._apply_choice_change()

    def set_microphone(self, device_name: str) -> None:
        """Store `device_name` as the microphone and route the open audio links from it.

        Args:
            device_name: `node.name` of the device; empty for the system default.
        """
        self._choice = CallAudioChoice(self._choice.speaker_name, device_name)
        self._apply_choice_change()

    def _apply_choice_change(self) -> None:
        """Persist the choice, rewrite it for every present call node and announce it."""
        self._preferences.save(self._choice)
        self._routed_node_ids.clear()
        self._route_new_nodes()
        self.state_changed.emit()

    def _devices_of_kind(self, kind: str) -> list[AudioDevice]:
        """Return the client's devices of one kind, sorted by description then name."""
        devices_of_kind: list[AudioDevice] = []
        for device in self._client.devices:
            if device.kind == kind:
                devices_of_kind.append(device)

        devices_of_kind.sort(key=_device_sort_key)

        return devices_of_kind

    def _on_availability_changed(self, is_available: bool) -> None:
        """Forget the routed nodes when the connection is lost; announce either way."""
        if not is_available:
            self._routed_node_ids.clear()

        self.state_changed.emit()

    def _on_nodes_changed(self) -> None:
        """Route call nodes that just appeared, forget the ones that left, announce."""
        self._route_new_nodes()
        self.state_changed.emit()

    def _route_new_nodes(self) -> None:
        """Write the choice for every present call node that does not carry it yet."""
        present_node_ids: set[int] = set()

        for call_node in self._client.call_nodes:
            present_node_ids.add(call_node.node_id)
            is_routed = call_node.node_id in self._routed_node_ids
            if is_routed:
                continue

            target_name = target_device_name(call_node, self._choice)
            was_written = self._client.set_target(call_node.node_id, target_name)
            if was_written:
                self._routed_node_ids.add(call_node.node_id)
                logger.info(
                    "call audio %s node %d routed to %s",
                    call_node.kind,
                    call_node.node_id,
                    target_name or "the system default",
                )

        self._routed_node_ids &= present_node_ids


def _device_sort_key(device: AudioDevice) -> tuple[str, str]:
    """Order devices by description, case-insensitively, then by name for stability."""
    description_key = device.description.casefold()

    return (description_key, device.name)
