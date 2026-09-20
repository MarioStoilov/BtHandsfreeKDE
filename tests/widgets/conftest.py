"""Shared data for widget tests: a fake gateway, phone info and a synced phonebook."""

from datetime import datetime

import pytest

from bt_handsfree_kde.contacts.client import SYNC_STATE_SYNCED, PhonebookState
from bt_handsfree_kde.contacts.phonebook import Phonebook
from bt_handsfree_kde.contacts.vcard import Contact, PhoneNumber
from bt_handsfree_kde.dbus.bluez import PhoneInfo
from bt_handsfree_kde.dbus.telephony import AudioGateway
from tests.conftest import PHONE_ADDRESS, PHONE_ALIAS

# Object path of the fake gateway used by the widget tests.
GATEWAY_PATH = "/org/pipewire/Telephony/ag1"
# When the fake phonebook was synced.
SYNCED_AT = datetime(2026, 9, 20, 10, 5)


@pytest.fixture
def gateway() -> AudioGateway:
    """A connected fake phone as the telephony client would report it."""
    return AudioGateway(GATEWAY_PATH, PHONE_ADDRESS, 15, 15, "idle", 0, False)


@pytest.fixture
def phone_info_by_address() -> dict[str, PhoneInfo]:
    """BlueZ details of the fake phone."""
    return {PHONE_ADDRESS: PhoneInfo("/org/bluez/hci0/dev_x", PHONE_ADDRESS, PHONE_ALIAS, True, 80)}


@pytest.fixture
def phonebook() -> Phonebook:
    """Three contacts, one of them with two numbers."""
    return Phonebook(
        (
            Contact(
                "Alice Doe",
                (
                    PhoneNumber("+15550100", "+15550100", "Mobile", True),
                    PhoneNumber("+1 555 0102", "+15550102", "Work", False),
                ),
            ),
            Contact("Bob", (PhoneNumber("5550103", "5550103", "Home", False),)),
            Contact("Carol", (PhoneNumber("5550104", "5550104", "Mobile", False),)),
        )
    )


@pytest.fixture
def synced_phonebook_state(phonebook: Phonebook) -> PhonebookState:
    """The phonebook as a finished sync."""
    return PhonebookState(PHONE_ADDRESS, SYNC_STATE_SYNCED, phonebook, SYNCED_AT, "")
