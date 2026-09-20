"""Tests for the name lookup on a phonebook."""

from bt_handsfree_kde.contacts.phonebook import Phonebook
from bt_handsfree_kde.contacts.vcard import Contact, PhoneNumber


def _contact(name: str, number: str) -> Contact:
    """Build a contact with one mobile number."""
    return Contact(name, (PhoneNumber(number, number, "Mobile", False),))


def test_lookup_matches_national_and_international_forms() -> None:
    """Either form of a number finds the contact; short fragments do not."""
    phonebook = Phonebook((_contact("Alice Doe", "+15550100"), _contact("Bob", "5550103")))

    assert phonebook.lookup_name("+15550100") == "Alice Doe"
    assert phonebook.lookup_name("5550100") == "Alice Doe"
    assert phonebook.lookup_name("+1 (555) 0103") == "Bob"
    assert phonebook.lookup_name("0103") == ""
    assert phonebook.lookup_name("") == ""
    assert phonebook.contact_count == 2
