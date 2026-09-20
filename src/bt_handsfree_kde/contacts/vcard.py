"""Minimal vCard reader for phonebooks pulled over PBAP: names and telephone numbers only.

Phones send vCard 2.1 or 3.0. Only the properties the app asks the phone for (`N`, `FN`,
`TEL`) are read; everything else is skipped. Folded lines, quoted-printable encoding and
the 3.0 escape rules are handled so that a phone ignoring the requested format still
parses.
"""

import codecs
import logging
import quopri
from dataclasses import dataclass

from bt_handsfree_kde.phone_numbers import dial_string_from_text

logger = logging.getLogger(__name__)

# Property names read from each card; everything else is ignored.
FORMATTED_NAME_PROPERTY = "FN"
STRUCTURED_NAME_PROPERTY = "N"
TELEPHONE_PROPERTY = "TEL"
# Lines that delimit one card, compared case-insensitively.
CARD_BEGIN_LINE = "BEGIN:VCARD"
CARD_END_LINE = "END:VCARD"
# Parameter names that affect how a value is decoded or classified.
ENCODING_PARAMETER = "ENCODING"
CHARSET_PARAMETER = "CHARSET"
TYPE_PARAMETER = "TYPE"
# `ENCODING` value used by vCard 2.1 for non-ASCII text.
QUOTED_PRINTABLE_ENCODING = "QUOTED-PRINTABLE"
# Character set assumed for quoted-printable values that name none; vCard 2.1 defaults
# to ASCII, which this contains.
DEFAULT_CHARSET = "utf-8"
# Kind label shown for a number, by its `TYPE` values (lower case); the first value of a
# number that appears here wins, in the number's own order.
KIND_LABEL_BY_TYPE = {
    "cell": "Mobile",
    "mobile": "Mobile",
    "home": "Home",
    "work": "Work",
    "fax": "Fax",
    "pager": "Pager",
}
# Kind label for a number without a recognised type (plain `TEL` or `TYPE=VOICE`).
DEFAULT_KIND_LABEL = "Phone"
# `TYPE` value marking the number a contact prefers to be reached on.
PREFERRED_TYPE = "pref"
# Component positions of the structured `N` value (family, given, additional, prefix,
# suffix per RFC 2426), listed in the order a display name is assembled from them.
NAME_COMPONENT_DISPLAY_ORDER = (3, 1, 2, 0, 4)
# Characters that start a folded continuation line.
FOLD_CONTINUATION_CHARACTERS = (" ", "\t")
# Escape sequences of vCard 3.0 values (after the backslash) and what they stand for.
UNESCAPED_CHARACTER_BY_ESCAPE = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}


@dataclass(frozen=True)
class PhoneNumber:
    """One telephone number of a contact."""

    # The number as the phone delivered it, for display.
    number: str
    # Digits with an optional leading `+`, as `Dial` accepts them.
    dial_string: str
    # Kind label: Mobile, Home, Work, Fax, Pager or Phone.
    kind: str
    # Whether the contact marked this number as preferred.
    preferred: bool


@dataclass(frozen=True)
class Contact:
    """One phonebook entry with at least one number; preferred numbers come first."""

    name: str
    numbers: tuple[PhoneNumber, ...]


@dataclass(frozen=True)
class _Property:
    """One decoded content line of a card."""

    # Property name in upper case, without its group prefix.
    name: str
    # Parameter values (lower case) by upper-case parameter name.
    parameters: dict[str, list[str]]
    # Value with transfer encoding undone, escape sequences still in place.
    value: str


def parse_contacts(vcard_text: str) -> list[Contact]:
    """Read every card in `vcard_text` and return the contacts that have a number.

    Cards without a usable telephone number are dropped. A card without any name is
    named after its first number. The result is sorted by name, case-insensitively.

    Args:
        vcard_text: The whole phonebook file, one or more concatenated cards.

    Returns:
        The contacts in display order; empty when the text holds no usable card.
    """
    logical_lines = _unfold_lines(vcard_text)
    contacts: list[Contact] = []
    card_properties: list[_Property] | None = None

    for logical_line in logical_lines:
        delimiter_candidate = logical_line.strip().upper()
        if delimiter_candidate == CARD_BEGIN_LINE:
            card_properties = []
            continue

        if delimiter_candidate == CARD_END_LINE:
            if card_properties is not None:
                contact = _contact_from_properties(card_properties)
                if contact is not None:
                    contacts.append(contact)
            card_properties = None
            continue

        is_inside_card = card_properties is not None
        if not is_inside_card:
            continue

        content_property = _parse_property(logical_line)
        if content_property is not None:
            card_properties.append(content_property)

    contacts.sort(key=_contact_sort_key)

    return contacts


