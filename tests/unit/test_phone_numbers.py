"""Tests for dial-string extraction, `tel:` URIs and number matching."""

import pytest

from bt_handsfree_kde.phone_numbers import (
    dial_string_from_text,
    digits_of,
    number_from_tel_uri,
    same_number,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+1 (555) 010-0", "+15550100"),
        ("555.0100", "5550100"),
        ("*100#", "*100#"),
        ("abc", ""),
        ("1+2", "12"),
        ("²3", "3"),
    ],
)
def test_dial_string_keeps_only_dialable_characters(text: str, expected: str) -> None:
    """Formatting is dropped, `+` survives only in front, other characters are ignored."""
    assert dial_string_from_text(text) == expected


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("tel:+1-555-0100", "+15550100"),
        ("TEL:+1.555.0100;phone-context=example.com", "+15550100"),
        ("tel://+15550100", "+15550100"),
        ("tel:%2B1%20555%200100", "+15550100"),
        ("tel:5550100;ext=12", "5550100"),
        ("  tel:+15550100  ", "+15550100"),
        ("tel:", ""),
        ("mailto:someone@example.com", ""),
        ("+15550100", ""),
    ],
)
def test_number_from_tel_uri(uri: str, expected: str) -> None:
    """RFC 3966 URIs yield their dial string; anything else yields an empty string."""
    assert number_from_tel_uri(uri) == expected


def test_digits_of_drops_everything_but_digits() -> None:
    """Plus signs, symbols and formatting disappear."""
    assert digits_of("+1 (555) 010-0#") == "15550100"
    assert digits_of("MYBANK") == ""


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("15550100", "15550100", True),
        ("5550100", "15550100", True),
        ("0100", "15550100", False),
        ("15550100", "15550199", False),
        ("", "15550100", False),
    ],
)
def test_same_number_allows_a_country_prefix(first: str, second: str, expected: bool) -> None:
    """A national form matches its international form only with a long enough suffix."""
    assert same_number(first, second) is expected
