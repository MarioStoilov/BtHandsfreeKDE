"""One text message as the phone describes it, built from obexd's `Message1` properties."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# `Message1.Folder` values end in one of these; anything else (drafts, deleted) is skipped.
INBOX_FOLDER_NAME = "inbox"
OUTGOING_FOLDER_NAMES = frozenset({"sent", "outbox"})
# `Message1.Type` value for multimedia messages, whose text may be absent.
MMS_TYPE = "mms"
# Longest `Subject` the listing carries; a subject this long may be cut and the full
# text has to be fetched. obexd's `SubjectLength` filter is a byte, so 255 is the most
# it can ask for and the length at which truncation is possible.
SUBJECT_PREVIEW_LIMIT = 255
# Text shown for a multimedia message that carries no text.
MMS_WITHOUT_TEXT = "(picture message)"
# Layouts of `Message1.Timestamp`: MAP 1.0 local time, MAP 1.1+ with a UTC offset.
TIMESTAMP_LAYOUTS = ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%S")
# Object path element that precedes the phone's message handle.
MESSAGE_PATH_MARKER = "/message"


@dataclass(frozen=True)
class TextMessage:
    """One SMS or MMS, incoming or outgoing."""

    # obexd object path of the message; valid while its session lives.
    path: str
    # The phone's handle for the message, stable across sessions.
    handle: str
    # The other party: sender of an incoming message, recipient of an outgoing one.
    counterpart_address: str
    # Name the phone attached to the other party; may equal the address or be empty.
    counterpart_name: str
    # Message text; the listing's preview until `is_text_complete`.
    text: str
    # Whether `text` is the whole message or a preview that may be cut.
    is_text_complete: bool
    # When the message was sent or received, in the phone's local time; `None` when the
    # phone gave none or an unparsable one.
    timestamp: datetime | None
    is_incoming: bool
    is_read: bool
    # `Message1.Type`: `sms-gsm`, `sms-cdma`, `mms` or `email`.
    message_type: str

    @property
    def sort_key(self) -> tuple[datetime, str]:
        """Order messages by time, oldest first; messages without a time come first."""
        if self.timestamp is None:
            return (datetime.min, self.handle)

        return (self.timestamp.replace(tzinfo=None), self.handle)


def message_from_properties(path: str, properties: dict[str, Any]) -> TextMessage | None:
    """Build a `TextMessage` from `Message1` properties (variants already unwrapped).

    Args:
        path: obexd object path of the message.
        properties: The properties obexd delivered for it.

    Returns:
        The message, or `None` when it lives in a folder the app does not show.
    """
    folder = str(properties.get("Folder", ""))
    folder_name = folder.rstrip("/").rsplit("/", 1)[-1].lower()
    is_incoming = folder_name == INBOX_FOLDER_NAME
    is_outgoing = folder_name in OUTGOING_FOLDER_NAMES
    if not is_incoming and not is_outgoing:
        return None

    if is_incoming:
        counterpart_address = str(properties.get("SenderAddress", ""))
        counterpart_name = str(properties.get("Sender", ""))
    else:
        counterpart_address = str(properties.get("RecipientAddress", ""))
        counterpart_name = str(properties.get("Recipient", ""))

    message_type = str(properties.get("Type", ""))
    subject = str(properties.get("Subject", ""))
    has_text = bool(properties.get("Text", True))
    is_multimedia_without_text = message_type == MMS_TYPE and not has_text and not subject
    if is_multimedia_without_text:
        text = MMS_WITHOUT_TEXT
        is_text_complete = True
    else:
        text = subject
        is_text_complete = len(subject) < SUBJECT_PREVIEW_LIMIT

    timestamp = parse_timestamp(str(properties.get("Timestamp", "")))
    is_read = bool(properties.get("Read", False))
    handle = handle_from_path(path)

    return TextMessage(
        path=path,
        handle=handle,
        counterpart_address=counterpart_address,
        counterpart_name=counterpart_name,
        text=text,
        is_text_complete=is_text_complete,
        timestamp=timestamp,
        is_incoming=is_incoming,
        is_read=is_read,
        message_type=message_type,
    )


def handle_from_path(path: str) -> str:
    """Return the phone's message handle encoded at the end of an obexd message path."""
    _, marker, handle = path.rpartition(MESSAGE_PATH_MARKER)
    if not marker:
        return path

    return handle


def parse_timestamp(text: str) -> datetime | None:
    """Parse a MAP timestamp such as `20260920T101500` or `20260920T101500+0200`.

    Returns:
        The parsed time, or `None` when `text` is empty or in another layout.
    """
    stripped_text = text.strip()
    if not stripped_text:
        return None

    for layout in TIMESTAMP_LAYOUTS:
        try:
            return datetime.strptime(stripped_text, layout)
        except ValueError:
            continue

    return None