def _unfold_lines(vcard_text: str) -> list[str]:
    """Join folded continuation lines and quoted-printable soft line breaks.

    A line starting with a space or tab continues the previous one (RFC 2425 folding).
    A quoted-printable value ending in `=` continues on the next line (vCard 2.1).
    """
    logical_lines: list[str] = []

    for raw_line in vcard_text.splitlines():
        has_previous_line = bool(logical_lines)
        is_fold_continuation = has_previous_line and raw_line[:1] in FOLD_CONTINUATION_CHARACTERS
        if is_fold_continuation:
            logical_lines[-1] += raw_line[1:]
            continue

        previous_line = logical_lines[-1] if has_previous_line else ""
        is_soft_break_continuation = previous_line.endswith("=") and _is_quoted_printable_line(
            previous_line
        )
        if is_soft_break_continuation:
            logical_lines[-1] = previous_line[:-1] + raw_line
            continue

        logical_lines.append(raw_line)

    return logical_lines


def _is_quoted_printable_line(logical_line: str) -> bool:
    """Tell whether the content line declares a quoted-printable value."""
    separator_index = _find_value_separator(logical_line)
    descriptor = logical_line[:separator_index] if separator_index >= 0 else logical_line
    descriptor_upper = descriptor.upper()

    return QUOTED_PRINTABLE_ENCODING in descriptor_upper


def _find_value_separator(logical_line: str) -> int:
    """Return the index of the `:` that separates name and parameters from the value.

    Colons inside double-quoted parameter values do not count. Returns -1 when the line
    has no separator.
    """
    is_inside_quotes = False

    for character_index, character in enumerate(logical_line):
        if character == '"':
            is_inside_quotes = not is_inside_quotes
        elif character == ":" and not is_inside_quotes:
            return character_index

    return -1


def _parse_property(logical_line: str) -> _Property | None:
    """Split one content line into name, parameters and decoded value.

    Returns `None` for lines without a `:` separator, which are not content lines.
    """
    separator_index = _find_value_separator(logical_line)
    if separator_index < 0:
        return None

    descriptor = logical_line[:separator_index]
    raw_value = logical_line[separator_index + 1 :]
    descriptor_parts = descriptor.split(";")
    qualified_name = descriptor_parts[0]
    parameter_parts = descriptor_parts[1:]
    _, _, property_name = qualified_name.rpartition(".")

    parameters: dict[str, list[str]] = {}
    for parameter_part in parameter_parts:
        parameter_key, has_explicit_value, parameter_value_text = parameter_part.partition("=")
        if has_explicit_value:
            parameter_name = parameter_key.strip().upper()
            unquoted_value_text = parameter_value_text.strip().strip('"')
            parameter_values = unquoted_value_text.split(",")
        else:
            # vCard 2.1 writes bare type values: `TEL;CELL;PREF:`.
            parameter_name = TYPE_PARAMETER
            parameter_values = [parameter_part]

        known_values = parameters.setdefault(parameter_name, [])
        for parameter_value in parameter_values:
            known_values.append(parameter_value.strip().lower())

    decoded_value = _decode_value(raw_value, parameters)

    return _Property(property_name.strip().upper(), parameters, decoded_value)


def _decode_value(raw_value: str, parameters: dict[str, list[str]]) -> str:
    """Undo the quoted-printable transfer encoding when the parameters declare it."""
    encodings = parameters.get(ENCODING_PARAMETER, [])
    is_quoted_printable = QUOTED_PRINTABLE_ENCODING.lower() in encodings
    if not is_quoted_printable:
        return raw_value

    charsets = parameters.get(CHARSET_PARAMETER, [])
    charset = charsets[0] if charsets else DEFAULT_CHARSET
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = DEFAULT_CHARSET

    encoded_bytes = raw_value.encode("latin-1", errors="replace")
    decoded_bytes = quopri.decodestring(encoded_bytes)

    return decoded_bytes.decode(charset, errors="replace")


