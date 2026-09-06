"""Envelope behaviour, and the claim the project rests on.

The fixtures under tests/fixtures/sides are real envelopes measured from real
records, with the titles removed - they are derived measurements, not audio.
Testing against them catches things synthetic signals never would, because the
awkward cases on a record are not the ones anybody thinks to synthesise.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from ripdoctor.core import envelope as env

FIXTURES = Path(__file__).parent / "fixtures" / "sides"


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*.env"))


def load(path: Path) -> env.Lanes:
    return env.decode(path.read_bytes())


# --------------------------------------------------------------- quantiser


@pytest.mark.parametrize("db", [0.0, -0.5, -13.25, -43.0, -85.8, -120.0, -127.5])
def test_quantiser_round_trips_to_within_half_a_step(db: float) -> None:
    assert abs(env.deq_db(env.q_db(db)) - db) <= env.DB_STEP / 2


@pytest.mark.parametrize("bad", [None, float("nan"), float("-inf")])
def test_unmeasurable_readings_collapse_to_the_floor(bad: float | None) -> None:
    """ffmpeg emits all three of these, and astats prints `-nan` outright."""
    assert env.deq_db(env.q_db(bad)) == env.DB_MIN


def test_quantiser_clamps_rather_than_wrapping() -> None:
    assert env.q_db(40.0) == env.q_db(0.0)
    assert env.q_db(-400.0) == env.q_db(env.DB_MIN)


# --------------------------------------------------------------- the format


def test_round_trip_through_the_cache_format() -> None:
    lanes = env.from_readings(
        [-20.0, -43.0, -85.0], [-10.0, -30.0, -70.0], [-40.0, -73.0, -95.0], 0.05
    )
    out = env.decode(env.encode(lanes))
    for before, after in zip(lanes, out, strict=True):
        assert after.window == before.window
        for a, b in zip(before.levels, after.levels, strict=True):
            assert abs(a - b) <= env.DB_STEP / 2


def test_a_foreign_blob_is_refused_not_guessed_at() -> None:
    with pytest.raises(env.FormatError):
        env.decode(b"RIFF____WAVEfmt ")


def test_a_truncated_envelope_is_refused() -> None:
    """Half a file read as decibels produces plausible-looking numbers."""
    good = env.encode(env.from_readings([-20.0] * 8, [-10.0] * 8, [-40.0] * 8, 0.05))
    with pytest.raises(env.FormatError, match="bytes"):
        env.decode(good[: len(good) - 4])


def test_a_future_version_is_refused() -> None:
    good = bytearray(env.encode(env.from_readings([-20.0], [-10.0], [-40.0], 0.05)))
    good[4:8] = (env.VERSION + 1).to_bytes(4, "little")
    with pytest.raises(env.FormatError, match="version"):
        env.decode(bytes(good))


def test_an_empty_envelope_is_not_written() -> None:
    with pytest.raises(env.FormatError):
        env.encode(env.Lanes(*(env.Envelope((), 0.05) for _ in range(3))))


# --------------------------------------------- the window-count guard


@pytest.mark.parametrize(
    ("windows", "duration", "ok"),
    [
        (26502, 1325.056, True),  # a real side, measured
        (24000, 1200.0, True),  # exact
        (24017, 1200.0, True),  # 0.072%: the worst real deviation observed
        (22056, 1200.0, False),  # 0.919: 44.1 kHz envelope, 48 kHz audio
        (21600, 1200.0, False),  # 0.90: the predecessor's lower boundary
        (26400, 1200.0, False),  # 1.10: the predecessor's upper boundary
        (12000, 1200.0, False),  # half - a 96 kHz/48 kHz confusion
        (24000, 0.0, False),  # no duration to compare against
    ],
)
def test_window_count_guard(windows: int, duration: float, ok: bool) -> None:
    """The 0.919 case is what this exists to catch.

    An envelope measured at one sample rate against audio at another lines up
    almost well enough to look right. The predecessor's ten per cent tolerance
    accepted it. Two per cent does not, and still clears every real side by a
    wide margin.
    """
    assert env.window_count_is_plausible(windows, duration, 0.05) is ok


def test_the_tolerance_clears_every_real_side() -> None:
    """The margin is measured, not chosen.

    If a future change tightened the tolerance past what real captures produce,
    this fails before anybody's envelope is rejected.
    """
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    worst, checked = 0.0, 0
    for m in manifest.values():
        if not m["duration_s"]:
            continue
        expected = m["duration_s"] / (m["window_ms"] / 1000.0)
        worst = max(worst, abs(m["windows"] / expected - 1.0))
        checked += 1

    assert checked >= 20, "no sides were examined; the pattern has drifted"
    assert worst < env.WINDOW_COUNT_SLACK / 4, (
        f"real sides deviate by up to {worst:.4%}, which is close to the "
        f"{env.WINDOW_COUNT_SLACK:.0%} tolerance; the margin has eroded"
    )


# --------------------------------------------------------------- timebase


def test_rescaling_corrects_the_resampler_drift() -> None:
    """ffmpeg's resampler runs ~0.25% long; uncorrected that is ~3 s per side."""
    true_duration = 1320.0
    measured = env.Envelope(tuple([-30.0] * int(1320 * 1.0025 / 0.05)), 0.05)

    drift = measured.duration - true_duration
    assert 3.0 < drift < 3.7, "the case this guards against"

    fixed = measured.rescaled(true_duration)
    assert abs(fixed.duration - true_duration) < 1e-6
    assert fixed.levels == measured.levels  # readings do not move; spacing does


def test_rescaling_a_degenerate_envelope_is_a_no_op() -> None:
    e = env.Envelope((-30.0,), 0.05)
    assert e.rescaled(0.0) is e
    assert env.Envelope((), 0.05).rescaled(10.0).levels == ()


