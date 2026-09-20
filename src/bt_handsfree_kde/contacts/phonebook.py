"""One phone's contacts in memory, and the number matching behind caller ID."""

from dataclasses import dataclass

from bt_handsfree_kde.contacts.vcard import Contact
from bt_handsfree_kde.phone_numbers import DIGITS

# Two numbers are the same when one ends with the other and the shorter has at least
# this many digits; this matches national and international forms of one number.
MIN_MATCHING_SUFFIX_DIGITS = 7


@dataclass(frozen=True)
class Phonebook:
    """The contacts pulled from one phone, in display order."""

    contacts: tuple[Contact, ...]

    @property
    def contact_count(self) -> int:
        """Return how many contacts the phonebook holds."""
        return len(self.contacts)

    def lookup_name(self, number: str) -> str:
        """Return the name of the contact that has `number`, or an empty string.

        Numbers are compared by digits only; a national form matches its international
        form when they share a suffix of at least `MIN_MATCHING_SUFFIX_DIGITS` digits.
        """
        wanted_digits = digits_of(number)
        if not wanted_digits:
            return ""

        for contact in self.contacts:
            for phone_number in contact.numbers:
                known_digits = digits_of(phone_number.dial_string)
                if _same_number(wanted_digits, known_digits):
                    return contact.name

        return ""


def digits_of(number: str) -> str:
    """Return only the decimal digits of `number`, dropping `+`, symbols and formatting."""
    digit_characters: list[str] = []

    for character in number:
        if character in DIGITS:
            digit_characters.append(character)

    return "".join(digit_characters)


def _same_number(first_digits: str, second_digits: str) -> bool:
    """Tell whether two digit strings denote the same number, allowing a country prefix."""
    if first_digits == second_digits:
        return True

    shorter_digits, longer_digits = sorted((first_digits, second_digits), key=len)
    is_long_enough = len(shorter_digits) >= MIN_MATCHING_SUFFIX_DIGITS

    return is_long_enough and longer_digits.endswith(shorter_digits)