def _unescape(value: str) -> str:
    """Replace vCard 3.0 escape sequences with the characters they stand for."""
    unescaped_characters: list[str] = []
    is_after_backslash = False

    for character in value:
        if is_after_backslash:
            unescaped_characters.append(UNESCAPED_CHARACTER_BY_ESCAPE.get(character, character))
            is_after_backslash = False
        elif character == "\\":
            is_after_backslash = True
        else:
            unescaped_characters.append(character)

    return "".join(unescaped_characters)


def _split_components(value: str) -> list[str]:
    """Split a structured value on `;` separators that are not escaped, unescaping each part."""
    components: list[str] = []
    current_component: list[str] = []
    is_after_backslash = False

    for character in value:
        if is_after_backslash:
            current_component.append(character)
            is_after_backslash = False
        elif character == "\\":
            current_component.append(character)
            is_after_backslash = True
        elif character == ";":
            components.append("".join(current_component))
            current_component = []
        else:
            current_component.append(character)
    components.append("".join(current_component))

    unescaped_components: list[str] = []
    for component in components:
        unescaped_components.append(_unescape(component).strip())

    return unescaped_components


def _contact_from_properties(properties: list[_Property]) -> Contact | None:
    """Assemble a contact from a card's properties; `None` when it has no number."""
    formatted_name = ""
    structured_name = ""
    numbers: list[PhoneNumber] = []
    seen_dial_strings: set[str] = set()

    for content_property in properties:
        if content_property.name == TELEPHONE_PROPERTY:
            phone_number = _phone_number_from_property(content_property)
            is_new_number = (
                phone_number is not None and phone_number.dial_string not in seen_dial_strings
            )
            if is_new_number:
                numbers.append(phone_number)
                seen_dial_strings.add(phone_number.dial_string)
        elif content_property.name == FORMATTED_NAME_PROPERTY and not formatted_name:
            formatted_name = _unescape(content_property.value).strip()
        elif content_property.name == STRUCTURED_NAME_PROPERTY and not structured_name:
            structured_name = content_property.value

    if not numbers:
        return None

    preferred_numbers: list[PhoneNumber] = []
    other_numbers: list[PhoneNumber] = []
    for phone_number in numbers:
        if phone_number.preferred:
            preferred_numbers.append(phone_number)
        else:
            other_numbers.append(phone_number)
    ordered_numbers = preferred_numbers + other_numbers

    name = formatted_name or _display_name_from_structured(structured_name)
    if not name:
        first_number = ordered_numbers[0]
        name = first_number.number

    return Contact(name, tuple(ordered_numbers))


def _display_name_from_structured(structured_name: str) -> str:
    """Build a display name from an `N` value: prefix, given, additional, family, suffix."""
    components = _split_components(structured_name)
    name_parts: list[str] = []

    for component_index in NAME_COMPONENT_DISPLAY_ORDER:
        has_component = component_index < len(components)
        if has_component and components[component_index]:
            name_parts.append(components[component_index])

    return " ".join(name_parts)


def _phone_number_from_property(content_property: _Property) -> PhoneNumber | None:
    """Turn a `TEL` property into a `PhoneNumber`; `None` when nothing is dialable."""
    number_text = _unescape(content_property.value).strip()
    dial_string = dial_string_from_text(number_text)
    if not dial_string:
        return None

    type_values = content_property.parameters.get(TYPE_PARAMETER, [])
    kind = DEFAULT_KIND_LABEL
    for type_value in type_values:
        kind_label = KIND_LABEL_BY_TYPE.get(type_value)
        if kind_label is not None:
            kind = kind_label
            break
    is_preferred = PREFERRED_TYPE in type_values

    return PhoneNumber(number_text, dial_string, kind, is_preferred)


def _contact_sort_key(contact: Contact) -> tuple[str, str]:
    """Order contacts case-insensitively by name, ties broken by the exact name."""
    return (contact.name.casefold(), contact.name)
