"""Tests for the router that applies the chosen devices to the phone's call nodes."""

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from bt_handsfree_kde.audio.devices import (
    DEVICE_KIND_SINK,
    DEVICE_KIND_SOURCE,
    AudioDevice,
    CallAudioChoice,
    CallAudioNode,
)
from bt_handsfree_kde.audio.preferences import CallAudioPreferences
from bt_handsfree_kde.audio.router import CallAudioRouter
from tests.conftest import PHONE_ADDRESS

SPEAKER = AudioDevice(7, "alsa_output.example", "Example Speakers", DEVICE_KIND_SINK)
HEADSET = AudioDevice(8, "bluez_output.headset", "Headset", DEVICE_KIND_SINK)
MICROPHONE = AudioDevice(9, "alsa_input.example", "Example Microphone", DEVICE_KIND_SOURCE)
PHONE_SOURCE_NODE = CallAudioNode(20, PHONE_ADDRESS, DEVICE_KIND_SOURCE)
PHONE_SINK_NODE = CallAudioNode(21, PHONE_ADDRESS, DEVICE_KIND_SINK)


class FakePipeWireClient(QObject):
    """Stands in for `PipeWireClient`: scripted devices and call nodes, recorded targets."""

    availability_changed = Signal(bool)
    nodes_changed = Signal()

    def __init__(self) -> None:
        """Start unavailable with nothing listed."""
        super().__init__()
        self.is_available = False
        self.devices: list[AudioDevice] = []
        self.call_nodes: list[CallAudioNode] = []
        self.written_targets: list[tuple[int, str]] = []

    def start(self) -> None:
        """Become available, as a successful connection would."""
        self.is_available = True
        self.availability_changed.emit(True)

    def stop(self) -> None:
        """Become unavailable."""
        self.is_available = False

    def set_target(self, node_id: int, device_name: str) -> bool:
        """Record the write; succeed while available."""
        if not self.is_available:
            return False

        self.written_targets.append((node_id, device_name))
        return True


def test_router_routes_call_nodes_as_they_appear_and_when_the_choice_changes(
    tmp_path: Path,
) -> None:
    """New call nodes get the stored targets once; a new choice is saved and rewritten."""
    preferences = CallAudioPreferences(tmp_path / "settings.ini")
    preferences.save(CallAudioChoice("bluez_output.headset", ""))
    client = FakePipeWireClient()
    router = CallAudioRouter(client, preferences)
    state_changes: list[bool] = []
    router.state_changed.connect(lambda: state_changes.append(True))

    router.start()
    client.devices = [MICROPHONE, SPEAKER, HEADSET]
    client.call_nodes = [PHONE_SOURCE_NODE, PHONE_SINK_NODE]
    client.nodes_changed.emit()

    assert client.written_targets == [(20, "bluez_output.headset"), (21, "")]
    assert [device.description for device in router.speakers] == ["Example Speakers", "Headset"]
    assert [device.description for device in router.microphones] == ["Example Microphone"]

    # An unrelated node change does not rewrite the targets.
    client.nodes_changed.emit()
    assert len(client.written_targets) == 2

    router.set_microphone("alsa_input.example")
    assert client.written_targets[2:] == [
        (20, "bluez_output.headset"),
        (21, "alsa_input.example"),
    ]
    assert preferences.load() == CallAudioChoice("bluez_output.headset", "alsa_input.example")

    # A node that went away and came back is routed again.
    client.call_nodes = []
    client.nodes_changed.emit()
    client.call_nodes = [PHONE_SOURCE_NODE]
    client.nodes_changed.emit()
    assert client.written_targets[-1] == (20, "bluez_output.headset")
    assert state_changes


def test_router_reports_unavailable_pipewire(tmp_path: Path) -> None:
    """Without a connection nothing is written and no devices are listed."""
    client = FakePipeWireClient()
    router = CallAudioRouter(client, CallAudioPreferences(tmp_path / "settings.ini"))

    client.call_nodes = [PHONE_SOURCE_NODE]
    client.nodes_changed.emit()

    assert not router.is_available
    assert client.written_targets == []
    assert router.speakers == [] and router.microphones == []
