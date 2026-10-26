"""Tests for deriving devices and call nodes from PipeWire node properties."""

from bt_handsfree_kde.audio.devices import (
    DEVICE_KIND_SINK,
    DEVICE_KIND_SOURCE,
    CallAudioChoice,
    CallAudioNode,
    call_audio_node_from_properties,
    device_from_node_properties,
    target_device_name,
)
from tests.conftest import PHONE_ADDRESS

# Node properties as PipeWire's registry announces them (fictional device names).
SPEAKER_PROPERTIES = {
    "media.class": "Audio/Sink",
    "node.name": "alsa_output.example.analog-stereo",
    "node.description": "Example Analog Stereo",
}
VIRTUAL_MICROPHONE_PROPERTIES = {
    "media.class": "Audio/Source/Virtual",
    "node.name": "effects_source",
    "node.nick": "Effects",
}
STREAM_PROPERTIES = {"media.class": "Stream/Output/Audio", "node.name": "music-player"}
PHONE_SOURCE_PROPERTIES = {
    "media.class": "Audio/Source",
    "node.name": "bluez_input.example.1",
    "api.bluez5.profile": "headset-audio-gateway",
    "api.bluez5.address": PHONE_ADDRESS.lower(),
}
PHONE_SINK_PROPERTIES = {
    "media.class": "Audio/Sink",
    "node.name": "bluez_output.example.1",
    "api.bluez5.profile": "headset-audio-gateway",
    "api.bluez5.address": PHONE_ADDRESS,
}


def test_devices_come_from_sink_and_source_classes_only() -> None:
    """Sinks and sources become devices with a description; streams and phone nodes do not."""
    speaker = device_from_node_properties(7, SPEAKER_PROPERTIES)
    microphone = device_from_node_properties(8, VIRTUAL_MICROPHONE_PROPERTIES)

    assert speaker is not None and speaker.kind == DEVICE_KIND_SINK
    assert speaker.name == "alsa_output.example.analog-stereo"
    assert speaker.description == "Example Analog Stereo"
    assert microphone is not None and microphone.kind == DEVICE_KIND_SOURCE
    assert microphone.description == "Effects"
    assert device_from_node_properties(9, STREAM_PROPERTIES) is None
    assert device_from_node_properties(10, PHONE_SOURCE_PROPERTIES) is None
    assert device_from_node_properties(11, {"media.class": "Audio/Sink"}) is None


def test_call_nodes_are_the_phone_profile_nodes() -> None:
    """Only gateway-profile nodes with an address are call nodes; the address is upper-cased."""
    source_node = call_audio_node_from_properties(10, PHONE_SOURCE_PROPERTIES)
    sink_node = call_audio_node_from_properties(12, PHONE_SINK_PROPERTIES)

    assert source_node == CallAudioNode(10, PHONE_ADDRESS, DEVICE_KIND_SOURCE)
    assert sink_node == CallAudioNode(12, PHONE_ADDRESS, DEVICE_KIND_SINK)
    assert call_audio_node_from_properties(7, SPEAKER_PROPERTIES) is None


def test_target_follows_the_node_direction() -> None:
    """The phone's source goes to the speaker; the phone's sink is fed by the microphone."""
    choice = CallAudioChoice("headset_sink", "usb_microphone")
    source_node = CallAudioNode(10, PHONE_ADDRESS, DEVICE_KIND_SOURCE)
    sink_node = CallAudioNode(12, PHONE_ADDRESS, DEVICE_KIND_SINK)

    assert target_device_name(source_node, choice) == "headset_sink"
    assert target_device_name(sink_node, choice) == "usb_microphone"
    assert target_device_name(source_node, CallAudioChoice()) == ""
