"""The computer's audio devices and the phone's call nodes, as derived from PipeWire properties."""

from dataclasses import dataclass

# Kinds of audio device: a sink plays audio (speakers, headphones), a source captures it
# (microphones). The phone's call nodes use the same kinds: its source carries the far
# end's voice, its sink takes what the phone should hear.
DEVICE_KIND_SINK = "sink"
DEVICE_KIND_SOURCE = "source"
# PipeWire media classes that denote a device of each kind.
SINK_MEDIA_CLASSES = ("Audio/Sink",)
SOURCE_MEDIA_CLASSES = ("Audio/Source", "Audio/Source/Virtual")
# Value of `api.bluez5.profile` on the nodes PipeWire creates for a phone's audio link.
GATEWAY_PROFILE = "headset-audio-gateway"
# Choice value meaning "whatever the system default device is"; stored as the empty string.
SYSTEM_DEFAULT_DEVICE = ""
# Node properties read from PipeWire.
MEDIA_CLASS_PROPERTY = "media.class"
NODE_NAME_PROPERTY = "node.name"
NODE_DESCRIPTION_PROPERTY = "node.description"
NODE_NICK_PROPERTY = "node.nick"
BLUEZ_PROFILE_PROPERTY = "api.bluez5.profile"
BLUEZ_ADDRESS_PROPERTY = "api.bluez5.address"


@dataclass(frozen=True)
class AudioDevice:
    """One speaker-like or microphone-like node of the computer."""

    node_id: int
    # `node.name`, the stable identifier a routing target is expressed with.
    name: str
    # `node.description`, what the user sees.
    description: str
    kind: str


@dataclass(frozen=True)
class CallAudioNode:
    """One of the two nodes PipeWire creates for a phone while an audio link is open."""

    node_id: int
    # Bluetooth address of the phone, upper case.
    address: str
    # `DEVICE_KIND_SOURCE` for the far end's voice, `DEVICE_KIND_SINK` for what the
    # phone hears.
    kind: str


@dataclass(frozen=True)
class CallAudioChoice:
    """Which devices of the computer carry calls; empty names mean the system default."""

    speaker_name: str = SYSTEM_DEFAULT_DEVICE
    microphone_name: str = SYSTEM_DEFAULT_DEVICE


def _kind_of_media_class(media_class: str) -> str:
    """Return the device kind a media class denotes, or an empty string for other classes."""
    if media_class in SINK_MEDIA_CLASSES:
        return DEVICE_KIND_SINK

    if media_class in SOURCE_MEDIA_CLASSES:
        return DEVICE_KIND_SOURCE

    return ""


def device_from_node_properties(node_id: int, properties: dict[str, str]) -> AudioDevice | None:
    """Describe a PipeWire node as a device of the computer, or `None` when it is not one.

    Nodes of other media classes (streams, MIDI, video) and the nodes of a phone's own
    audio link are not devices. A node without a description is shown by its nick, then
    by its name.

    Args:
        node_id: Global id of the node.
        properties: The node's properties as delivered by the registry.

    Returns:
        The device, or `None` when the node is not a speaker-like or microphone-like
        device of the computer.
    """
    media_class = properties.get(MEDIA_CLASS_PROPERTY, "")
    kind = _kind_of_media_class(media_class)
    is_phone_node = properties.get(BLUEZ_PROFILE_PROPERTY) == GATEWAY_PROFILE
    name = properties.get(NODE_NAME_PROPERTY, "")
    if not kind or is_phone_node or not name:
        return None

    description = properties.get(NODE_DESCRIPTION_PROPERTY, "")
    if not description:
        description = properties.get(NODE_NICK_PROPERTY, "")
    if not description:
        description = name

    return AudioDevice(node_id, name, description, kind)


def call_audio_node_from_properties(
    node_id: int, properties: dict[str, str]
) -> CallAudioNode | None:
    """Describe a PipeWire node as one of a phone's call nodes, or `None` when it is not.

    Args:
        node_id: Global id of the node.
        properties: The node's properties as delivered by the registry.

    Returns:
        The call node, or `None` when the node does not belong to a phone's audio link.
    """
    is_phone_node = properties.get(BLUEZ_PROFILE_PROPERTY) == GATEWAY_PROFILE
    media_class = properties.get(MEDIA_CLASS_PROPERTY, "")
    kind = _kind_of_media_class(media_class)
    address = properties.get(BLUEZ_ADDRESS_PROPERTY, "")
    if not is_phone_node or not kind or not address:
        return None

    return CallAudioNode(node_id, address.upper(), kind)


def target_device_name(call_node: CallAudioNode, choice: CallAudioChoice) -> str:
    """Return the name of the device a call node should be linked to under `choice`.

    The far end's voice (the phone's source node) goes to the chosen speaker; the
    phone's sink node is fed by the chosen microphone.

    Args:
        call_node: One of the phone's call nodes.
        choice: The user's devices.

    Returns:
        The target device's `node.name`, or the empty string for the system default.
    """
    if call_node.kind == DEVICE_KIND_SOURCE:
        return choice.speaker_name

    return choice.microphone_name
