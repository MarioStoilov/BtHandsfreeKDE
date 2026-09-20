"""Tests for the vCard reader used on pulled phonebooks."""

from bt_handsfree_kde.contacts.vcard import parse_contacts

VCARD_30_TEXT = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Doe;Alice;;;\r\nFN:Alice Doe\r\n"
    "TEL;TYPE=WORK:+1 555 0102\r\nTEL;TYPE=CELL;TYPE=PREF:+15550100\r\n"
    "TEL;TYPE=CELL:+15550100\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:;;;;\r\nFN:\r\nTEL;TYPE=VOICE:5550199\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Zed;;;;\r\nFN:Zed\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Smith\\, Jr.;Bob;;Dr.;\r\nFN:\r\n"
    'TEL;TYPE="home,pref":(555) 0103\r\nitem1.TEL;TYPE=CELL:5550104\r\n'
    "NOTE:multi\r\n line folded\r\nEND:VCARD\r\n"
    "begin:vcard\r\nversion:3.0\r\nfn:élan Über\r\ntel:+155501\r\nend:vcard\r\n"
)
VCARD_21_TEXT = (
    "BEGIN:VCARD\r\nVERSION:2.1\r\n"
    "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:M=C3=BCller;J=C3=B6rg;;;\r\n"
    "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:J=C3=B6rg M=C3=BCl=\r\nler\r\n"
    "TEL;CELL;PREF:+1-555-0105\r\nTEL;HOME:5550106\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:2.1\r\nN;ENCODING=QUOTED-PRINTABLE:Roe;Ann;;;\r\n"
    "TEL;WORK;FAX:5550107\r\nEND:VCARD\r\n"
)


def test_vcard_30_contacts_are_sorted_named_and_deduplicated() -> None:
    """Cards without a number are dropped, names fall back, duplicates collapse."""
    contacts = parse_contacts(VCARD_30_TEXT)

    names = [contact.name for contact in contacts]
    assert names == ["5550199", "Alice Doe", "Dr. Bob Smith, Jr.", "élan Über"]
    alice_numbers = [
        (number.kind, number.dial_string, number.preferred) for number in contacts[1].numbers
    ]
    assert alice_numbers == [("Mobile", "+15550100", True), ("Work", "+15550102", False)]
    bob_numbers = [(number.kind, number.number) for number in contacts[2].numbers]
    assert bob_numbers == [("Home", "(555) 0103"), ("Mobile", "5550104")]
    assert contacts[0].numbers[0].kind == "Phone"


def test_vcard_21_quoted_printable_and_bare_types() -> None:
    """Quoted-printable names with soft breaks decode; bare 2.1 type words classify numbers."""
    contacts = parse_contacts(VCARD_21_TEXT)

    assert [contact.name for contact in contacts] == ["Ann Roe", "Jörg Müller"]
    joerg_numbers = [
        (number.kind, number.dial_string, number.preferred) for number in contacts[1].numbers
    ]
    assert joerg_numbers == [("Mobile", "+15550105", True), ("Home", "5550106", False)]
    assert contacts[0].numbers[0].kind == "Work"


def test_empty_text_yields_no_contacts() -> None:
    """An empty phonebook parses to an empty list."""
    assert parse_contacts("") == []
