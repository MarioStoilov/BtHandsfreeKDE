"""One phone's contacts in memory, and the number matching behind caller ID."""

from dataclasses import dataclass

from bt_handsfree_kde.contacts.vcard import Contact
from bt_handsfree_kde.phone_numbers import digits_of, same_number


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
                if same_number(wanted_digits, known_digits):
                    return contact.name

        return ""
