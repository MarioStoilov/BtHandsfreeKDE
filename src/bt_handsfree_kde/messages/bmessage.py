"""Reader for the bMessage format `Message1.Get` delivers: originator and body text.

A bMessage (Bluetooth MAP specification) wraps a message in `BEGIN:BMSG` / `END:BMSG`
with a status, a type, a folder, zero or more originator vCards, an envelope with
recipient vCards and a body whose text sits between `BEGIN:MSG` and `END:MSG`. Only what
the app shows is read: whether the message is read, the originator's number and the text.
"""

from dataclasses import dataclass

# Line markers of the parts that matter here.
BODY_BEGIN_LINE = "BEGIN:MSG"
BODY_END_LINE = "END:MSG"
ENVELOPE_BEGIN_LINE = "BEGIN:BENV"
VCARD_BEGIN_LINE = "BEGIN:VCARD"
VCARD_END_LINE = "END:VCARD"
# Properties read from the header and from the originator vCard.
STATUS_PROPERTY = "STATUS"
TELEPHONE_PROPERTY = "TEL"
# `STATUS` value marking a read message.
STATUS_READ = "READ"


@dataclass(frozen=True)
class BMessage:
    """The parts of a bMessage the app uses."""

    # Whether the phone marks the message as read; `None` when no status was given.
    is_read: bool | None
    # Telephone number of the originator vCard, empty when there is none (outgoing).
    originator_address: str
    # The message text, body parts concatenated.
    text: str


def parse_bmessage(bmessage_text: str) -> BMessage:
    """Read a bMessage; missing parts yield empty values rather than errors.

    Args:
        bmessage_text: The whole file as delivered by obexd, any line endings.

    Returns:
        The read flag, originator number and text found.
    """
    is_read: bool | None = None
    originator_address = ""
    body_parts: list[str] = []

    is_in_envelope = False
    is_in_vcard = False
    is_in_body = False
    current_body_lines: list[str] = []

    for raw_line in bmessage_text.splitlines():
        line = raw_line.rstrip("\r")
        upper_line = line.upper()

        # The body is copied verbatim until its end marker; nothing inside it is parsed.
        if is_in_body:
            if upper_line == BODY_END_LINE:
                body_parts.append("\n".join(current_body_lines))
                current_body_lines = []
                is_in_body = False
            else:
                current_body_lines.append(line)
            continue

        if upper_line == BODY_BEGIN_LINE:
            is_in_body = True
            continue

        if upper_line == ENVELOPE_BEGIN_LINE:
            is_in_envelope = True
            continue

        # vCards before the envelope describe the originator; those inside it the
        # recipients, which are not needed.
        if upper_line == VCARD_BEGIN_LINE:
            is_in_vcard = True
            continue
        if upper_line == VCARD_END_LINE:
            is_in_vcard = False
            continue

        property_name, has_separator, property_value = line.partition(":")
        if not has_separator:
            continue
        base_property_name = property_name.split(";", 1)[0].strip().upper()

        is_originator_number = (
            is_in_vcard
            and not is_in_envelope
            and base_property_name == TELEPHONE_PROPERTY
            and not originator_address
        )
        if is_originator_number:
            originator_address = property_value.strip()
        elif base_property_name == STATUS_PROPERTY and not is_in_vcard:
            is_read = property_value.strip().upper() == STATUS_READ

    # A body without its end marker (cut transfer) still yields what arrived.
    if current_body_lines:
        body_parts.append("\n".join(current_body_lines))

    text = "".join(body_parts)

    return BMessage(is_read, originator_address, text)
