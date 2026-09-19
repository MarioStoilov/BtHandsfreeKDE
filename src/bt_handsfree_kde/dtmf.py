"""Local DTMF feedback tones: synthesised in Python, played through libpulse's simple API.

The tone the far end hears is sent by the phone (`AudioGateway1.SendTones`); this module
only reproduces the audible key feedback a phone gives the user. libpulse is loaded
through ctypes so no Python multimedia package is needed; PipeWire serves the API.
"""

import ctypes
import logging
import math
import threading
from array import array

from bt_handsfree_kde import APPLICATION_NAME

logger = logging.getLogger(__name__)

# Low and high frequencies of each key, in hertz, per ITU-T Q.23.
LOW_FREQUENCY_BY_KEY = {
    "1": 697,
    "2": 697,
    "3": 697,
    "4": 770,
    "5": 770,
    "6": 770,
    "7": 852,
    "8": 852,
    "9": 852,
    "*": 941,
    "0": 941,
    "#": 941,
}
HIGH_FREQUENCY_BY_KEY = {
    "1": 1209,
    "2": 1336,
    "3": 1477,
    "4": 1209,
    "5": 1336,
    "6": 1477,
    "7": 1209,
    "8": 1336,
    "9": 1477,
    "*": 1209,
    "0": 1336,
    "#": 1477,
}
# Length of one feedback tone, in milliseconds; phones use roughly this.
TONE_DURATION_MS = 150
# Fade-in and fade-out length, in milliseconds, to avoid clicks at the tone edges.
TONE_RAMP_MS = 5
# Sample rate of the synthesised tone; 8 kHz is plenty for frequencies below 1.5 kHz.
SAMPLE_RATE_HZ = 8000
# Peak amplitude as a fraction of full scale; two sines are summed, so the sum stays below 1.
TONE_AMPLITUDE = 0.35
# Largest positive value of a signed 16-bit sample.
INT16_FULL_SCALE = 32767
# libpulse constants: 16-bit little-endian PCM and the playback stream direction.
PA_SAMPLE_S16LE = 3
PA_STREAM_PLAYBACK = 1
# Shared library name resolved by the dynamic linker.
LIBPULSE_SIMPLE_NAME = "libpulse-simple.so.0"
# Stream name shown in the audio mixer.
STREAM_NAME = "DTMF key tone"


class _PaSampleSpec(ctypes.Structure):
    """Mirror of libpulse's `pa_sample_spec`."""

    _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]


def render_tone(key: str) -> bytes:
    """Synthesise the dual-tone signal for `key` as 16-bit mono PCM.

    Args:
        key: One of the twelve dialpad keys.

    Returns:
        Little-endian signed 16-bit samples at `SAMPLE_RATE_HZ`, `TONE_DURATION_MS` long.

    Raises:
        KeyError: `key` is not a dialpad key.
    """
    low_frequency = LOW_FREQUENCY_BY_KEY[key]
    high_frequency = HIGH_FREQUENCY_BY_KEY[key]
    sample_count = SAMPLE_RATE_HZ * TONE_DURATION_MS // 1000
    ramp_samples = SAMPLE_RATE_HZ * TONE_RAMP_MS // 1000
    samples = array("h")

    for sample_index in range(sample_count):
        seconds = sample_index / SAMPLE_RATE_HZ
        low_component = math.sin(2 * math.pi * low_frequency * seconds)
        high_component = math.sin(2 * math.pi * high_frequency * seconds)
        envelope = _envelope(sample_index, sample_count, ramp_samples)

        mixed = (low_component + high_component) * TONE_AMPLITUDE * envelope
        samples.append(int(mixed * INT16_FULL_SCALE))

    return samples.tobytes()


def _envelope(sample_index: int, sample_count: int, ramp_samples: int) -> float:
    """Return the linear fade factor (0..1) for a sample position."""
    samples_from_end = sample_count - sample_index

    if sample_index < ramp_samples:
        return sample_index / ramp_samples

    if samples_from_end < ramp_samples:
        return samples_from_end / ramp_samples

    return 1.0


class DtmfTonePlayer:
    """Plays key feedback tones on the default audio output without blocking the UI.

    Tones are rendered once per key on first use. Playback happens on a short-lived
    thread per press because the simple API blocks while the tone drains. A missing
    libpulse is reported once and playback is then silently skipped.
    """

    def __init__(self) -> None:
        """Load libpulse lazily; nothing is opened until the first tone."""
        self._pcm_by_key: dict[str, bytes] = {}
        self._library: ctypes.CDLL | None = None
        self._library_missing = False

    def play(self, key: str) -> None:
        """Play the tone for `key`; unknown keys and missing audio are ignored."""
        if key not in LOW_FREQUENCY_BY_KEY:
            return

        library = self._load_library()
        if library is None:
            return

        pcm = self._pcm_by_key.get(key)
        if pcm is None:
            pcm = render_tone(key)
            self._pcm_by_key[key] = pcm

        playback_thread = threading.Thread(
            target=self._play_blocking, args=(library, pcm), name="dtmf-tone", daemon=True
        )
        playback_thread.start()

    def _load_library(self) -> ctypes.CDLL | None:
        """Return libpulse-simple, loading it on first call; `None` when unavailable."""
        if self._library is not None:
            return self._library

        if self._library_missing:
            return None

        try:
            library = ctypes.CDLL(LIBPULSE_SIMPLE_NAME)
        except OSError as load_error:
            logger.warning(
                "no key tones: %s could not be loaded (%s)", LIBPULSE_SIMPLE_NAME, load_error
            )
            self._library_missing = True
            return None

        library.pa_simple_new.restype = ctypes.c_void_p
        library.pa_simple_new.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(_PaSampleSpec),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        library.pa_simple_write.restype = ctypes.c_int
        library.pa_simple_write.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int),
        ]
        library.pa_simple_drain.restype = ctypes.c_int
        library.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        library.pa_simple_free.restype = None
        library.pa_simple_free.argtypes = [ctypes.c_void_p]
        self._library = library

        return library

    @staticmethod
    def _play_blocking(library: ctypes.CDLL, pcm: bytes) -> None:
        """Open a playback stream, write `pcm`, wait for it to finish and close the stream."""
        sample_spec = _PaSampleSpec(PA_SAMPLE_S16LE, SAMPLE_RATE_HZ, 1)
        error_code = ctypes.c_int(0)
        application_name = APPLICATION_NAME.encode()
        stream_name = STREAM_NAME.encode()

        stream = library.pa_simple_new(
            None,
            application_name,
            PA_STREAM_PLAYBACK,
            None,
            stream_name,
            ctypes.byref(sample_spec),
            None,
            None,
            ctypes.byref(error_code),
        )
        if not stream:
            logger.warning(
                "key tone playback stream could not be opened (error %d)", error_code.value
            )
            return

        write_result = library.pa_simple_write(stream, pcm, len(pcm), ctypes.byref(error_code))
        if write_result == 0:
            library.pa_simple_drain(stream, ctypes.byref(error_code))
        else:
            logger.warning("key tone could not be written (error %d)", error_code.value)

        library.pa_simple_free(stream)
