"""The oracle, and the agreement it exists to establish.

The pure half runs everywhere. The half that decodes needs a real ffmpeg and is
marked, so on a machine without one these skip rather than pretending.
"""

from __future__ import annotations

import array
import math
import shutil
import struct

import pytest

from ripdoctor.audio import astats, decode
from ripdoctor.audio.runner import FakeRunner, RealRunner
from ripdoctor.core.envelope import DB_MIN

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs a real ffmpeg"
)


def sine(hz: float, seconds: float, rate: int, amplitude: float) -> array.array:
    n = int(rate * seconds)
    peak = amplitude * (decode.FULL_SCALE - 1)
    return array.array(
        "h", [int(peak * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)]
    )


# ------------------------------------------------------- the pure half


def test_a_half_scale_sine_reads_its_own_rms() -> None:
    """RMS of a sine is its peak over root two: -9.03 dB at half scale."""
    got = decode.envelope_of_samples(sine(200.0, 1.0, 8000, 0.5), 8000, 0.05)
    assert len(got) == 20
    for v in got:
        assert v == pytest.approx(-9.03, abs=0.3)


def test_silence_reads_the_floor_rather_than_negative_infinity() -> None:
    got = decode.envelope_of_samples(array.array("h", [0] * 800), 8000, 0.05)
    assert got == [DB_MIN] * 2


def test_a_trailing_partial_window_is_dropped() -> None:
    """Measuring it short reads quieter than the audio is, which would put a
    false gap at the end of every side."""
    rate, window = 8000, 0.05
    full = decode.envelope_of_samples(sine(200.0, 1.0, rate, 0.5), rate, window)
    ragged = decode.envelope_of_samples(
        sine(200.0, 1.0, rate, 0.5)[: int(rate * 0.97)], rate, window
    )
    assert len(full) == 20 and len(ragged) == 19
    assert ragged[-1] == pytest.approx(full[-1], abs=0.1)


def test_too_little_audio_to_measure_yields_nothing() -> None:
    assert decode.envelope_of_samples(array.array("h", [1] * 10), 8000, 0.05) == []
    assert decode.envelope_of_samples(array.array("h", [1] * 800), 8000, 0.0) == []


def test_quiet_and_loud_windows_are_distinguished() -> None:
    rate = 8000
    samples = sine(200.0, 0.5, rate, 0.5) + array.array("h", [0] * (rate // 2))
    got = decode.envelope_of_samples(samples, rate, 0.05)
    assert got[0] > -12 and got[-1] == DB_MIN


# ------------------------------------------------------ the argv it builds


def test_the_oracle_decodes_to_one_channel_at_its_own_rate() -> None:
    fake = FakeRunner().expect("ffmpeg", stdout=struct.pack("<4h", 0, 1, 2, 3))
    decode.decode_mono(fake, "a.flac", rate=8000)
    argv = fake.argv_for("s16le")
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-ar") + 1] == "8000"


def test_the_band_filter_matches_the_one_the_fast_path_uses() -> None:
    """Comparing the band lane is only meaningful if both filter the same way."""
    fake = FakeRunner().expect("ffmpeg", stdout=b"")
    decode.decode_mono(fake, "a.flac", band=(astats.BAND_LO_HZ, astats.BAND_HI_HZ))
    argv = fake.argv_for("highpass")
    chain = argv[argv.index("-af") + 1]
    assert "highpass=f=1000" in chain and "lowpass=f=3000" in chain


def test_an_odd_trailing_byte_does_not_derail_the_decode() -> None:
    fake = FakeRunner().expect("ffmpeg", stdout=struct.pack("<2h", 5, -5) + b"\x01")
    assert list(decode.decode_mono(fake, "a.flac")) == [5, -5]


# ------------------------------------------------- the agreement itself


@needs_ffmpeg
def test_the_fast_path_and_the_oracle_agree(tmp_path) -> None:
    """The reason the oracle exists.

    ffmpeg builds the signal, both paths measure it, and the answers must match.
    If they ever diverge, one is wrong and this says which by construction.
    """
    runner = RealRunner()
    src = tmp_path / "tone.flac"
    runner.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=2000:duration=4:sample_rate=48000",
            "-af",
            "volume=0.5",
            str(src),
        ],
    ).require()

    fast = astats.lanes(runner, str(src), rate=48000)
    slow = decode.envelope(runner, str(src), window=0.05, rate=48000)

    n = min(len(fast.full), len(slow))
    assert n > 60, "too little measured to compare"
    worst = max(abs(fast.full.levels[i] - slow.levels[i]) for i in range(5, n - 5))
    assert worst < 0.6, f"the two paths disagree by {worst:.2f} dB"


@needs_ffmpeg
def test_the_two_paths_agree_on_the_band_lane_too(tmp_path) -> None:
    runner = RealRunner()
    src = tmp_path / "tone.flac"
    runner.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=2000:duration=4:sample_rate=48000",
            "-af",
            "volume=0.5",
            str(src),
        ],
    ).require()

    fast = astats.lanes(runner, str(src), rate=48000)
    slow = decode.envelope(
        runner,
        str(src),
        window=0.05,
        rate=48000,
        band=(astats.BAND_LO_HZ, astats.BAND_HI_HZ),
    )
    n = min(len(fast.band), len(slow))
    worst = max(abs(fast.band.levels[i] - slow.levels[i]) for i in range(5, n - 5))
    assert worst < 1.0, f"the band lanes disagree by {worst:.2f} dB"


def test_the_agreement_tests_are_marked_not_silently_absent() -> None:
    """A skipped check that nobody knows is skipped is not a check.

    This is the guard against the marked tests quietly disappearing: they exist,
    they carry the marker, and CI runs them where ffmpeg is installed.
    """
    import pathlib

    text = pathlib.Path(__file__).read_text()
    assert text.count("@needs_ffmpeg") >= 2
    assert (
        "needs_ffmpeg"
        in (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
    )
