"""The spec/plan contract, and the refusals that protect a cut.

The fixture pair is a real album's spec and the plan that was fitted from it,
with the titles replaced. Every one of its eleven tracks carries edges a person
set by ear, it has a manual override on two gaps, and one of its sides is a
single-track re-rip named "Orphan" rather than a letter - so it exercises the
three things a synthetic fixture would not think to include.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from ripdoctor.core import plan as P

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def load_spec() -> P.Spec:
    return P.Spec.from_dict(json.loads((FIXTURES / "ear-set.spec.json").read_text()))


def load_plan() -> P.Plan:
    return P.Plan.from_dict(json.loads((FIXTURES / "ear-set.plan.json").read_text()))


# --------------------------------------------------------------- the spec


def test_the_real_spec_loads_with_every_edge_intact() -> None:
    spec = load_spec()
    assert len(spec.sides) == 3
    tracks = [t for s in spec.sides for t in s.tracks]
    assert len(tracks) == 11
    assert all(t.has_ear_edges for t in tracks), "every track was set by ear"
    assert all(t.cat > 0 for t in tracks), "catalogue durations survive"


def test_a_side_is_not_always_a_letter() -> None:
    """A botched side re-ripped as one track is kept as its own side.

    Anything that assumes a, b, c, d - or one character - breaks here.
    """
    letters = [s.letter for s in load_spec().sides]
    assert "Orphan" in letters
    assert len(load_spec().sides[-1].tracks) == 1


def test_manual_gap_overrides_load_as_numbers() -> None:
    """`fix` is keyed by track number, but JSON object keys are strings."""
    fixes = {k: v for s in load_spec().sides for k, v in s.fix.items()}
    assert fixes, "the fixture has manual overrides"
    assert all(isinstance(k, int) for k in fixes)
    for music_end, next_start in fixes.values():
        assert next_start > music_end, "an override names a gap, not a point"


def test_lead_and_tail_default_when_a_spec_omits_them() -> None:
    spec = P.Spec.from_dict(
        {"slug": "s", "sides": [{"letter": "a", "start": 0.0, "end": 10.0}]}
    )
    assert spec.lead == P.LEAD and spec.tail == P.TAIL


# --------------------------------------------------------------- the plan


def test_the_real_plan_loads_and_validates() -> None:
    plan = load_plan()
    assert len(plan.sides) == 3
    assert sum(len(s.tracks) for s in plan.sides) == 11
    P.validate(plan)


def test_tracks_do_not_share_boundaries() -> None:
    """The reason this format replaced the last one.

    Each track owns its own start and end; the groove between them belongs to
    neither and is not written. On this record the gaps run several seconds, so
    a shared boundary would leave a track ending in blank groove.
    """
    plan = load_plan()
    shared = adjacent = 0
    for side in plan.sides:
        for a, b in pairwise(side.tracks):
            adjacent += 1
            if abs(b.start - a.end) < 1e-9:
                shared += 1
    assert adjacent >= 7, "too few adjacent pairs; the fixture has drifted"
    assert shared == 0, "no boundary is shared between two tracks"


def test_the_discarded_groove_is_what_is_left_after_padding() -> None:
    """Per-track edges do not throw the whole gap away - they give most of it back.

    A detected inter-track gap on this record runs several seconds. Each
    neighbour then keeps `tail` after its last note and `lead` before its first,
    so what is actually discarded is the gap minus about 2.8 s of padding. The
    measured remainder here is 0.15 to 2.3 s, which is the point: the groove that
    belongs to neither track, and nothing more.
    """
    plan = load_plan()
    gaps = [b.start - a.end for side in plan.sides for a, b in pairwise(side.tracks)]
    assert len(gaps) >= 7, "too few adjacent pairs; the fixture has drifted"
    assert min(gaps) > 0, "tracks never overlap"
    assert max(gaps) < P.LEAD + P.TAIL, (
        "more than the padding was discarded; a track has lost its lead-in"
    )


def test_deltas_are_small_and_a_consistent_bias_is_normal() -> None:
    """Arithmetic is the check, not the driver.

    A steady negative bias across a side is expected - quiet heads and tails
    fall below any threshold. An outlier is the bug signal.
    """
    plan = load_plan()
    deltas = [t.delta for s in plan.sides for t in s.tracks]
    assert max(abs(d) for d in deltas) < 15.0, "no track is wildly off catalogue"


def test_round_trip_through_a_dict_is_lossless() -> None:
    plan = load_plan()
    again = P.Plan.from_dict(plan.to_dict())
    assert again == plan


# ------------------------------------------------------------- refusals


def test_the_superseded_cuts_format_is_refused_not_converted() -> None:
    """Converting would have to guess which track owns the groove."""
    old = {
        "slug": "s",
        "sides": [{"file": "side-a.flac", "cuts": [12.1, 177.6, 361.2], "tracks": []}],
    }
    with pytest.raises(P.OldFormat, match="re-fit"):
        P.Plan.from_dict(old)


def build(tracks: list[tuple[int, float, float]], file: str = "side-a.flac") -> P.Plan:
    return P.Plan(
        slug="s",
        album="",
        artist="",
        sides=(
            P.PlanSide(
                file=file,
                tracks=tuple(
                    P.PlanTrack(number=n, title="", start=s, end=e, cat=e - s)
                    for n, s, e in tracks
                ),
            ),
        ),
    )


def test_a_track_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(P.BadPlan, match="not after"):
        P.validate(build([(1, 100.0, 50.0)]))


def test_a_zero_length_track_is_refused() -> None:
    with pytest.raises(P.BadPlan, match="not after"):
        P.validate(build([(1, 100.0, 100.0)]))


def test_overlapping_tracks_are_refused() -> None:
    """Overlap writes the same audio into two files."""
    with pytest.raises(P.BadPlan, match="before track"):
        P.validate(build([(1, 0.0, 100.0), (2, 90.0, 200.0)]))


def test_a_duplicate_track_number_across_sides_is_refused() -> None:
    """Track numbers run across the album, not per side, so they collide."""
    plan = P.Plan(
        slug="s",
        album="",
        artist="",
        sides=(
            build([(1, 0.0, 100.0)]).sides[0],
            build([(1, 0.0, 100.0)], file="side-b.flac").sides[0],
        ),
    )
    with pytest.raises(P.BadPlan, match="appears in both"):
        P.validate(plan)


def test_an_end_past_the_side_is_refused_when_the_duration_is_known() -> None:
    """Otherwise the last track is written truncated and nobody notices."""
    plan = build([(1, 0.0, 500.0)])
    P.validate(plan, durations={"side-a.flac": 500.0})  # exact is fine
    with pytest.raises(P.BadPlan, match="past the side"):
        P.validate(plan, durations={"side-a.flac": 400.0})


def test_validation_is_silent_about_sides_it_has_no_duration_for() -> None:
    P.validate(build([(1, 0.0, 500.0)]), durations={"side-z.flac": 10.0})
