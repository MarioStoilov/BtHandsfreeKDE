"""Tests for the bMessage reader."""

from bt_handsfree_kde.messages.bmessage import parse_bmessage

INCOMING_BMESSAGE = (
    "BEGIN:BMSG\r\nVERSION:1.0\r\nSTATUS:UNREAD\r\nTYPE:SMS_GSM\r\nFOLDER:telecom/msg/inbox\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alice\r\nN:Alice\r\nTEL:+15550100\r\nEND:VCARD\r\n"
    "BEGIN:BENV\r\nBEGIN:VCARD\r\nVERSION:3.0\r\nFN:Me\r\nTEL:+15550199\r\nEND:VCARD\r\n"
    "BEGIN:BBODY\r\nCHARSET:UTF-8\r\nLENGTH:60\r\nBEGIN:MSG\r\nHello there:\r\nsecond line\r\n"
    "END:MSG\r\nEND:BBODY\r\nEND:BENV\r\nEND:BMSG\r\n"
)
OUTGOING_BMESSAGE = (
    "BEGIN:BMSG\nVERSION:1.0\nSTATUS:READ\nTYPE:SMS_GSM\nFOLDER:telecom/msg/sent\nBEGIN:BENV\n"
    "BEGIN:VCARD\nTEL:+15550100\nEND:VCARD\nBEGIN:BBODY\nBEGIN:MSG\nyo\nEND:MSG\nEND:BBODY\n"
    "END:BENV\nEND:BMSG"
)


def test_incoming_message_yields_originator_flag_and_multiline_text() -> None:
    """The originator vCard before the envelope and the body between the markers are read."""
    parsed = parse_bmessage(INCOMING_BMESSAGE)

    assert parsed.is_read is False
    assert parsed.originator_address == "+15550100"
    assert parsed.text == "Hello there:\nsecond line"


def test_outgoing_message_has_no_originator() -> None:
    """Recipient vCards inside the envelope are not mistaken for the originator."""
    parsed = parse_bmessage(OUTGOING_BMESSAGE)

    assert parsed.is_read is True
    assert parsed.originator_address == ""
    assert parsed.text == "yo"


def test_empty_input_yields_empty_values() -> None:
    """Missing parts do not raise."""
    parsed = parse_bmessage("")

    assert parsed.is_read is None
    assert parsed.text == ""