# --------------------------------------------------------------- indexing


def test_indexing_is_clamped_at_both_ends() -> None:
    e = env.Envelope(tuple(float(-i) for i in range(10)), 0.05)
    assert e.at(-5.0) == e.levels[0]
    assert e.at(1e6) == e.levels[-1]
    assert e.between(0.10, 0.05) == ()


def test_percentile_anchors_on_the_music_not_the_floor() -> None:
    """Three floors exist on one side; anchoring low finds whichever is lowest.

    Electrical noise, lead-in groove and inter-track gap are tens of decibels
    apart. The 85th percentile lands in the music regardless of which floor a
    given pressing happens to have.
    """
    quiet = [-90.0] * 200 + [-60.0] * 200 + [-52.0] * 200
    music = [-25.0] * 400
    e = env.Envelope(tuple(quiet + music), 0.05)
    assert e.percentile(0.85) > -30.0
    assert e.percentile(0.02) < -80.0

    with pytest.raises(ValueError):
        env.Envelope((), 0.05).percentile(0.85)


# --------------------------------------------------------- real vinyl


def test_every_fixture_decodes_and_matches_its_manifest() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    files = fixture_files()
    assert len(files) >= 20, "the fixture set has shrunk; the pattern has drifted"

    for path in files:
        lanes = load(path)
        declared = manifest[path.name]
        assert len(lanes.full) == declared["windows"]
        assert abs(lanes.window - declared["window_ms"] / 1000.0) < 1e-9
        assert len(lanes.peak) == len(lanes.band) == len(lanes.full)
        assert env.window_count_is_plausible(
            len(lanes.full), declared["duration_s"], lanes.window
        ), f"{path.name} does not line up with its own audio"


def test_peak_below_rms_is_rare_enough_to_be_corruption() -> None:
    """Peak cannot really be quieter than RMS. Occasionally the data says it is.

    Peak is the largest sample in a window and RMS is an average over the same
    window, so the inequality is arithmetic. Across 29 real sides it is violated
    in exactly four places - one single window per affected side, at magnitudes
    from 1 to 49 dB. One isolated window is the signature of the interleaved
    astats output the predecessor already guards against when parsing, not of a
    lane written in the wrong order, which would fail everywhere at once.

    So this asserts the rate rather than the absolute. If the lanes were ever
    swapped, or the parser regressed, the proportion would jump and this fails.
    Repairing the individual windows belongs to the layer that measures them.
    """
    bad = total = checked = 0
    for path in fixture_files():
        lanes = load(path)
        for full, peak in zip(lanes.full.levels, lanes.peak.levels, strict=True):
            # Both saturate at the same floor where nothing was measurable.
            if full <= env.DB_MIN or peak <= env.DB_MIN:
                continue
            total += 1
            if peak < full - env.DB_STEP:
                bad += 1
        checked += 1

    assert checked >= 20, "no sides were examined; the pattern has drifted"
    assert total > 100_000, "too few readings to judge a rate"
    assert bad / total < 0.0001, (
        f"{bad} of {total} windows have peak below RMS ({bad / total:.4%}). "
        "At this rate it is no longer isolated corruption - suspect the lane "
        "order or the astats parser."
    )


def test_the_band_lane_separates_what_the_full_lane_cannot() -> None:
    """The claim this project exists for, measured on real records.

    On a sparse pressing an inter-track gap and a quiet musical passage sit at
    the same full-band level, so no threshold can divide them. Restricted to
    1-3 kHz the same two things separate, because vinyl's noise is bass-heavy
    and a quiet pressing has nothing above 8 kHz.

    The test does not assume which windows are gaps. It takes the quietest
    tenth of the side - which on any record is a mixture of real gaps and quiet
    music - and asks how far that mixture sits below the music in each lane.
    If the band lane did not separate them, the two spreads would match.
    """
    wins = ties = 0
    for path in fixture_files():
        lanes = load(path)
        if len(lanes.full) < 1000:
            continue

        full_spread = lanes.full.percentile(0.85) - lanes.full.percentile(0.10)
        band_spread = lanes.band.percentile(0.85) - lanes.band.percentile(0.10)

        assert full_spread > 0 and band_spread > 0, f"{path.name}: degenerate lane"
        if band_spread > full_spread:
            wins += 1
        else:
            ties += 1

    examined = wins + ties
    assert examined >= 20, "no sides were examined; the pattern has drifted"
    # Not every side is sparse - a loud, dense record separates fine full-band.
    # The claim is that the band lane is the better instrument in general.
    assert wins > examined * 0.7, (
        f"the band lane out-separated the full lane on only {wins} of {examined} "
        "sides; the premise of the project is in question"
    )


def test_real_sides_span_both_signal_chains() -> None:
    """The fixtures cover two different converters, not one.

    Detection thresholds were originally measured on a 16-bit/48 kHz chain and
    the current one records 24-bit/96 kHz. Having both in the fixture set is
    what makes it possible to ask whether a threshold generalises at all.
    """
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    durations = [m["duration_s"] for m in manifest.values() if m["duration_s"]]
    assert len(durations) >= 20
    assert not any(math.isnan(d) for d in durations)
    assert max(durations) < 40 * 60, "a side over forty minutes is not a side"

    # A "side" is not always a side. The shortest fixture here is 33 seconds:
    # a single-track re-rip, kept as its own side because the first capture of
    # that track was cut short. Anything that assumes a side is twenty minutes
    # of music breaks on it, which is exactly why it is in the fixture set.
    assert min(durations) < 60, "the short partial re-rip has gone missing"
    assert sum(1 for d in durations if d > 600) >= 15, "too few full-length sides"
