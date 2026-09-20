"""Tests for the DTMF tone synthesis."""

from array import array

import pytest

from bt_handsfree_kde.dtmf import (
    INT16_FULL_SCALE,
    SAMPLE_RATE_HZ,
    TONE_AMPLITUDE,
    TONE_DURATION_MS,
    render_tone,
)


def test_tone_has_the_expected_length_and_stays_within_amplitude() -> None:
    """A tone lasts `TONE_DURATION_MS` and its peak stays below twice the amplitude."""
    pcm = render_tone("5")
    samples = array("h", pcm)

    expected_sample_count = SAMPLE_RATE_HZ * TONE_DURATION_MS // 1000
    assert len(samples) == expected_sample_count
    peak = max(abs(sample) for sample in samples)
    assert peak <= 2 * TONE_AMPLITUDE * INT16_FULL_SCALE
    assert samples[0] == 0


def test_unknown_key_is_rejected() -> None:
    """Only the twelve dialpad keys have a tone."""
    with pytest.raises(KeyError):
        render_tone("A")
