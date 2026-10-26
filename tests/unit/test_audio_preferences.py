"""Tests for the settings file holding the chosen call audio devices."""

from pathlib import Path

from bt_handsfree_kde.audio.devices import CallAudioChoice
from bt_handsfree_kde.audio.preferences import CallAudioPreferences


def test_choice_round_trips_through_the_settings_file(tmp_path: Path) -> None:
    """A missing file yields the defaults; a saved choice is read back by a new instance."""
    settings_file = tmp_path / "config" / "settings.ini"
    preferences = CallAudioPreferences(settings_file)
    assert preferences.load() == CallAudioChoice()

    preferences.save(CallAudioChoice("headset_sink", "usb_microphone"))

    assert settings_file.is_file()
    reloaded = CallAudioPreferences(settings_file).load()
    assert reloaded == CallAudioChoice("headset_sink", "usb_microphone")

    preferences.save(CallAudioChoice())
    assert CallAudioPreferences(settings_file).load() == CallAudioChoice()
