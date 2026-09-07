"""The live meter: sample widths, band placement, and the scaling traps.

Three of these correspond to mistakes that produce confident, wrong readings
rather than errors: a 24-bit capture measured against a 16-bit full scale, a
band measured with a bin width from the wrong sample rate, and power summed
across bins corrected by the wrong property of the window.
"""

from __future__ import annotations

import math
import struct

import pytest

from ripdoctor.core import meter as M


def tone(
    hz: float,
    rate: int,
    frames: int,
    width: int,
    channels: int = 2,
    amplitude: float = 0.5,
) -> bytes:
    """Interleaved PCM of a sine at `hz`, at `amplitude` of full scale."""
    full = (1 << (width * 8 - 1)) - 1
    out = bytearray()
    for i in range(frames):
        v = int(amplitude * full * math.sin(2 * math.pi * hz * i / rate))
        for _ in range(channels):
            if width == 2:
                out += struct.pack("<h", v)
            elif width == 4:
                out += struct.pack("<i", v)
            else:
                out += (v & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


def silence(frames: int, width: int, channels: int = 2) -> bytes:
    return bytes(frames * channels * width)


# ---------------------------------------------------------------- samples


@pytest.mark.parametrize("width", [2, 3, 4])
def test_sample_widths_round_trip_through_the_unpacker(width: int) -> None:
    full = (1 << (width * 8 - 1)) - 1
    wanted = [0, 1, -1, full, -full, full // 3, -(full // 7)]
    raw = bytearray()
    for v in wanted:
        if width == 2:
            raw += struct.pack("<h", v)
        elif width == 4:
            raw += struct.pack("<i", v)
        else:
            raw += (v & 0xFFFFFF).to_bytes(3, "little")
    assert M.samples(bytes(raw), width) == wanted


def test_twenty_four_bit_negatives_sign_extend() -> None:
    """The width with no struct member, assembled by hand."""
    assert M.samples((-1 & 0xFFFFFF).to_bytes(3, "little"), 3) == [-1]
    assert M.samples((-8388608 & 0xFFFFFF).to_bytes(3, "little"), 3) == [-8388608]
    assert M.samples((8388607).to_bytes(3, "little"), 3) == [8388607]


def test_an_unsupported_width_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported sample width"):
        M.samples(b"\x00" * 8, 1)


# -------------------------------------------------------------------- fft


def test_the_fft_finds_a_tone_in_the_right_bin() -> None:
    n, rate, hz = 1024, 48000, 3000.0
    spec = M.fft(
        [complex(math.sin(2 * math.pi * hz * i / rate), 0.0) for i in range(n)]
    )
    loudest = max(range(1, n // 2), key=lambda k: abs(spec[k]))
    assert loudest == pytest.approx(hz / (rate / n), abs=1)


def test_the_fft_of_silence_is_silent() -> None:
    assert all(abs(v) < 1e-9 for v in M.fft([complex(0.0, 0.0)] * 256))


# ----------------------------------------------------------------- levels


def test_a_block_too_short_to_measure_returns_nothing() -> None:
    """Better than a confident wrong number."""
    assert M.levels_of(silence(8, 2), 48000, 2, 2) is None


def test_silence_reads_at_the_floor() -> None:
    lv = M.levels_of(silence(M.BLOCK, 2), 48000, 2, 2)
    assert lv is not None
    assert lv.full == M.FLOOR_DB and lv.band == M.FLOOR_DB
    assert lv.peak == M.FLOOR_DB


def test_a_half_scale_tone_reads_about_minus_six_dbfs_peak() -> None:
    raw = tone(1000.0, 48000, M.BLOCK, 2, amplitude=0.5)
    lv = M.levels_of(raw, 48000, 2, 2)
    assert lv is not None
    assert lv.peak == pytest.approx(-6.0, abs=0.3)
    assert lv.full == pytest.approx(-9.0, abs=0.5)  # RMS of a sine is peak/sqrt2


@pytest.mark.parametrize("width", [2, 3, 4])
def test_the_same_signal_reads_the_same_at_every_sample_width(width: int) -> None:
    """The 48 dB trap.

    Full scale follows the sample width. Measure a 24-bit capture against a
    16-bit full scale and every reading is 48 dB hot - which looks like a very
    loud record rather than like a bug.
    """
    raw = tone(1000.0, 48000, M.BLOCK, width, amplitude=0.25)
    lv = M.levels_of(raw, 48000, 2, width)
    assert lv is not None
    assert lv.peak == pytest.approx(-12.0, abs=0.4)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_the_band_measures_1_to_3_khz_at_every_rate(rate: int) -> None:
    """The bin-width trap.

    Bin width is rate over block size. Hardcode 48000 and at 96 kHz the band
    reads 2-6 kHz while still calling itself 1-3.
    """
    inside = M.levels_of(tone(2000.0, rate, M.BLOCK, 2), rate, 2, 2)
    outside = M.levels_of(tone(8000.0, rate, M.BLOCK, 2), rate, 2, 2)
    assert inside is not None and outside is not None

    assert inside.band > outside.band + 30, (
        f"at {rate} Hz the band did not separate 2 kHz from 8 kHz"
    )
    # Full-band level is the same for both; only the lane differs.
    assert inside.full == pytest.approx(outside.full, abs=1.0)


def test_a_tone_just_outside_the_band_is_rejected() -> None:
    rate = 48000
    inside = M.levels_of(tone(2000.0, rate, M.BLOCK, 2), rate, 2, 2)
    below = M.levels_of(tone(300.0, rate, M.BLOCK, 2), rate, 2, 2)
    assert inside is not None and below is not None
    assert inside.band > below.band + 25, "bass leaked into the band lane"


def test_the_band_figure_is_comparable_with_a_full_band_reading() -> None:
    """The window-normalisation trap, and the check that catches it.

    For a tone wholly inside the band, the band reading must equal the full-band
    reading of the same signal - all the energy is in the band. That is only
    true when power summed across bins is corrected by the window's mean square
    (0.375 for Hann) rather than its coherent gain squared (0.25). The wrong one
    reads 1.76 dB hot at every level, and nothing else in the suite notices.
    """
    raw = tone(2000.0, 48000, M.BLOCK, 2, amplitude=0.5)
    lv = M.levels_of(raw, 48000, 2, 2)
    assert lv is not None
    assert lv.band == pytest.approx(lv.full, abs=0.2)
    assert lv.full == pytest.approx(-9.03, abs=0.2)


def test_peak_is_never_below_the_full_band_reading() -> None:
    """Arithmetic: peak is a maximum, full is an average over the same block."""
    for hz in (200.0, 1500.0, 5000.0):
        lv = M.levels_of(tone(hz, 48000, M.BLOCK, 3), 48000, 2, 3)
        assert lv is not None
        assert lv.peak >= lv.full - 0.1, f"{hz} Hz: peak {lv.peak} < full {lv.full}"


def test_mono_and_stereo_of_the_same_tone_agree() -> None:
    a = M.levels_of(tone(1500.0, 48000, M.BLOCK, 2, channels=1), 48000, 1, 2)
    b = M.levels_of(tone(1500.0, 48000, M.BLOCK, 2, channels=2), 48000, 2, 2)
    assert a is not None and b is not None
    assert a.full == pytest.approx(b.full, abs=0.2)
    assert a.band == pytest.approx(b.band, abs=0.2)


def test_a_nonsense_rate_or_channel_count_is_refused() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        M.levels_of(silence(M.BLOCK, 2), 0, 2, 2)
    with pytest.raises(ValueError, match="must be positive"):
        M.levels_of(silence(M.BLOCK, 2), 48000, 0, 2)


def test_the_four_states_the_meter_exists_to_show() -> None:
    """Music, handling, groove and dead air separate in the band lane.

    This is the reading the meter is for: what the stylus is doing, before
    twenty minutes have been committed to.
    """
    rate = 48000

    def band_at(amplitude: float, hz: float) -> float:
        lv = M.levels_of(tone(hz, rate, M.BLOCK, 3, amplitude=amplitude), rate, 2, 3)
        assert lv is not None
        return lv.band

    music = band_at(0.25, 2000.0)  # in band, loud
    handling = band_at(0.25, 60.0)  # loud, but bass rumble
    groove = band_at(0.001, 2000.0)  # in band, very quiet

    assert music > handling + 20, "handling rumble should not read as music"
    assert music > groove + 30, "a silent groove should not read as music"
