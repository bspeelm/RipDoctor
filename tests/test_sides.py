"""Side layout: finding the music, and deciding what is on which side."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from ripdoctor.core import sides as S
from ripdoctor.core.envelope import Envelope, decode

FIXTURES = Path(__file__).parent / "fixtures" / "sides"


def build(segments: list[tuple[float, float]], window: float = 0.05) -> Envelope:
    levels: list[float] = []
    for secs, db in segments:
        levels.extend([db] * round(secs / window))
    return Envelope(tuple(levels), window)


# --------------------------------------------------------------- music span


def test_the_span_excludes_run_in_and_run_out_groove() -> None:
    env = build([(12, -80), (300, -25), (18, -80)])
    start, end = S.music_span(env)
    assert start == pytest.approx(12.0, abs=0.2)
    assert end == pytest.approx(312.0, abs=0.2)


def test_both_ends_are_measured_the_same_way() -> None:
    """A side with equal silence at both ends is bracketed symmetrically."""
    env = build([(20, -80), (200, -25), (20, -80)])
    start, end = S.music_span(env)
    assert start == pytest.approx(env.duration - end, abs=0.2)


def test_a_needle_drop_that_skids_is_not_the_start_of_the_side() -> None:
    """A drop that catches a groove and is lifted leaves a short burst of audio.

    Anchoring on 'first audio' reads that accident as track one. Requiring a
    sustained run steps over it.
    """
    env = build(
        [
            (8, -80),
            (0.9, -22),  # the skid: loud, and shorter than SPAN_RUN
            (2, -80),  # arm lifted
            (300, -25),  # the real side
            (10, -80),
        ]
    )
    start, _ = S.music_span(env)
    assert start > 9.0, "the skid was taken for the start of the music"
    assert start == pytest.approx(10.9, abs=0.3)


def test_arm_handling_does_not_move_the_start_on_the_band_lane() -> None:
    """Full band, handling rumble is louder than quiet music; in band it is not.

    The band lane is what makes this work, so the test states the band-lane case:
    rumble well below the music level is stepped over even though it is long.
    """
    env = build([(6, -95), (4, -67), (300, -35), (10, -95)])  # -67 = handling
    start, _ = S.music_span(env)
    assert start == pytest.approx(10.0, abs=0.3), "handling noise is not music"


def test_a_silent_side_returns_the_whole_side_rather_than_guessing() -> None:
    assert S.music_span(build([(30, -95)])) == (0.0, 30.0)


def test_an_empty_envelope_has_an_empty_span() -> None:
    assert S.music_span(Envelope((), 0.05)) == (0.0, 0.0)


def test_every_real_side_yields_a_span_inside_itself() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    checked = 0
    for path in sorted(FIXTURES.glob("*.env")):
        lanes = decode(path.read_bytes())
        start, end = S.music_span(lanes.band)
        dur = manifest[path.name]["duration_s"]
        assert 0 <= start < end <= dur + 0.1, f"{path.name}: span {start}-{end}"
        assert end - start > 0.5 * dur, (
            f"{path.name}: only {end - start:.0f}s of music in a {dur:.0f}s side"
        )
        checked += 1
    assert checked >= 20, "no sides were examined; the pattern has drifted"


def test_real_sides_open_with_run_in_groove() -> None:
    """Every record has some. A span starting at zero means the gate failed."""
    at_zero = checked = 0
    for path in sorted(FIXTURES.glob("*.env")):
        lanes = decode(path.read_bytes())
        if len(lanes.band) < 1000:
            continue
        start, _ = S.music_span(lanes.band)
        if start <= 0.05:
            at_zero += 1
        checked += 1
    assert checked >= 15, "no sides were examined; the pattern has drifted"
    assert at_zero <= checked * 0.2, f"{at_zero} of {checked} sides start at zero"


# ------------------------------------------------------------ side assignment


def test_tracks_are_split_where_the_arithmetic_agrees() -> None:
    """The sum a person does by hand: side A held 1061 s, tracks 1-6 total 1055."""
    lengths = [200.0, 180.0, 240.0, 190.0, 210.0, 220.0]
    cuts = S.assign_sides([620.0, 620.0], lengths)
    assert cuts == [(0, 3), (3, 6)]


def test_the_last_side_takes_every_remaining_track() -> None:
    cuts = S.assign_sides([100.0, 100.0], [90.0, 95.0, 30.0, 20.0])
    assert cuts[-1][1] == 4, "nothing is left unassigned"
    assert [lo for lo, _ in cuts] == [0, cuts[0][1]]


def test_splits_are_contiguous_and_ordered() -> None:
    lengths = [60.0] * 12
    cuts = S.assign_sides([180.0, 180.0, 180.0, 180.0], lengths)
    assert cuts[0][0] == 0
    for (_, a), (b, _) in pairwise(cuts):
        assert a == b, "sides abut with no gap and no overlap"
    assert cuts[-1][1] == len(lengths)


def test_the_exhaustive_search_beats_a_greedy_one() -> None:
    """A greedy split commits early and cannot recover.

    Side A measures 100 s. Greedily, tracks of 60 + 45 = 105 looks closer than
    60 alone, so a greedy walk takes both and leaves side B 40 s short. Choosing
    both splits together costs less overall.
    """
    lengths = [60.0, 45.0, 95.0]
    cuts = S.assign_sides([100.0, 140.0], lengths)
    assert cuts == [(0, 1), (1, 3)]


def test_degenerate_inputs_return_nothing_rather_than_raising() -> None:
    assert S.assign_sides([], [10.0]) == []
    assert S.assign_sides([10.0], []) == []


def test_missing_catalogue_durations_are_treated_as_zero() -> None:
    """MusicBrainz vinyl entries are frequently bulk clones with no lengths."""
    cuts = S.assign_sides([100.0, 100.0], [100.0, 0.0, 100.0])
    assert cuts[-1][1] == 3


def test_the_mismatch_per_side_is_reported() -> None:
    lengths = [200.0, 180.0, 240.0, 190.0, 210.0, 220.0]
    spans = [620.0, 620.0]
    cuts = S.assign_sides(spans, lengths)
    miss = S.side_mismatch(spans, lengths, cuts)
    assert len(miss) == 2
    assert all(abs(m) < 5.0 for m in miss), "a clean split is close on both sides"


def test_a_side_out_by_a_whole_track_shows_up_in_the_mismatch() -> None:
    """The signal that the release is wrong, rather than the detection."""
    lengths = [200.0, 200.0, 200.0]
    spans = [200.0, 200.0]  # a side is missing from the measurement
    cuts = S.assign_sides(spans, lengths)
    assert max(abs(m) for m in S.side_mismatch(spans, lengths, cuts)) >= 190.0
