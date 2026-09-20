"""Dial strings: what the phone accepts, extracted from typed numbers and `tel:` URIs."""

from urllib.parse import unquote

# URI scheme the desktop file registers the app for, compared case-insensitively.
TEL_URI_SCHEME = "tel"
# The ten digits; `str.isdigit` would also accept other Unicode digits a phone cannot dial.
DIGITS = "0123456789"
# International prefix, valid only as the first character of a dial string.
INTERNATIONAL_PREFIX = "+"
# DTMF symbols a phone dials like digits.
DTMF_SYMBOLS = "*#"
# What the number field accepts while typing: dial-string characters plus the visual
# separators RFC 3966 allows in `tel:` URIs (space, hyphen, dot, parentheses), so a
# formatted number can be pasted as is. The separators are dropped when dialling.
NUMBER_FIELD_PATTERN = r"[0-9+*# ().-]*"
# Marks the start of the URI parameters (`phone-context`, `ext`, ...), which are ignored.
URI_PARAMETER_SEPARATOR = ";"
# Some applications emit `tel://+15550100`; RFC 3966 has no authority part, so the marker
# is tolerated and skipped.
AUTHORITY_MARKER = "//"


def dial_string_from_text(text: str) -> str:
    """Reduce a typed or pasted number to the string handed to the phone.

    Formatting characters are dropped, `+` is kept only when it leads the number and
    any other character is ignored.

    Args:
        text: Number as typed, pasted or extracted from a URI.

    Returns:
        Digits, `*` and `#` with an optional leading `+`; empty when nothing dialable
        is left.
    """
    dial_characters: list[str] = []

    for character in text:
        is_dialable = character in DIGITS or character in DTMF_SYMBOLS
        is_leading_prefix = character == INTERNATIONAL_PREFIX and not dial_characters
        if is_dialable or is_leading_prefix:
            dial_characters.append(character)

    return "".join(dial_characters)


def number_from_tel_uri(uri: str) -> str:
    """Extract the dial string from a `tel:` URI as defined by RFC 3966.

    Parameters after the first `;` are ignored, percent-encoding is decoded and a `//`
    after the scheme is tolerated.

    Args:
        uri: The URI as received on the command line, such as `tel:+1-555-0100`.

    Returns:
        The dial string, or an empty string when `uri` is not a `tel:` URI or carries
        no dialable number.
    """
    scheme, colon, remainder = uri.strip().partition(":")
    normalised_scheme = scheme.strip().lower()
    is_tel_uri = colon == ":" and normalised_scheme == TEL_URI_SCHEME
    if not is_tel_uri:
        return ""

    number_part, _, _ = remainder.partition(URI_PARAMETER_SEPARATOR)
    number_without_marker = number_part.removeprefix(AUTHORITY_MARKER)
    decoded_number = unquote(number_without_marker)

    return dial_string_from_text(decoded_number)
