"""The app's settings file: the chosen call audio devices."""

import os
from pathlib import Path

from PySide6.QtCore import QSettings

from bt_handsfree_kde import APPLICATION_ID
from bt_handsfree_kde.audio.devices import SYSTEM_DEFAULT_DEVICE, CallAudioChoice

# Environment variable naming the user's configuration directory; `~/.config` when unset.
# Inside the Flatpak sandbox it points at the app's own configuration directory.
CONFIG_HOME_VARIABLE = "XDG_CONFIG_HOME"
DEFAULT_CONFIG_HOME_RELATIVE = ".config"
# File below the app's configuration directory that holds every setting.
SETTINGS_FILE_NAME = "settings.ini"
# Settings keys ("group/name"). Each holds the `node.name` of a PipeWire device; the
# empty string or an absent key means the system default device.
SPEAKER_KEY = "call_audio/speaker"
MICROPHONE_KEY = "call_audio/microphone"


def settings_file_path() -> Path:
    """Return the path of the settings file: `settings.ini` under the app's config directory."""
    config_home_setting = os.environ.get(CONFIG_HOME_VARIABLE, "")
    if config_home_setting:
        config_home = Path(config_home_setting)
    else:
        config_home = Path.home() / DEFAULT_CONFIG_HOME_RELATIVE

    return config_home / APPLICATION_ID / SETTINGS_FILE_NAME


class CallAudioPreferences:
    """Reads and writes the chosen call audio devices in the settings file."""

    def __init__(self, settings_file: Path | None = None) -> None:
        """Bind to a settings file; the default path is created on the first save.

        Args:
            settings_file: File to use instead of the default, for tests.
        """
        if settings_file is None:
            settings_file = settings_file_path()
        self._settings_file = settings_file

    def load(self) -> CallAudioChoice:
        """Return the stored choice; a missing file or key yields the system default."""
        settings = self._open()
        speaker_name = str(settings.value(SPEAKER_KEY, SYSTEM_DEFAULT_DEVICE))
        microphone_name = str(settings.value(MICROPHONE_KEY, SYSTEM_DEFAULT_DEVICE))

        return CallAudioChoice(speaker_name, microphone_name)

    def save(self, choice: CallAudioChoice) -> None:
        """Write `choice` to the settings file, creating its directory when needed."""
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings = self._open()

        settings.setValue(SPEAKER_KEY, choice.speaker_name)
        settings.setValue(MICROPHONE_KEY, choice.microphone_name)
        settings.sync()

    def _open(self) -> QSettings:
        """Create the Qt settings object over the INI file."""
        return QSettings(str(self._settings_file), QSettings.Format.IniFormat)
