"""Gap detection: the anchoring rules, and what happens when they are inverted.

Two of these tests exist to demonstrate failures rather than successes. The
anchoring choice is the part of this algorithm that is easy to get wrong and
produces confident nonsense when it is wrong, so the wrong answers are pinned
down as tightly as the right ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import Envelope, Lanes, decode

FIXTURES = Path(__file__).parent / "fixtures" / "sides"


def build(segments: list[tuple[float, float]], window: float = 0.05) -> Envelope:
    """An envelope from (seconds, dB) segments."""
    levels: list[float] = []
    for secs, db in segments:
        levels.extend([db] * round(secs / window))
    return Envelope(tuple(levels), window)


def real_lanes() -> list[tuple[str, Lanes]]:
    return [(p.name, decode(p.read_bytes())) for p in sorted(FIXTURES.glob("*.env"))]


# --------------------------------------------------------------- basics


def test_finds_a_sustained_quiet_run() -> None:
    env = build([(30, -25), (4, -70), (30, -25)])
    found = G.find(env, below=G.BELOW)
    assert len(found) == 1
    assert found.gaps[0].lo == pytest.approx(30.0, abs=0.1)
    assert found.gaps[0].hi == pytest.approx(33.95, abs=0.1)


def test_a_run_shorter_than_mingap_is_not_a_gap() -> None:
    """A brief quiet moment inside a song is not an inter-track gap."""
    assert len(G.find(build([(30, -25), (0.5, -70), (30, -25)]), below=G.BELOW)) == 0
    assert len(G.find(build([(30, -25), (2.0, -70), (30, -25)]), below=G.BELOW)) == 1


def test_a_gap_running_to_the_end_of_the_side_is_still_found() -> None:
    """The run-out is a gap, and it is the one that ends the last track."""
    found = G.find(build([(30, -25), (10, -70)]), below=G.BELOW)
    assert len(found) == 1
    assert found.gaps[0].hi == pytest.approx(39.95, abs=0.1)


def test_the_lane_anchor_must_be_chosen_explicitly() -> None:
    """A default would be silently wrong for one of the two lanes."""
    env = build([(10, -25)])
    with pytest.raises(ValueError, match="exactly one"):
        G.find(env)
    with pytest.raises(ValueError, match="exactly one"):
        G.find(env, below=16.0, above=12.0)


def test_an_empty_envelope_yields_no_gaps_rather_than_raising() -> None:
    assert len(G.find(Envelope((), 0.05), below=G.BELOW)) == 0


# ------------------------------------------------- the anchoring rules


def test_the_full_lane_anchored_the_band_lane_way_misses_the_real_gap() -> None:
    """The inversion, in the other direction.

    A full-lane side has three levels: needle-up electrical silence around -95,
    an inter-track gap at -60, and music at -25. Anchored down from the music at
    -41, the real gap is found. Anchored up from the floor at -83 - which is
    correct for the band lane - the inter-track gap is nowhere near quiet enough
    and only the electrical silence qualifies.
    """
    env = build([(5, -95), (30, -25), (4, -60), (30, -25)])

    right = G.find(env, below=G.BELOW)
    assert right.threshold == pytest.approx(-41.0, abs=0.5)
    assert any(g.mean < -55 and g.lo > 10 for g in right), "the -60 dB gap is found"

    wrong = G.find(env, above=G.ABOVE)
    assert wrong.threshold == pytest.approx(-83.0, abs=0.5)
    assert not any(g.lo > 10 for g in wrong), (
        "anchored up from the floor, the real inter-track gap is missed entirely"
    )


def test_the_band_lane_anchored_the_full_lane_way_finds_dozens_of_false_gaps() -> None:
    """The inverted-anchor failure, reproduced.

    The band lane's range is much wider, so music-minus-16 lands among the quiet
    music rather than below it, and every soft passage becomes a "gap".
    """
    # A band-lane side: music -35, quiet passages -55, true gaps -85.
    segments: list[tuple[float, float]] = []
    for _ in range(12):
        segments += [(20, -35), (3, -55), (20, -35), (4, -85)]
    env = build(segments)

    wrong = G.find(env, below=G.BELOW)  # music - 16 = -51: catches quiet music
    right = G.find(env, above=G.ABOVE)  # floor + 12 = -73: catches only silence

    assert len(right) == 12, "the true inter-track gaps"
    assert [round(g.mean) for g in right] == [-85] * 12, "silence only"

    # The count roughly doubles, but the damage is what got added: twelve quiet
    # musical passages are now offered as places to cut a track.
    assert len(wrong) == 24
    assert sorted(round(g.mean) for g in wrong) == [-85] * 12 + [-55] * 12


def test_the_reported_levels_explain_the_answer() -> None:
    """A gap list nobody can argue with is a gap list nobody can debug."""
    found = G.find(build([(30, -26), (4, -70), (30, -26)]), below=G.BELOW)
    assert found.music == pytest.approx(-26.0, abs=0.5)
    assert found.threshold == pytest.approx(-42.0, abs=0.5)
    assert found.gaps[0].mean == pytest.approx(-70.0, abs=1.0)


# --------------------------------------------------------------- refine


def test_refine_keeps_a_fade_that_the_side_wide_threshold_would_cut() -> None:
    """The three-to-four-second error, reproduced and fixed.

    A tail decaying from -30 to -55 is still music, but it drops under the
    side-wide threshold early. Judged against the gap's own floor it survives.
    """
    fade = [(0.5, db) for db in (-30, -35, -40, -45, -50, -55)]  # 3 s of tail
    env = build([(30, -25), *fade, (6, -78), (30, -25)])

    found = G.find(env, below=G.BELOW)
    assert len(found) == 1
    gap = found.gaps[0]

    music_end, music_start = G.refine(env, gap.lo, gap.hi)
    assert music_end > gap.lo, "the side-wide edge truncates the fade"
    assert music_end == pytest.approx(32.5, abs=0.6), "the tail is kept"
    assert music_start >= music_end


def test_refine_on_a_degenerate_gap_returns_its_bounds() -> None:
    env = build([(10, -25)])
    assert G.refine(env, 5.0, 5.0) == (5.0, 5.0)
    assert G.refine(env, 8.0, 2.0) == (8.0, 2.0)
    assert G.refine(Envelope((), 0.05), 1.0, 2.0) == (1.0, 2.0)


# --------------------------------------------------------------- verdict


def test_verdict_reports_the_margin_inside_a_gap() -> None:
    found = G.find(build([(30, -25), (6, -70), (30, -25)]), below=G.BELOW)
    v = G.verdict(33.0, found)
    assert v.inside and v.margin == pytest.approx(3.0, abs=0.2)
    assert "in gap" in str(v) and "margin" in str(v)


def test_verdict_reports_the_distance_outside_one() -> None:
    found = G.find(build([(30, -25), (6, -70), (30, -25)]), below=G.BELOW)
    v = G.verdict(20.0, found)
    assert not v.inside
    assert v.distance == pytest.approx(10.0, abs=0.2)
    assert str(v).startswith("NOT in a gap")


def test_verdict_with_no_gaps_says_so_rather_than_crashing() -> None:
    v = G.verdict(10.0, G.find(build([(30, -25)]), below=G.BELOW))
    assert not v.inside and v.lo is None
    assert "no gaps" in str(v)


# --------------------------------------------------------- real vinyl


def test_real_sides_yield_a_plausible_number_of_gaps() -> None:
    """Detection has to survive contact with actual records.

    A side holds a handful of tracks, so a handful of inter-track gaps. Hundreds
    means the threshold is inside the music; zero means it is below the floor.
    Both are the failures this bounds.
    """
    checked = 0
    for name, lanes in real_lanes():
        if len(lanes.band) < 1000:
            continue  # a partial re-rip, not a full side
        found = G.find(lanes.band, above=G.ABOVE)
        assert 0 < len(found) < 60, f"{name}: {len(found)} gaps in the band lane"
        checked += 1
    assert checked >= 20, "no sides were examined; the pattern has drifted"


def test_the_band_lane_is_the_more_selective_detector_on_real_sides() -> None:
    """On real records the band lane finds fewer, cleaner gaps than the full one.

    This is the practical consequence of the separation claim: it is not that
    the band lane finds more, it is that it finds the right ones.
    """
    band_tighter = examined = 0
    for _name, lanes in real_lanes():
        if len(lanes.band) < 1000:
            continue
        full = G.find(lanes.full, below=G.BELOW)
        band = G.find(lanes.band, above=G.ABOVE)
        if len(band) <= len(full):
            band_tighter += 1
        examined += 1

    assert examined >= 20, "no sides were examined; the pattern has drifted"
    assert band_tighter > examined * 0.6, (
        f"the band lane was the tighter detector on only {band_tighter} of "
        f"{examined} sides"
    )


def test_detected_gaps_are_ordered_and_disjoint_on_real_sides() -> None:
    checked = 0
    for name, lanes in real_lanes():
        found = G.find(lanes.band, above=G.ABOVE)
        prev = None
        for g in found:
            assert g.hi >= g.lo, f"{name}: inverted gap"
            assert g.length >= G.MINGAP - 1e-9, f"{name}: gap shorter than MINGAP"
            if prev is not None:
                assert g.lo > prev, f"{name}: gaps overlap or are unordered"
            prev = g.hi
        checked += 1
    assert checked >= 20, "no sides were examined; the pattern has drifted"


def test_a_cut_in_a_detected_gap_is_judged_inside_it() -> None:
    """The round trip the ear check depends on."""
    checked = 0
    for name, lanes in real_lanes():
        found = G.find(lanes.band, above=G.ABOVE)
        for g in found.gaps[:5]:
            v = G.verdict((g.lo + g.hi) / 2.0, found)
            assert v.inside, f"{name}: midpoint of a gap judged outside it"
            assert v.margin is not None and v.margin > 0
            checked += 1
    assert checked >= 40, "too few gaps examined; the pattern has drifted"


def test_side_starts_land_where_the_manifest_says_the_side_is_long_enough() -> None:
    """Every real side has quiet at the start: the lead-in groove.

    This is also the region where the needle drop lives, which is why a side's
    loudest sample is so often not music.
    """
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    with_lead_in = checked = 0
    for name, lanes in real_lanes():
        if manifest[name]["duration_s"] < 300:
            continue
        found = G.find(lanes.band, above=G.ABOVE)
        if found.gaps and found.gaps[0].lo < 30.0:
            with_lead_in += 1
        checked += 1
    assert checked >= 15, "no sides were examined; the pattern has drifted"
    assert with_lead_in > checked * 0.5, "most sides should open with a quiet lead-in"
