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


# ---------------------------------------------------------- who settled a track


def a_track_doc(**extra: object) -> dict:
    return {"number": 1, "title": "One", "start": 1.0, "end": 9.0, "cat": 8.0, **extra}


def a_plan_doc(*tracks: dict) -> dict:
    return {
        "slug": "album",
        "album": "A",
        "artist": "B",
        "sides": [{"file": "side-a.flac", "tracks": list(tracks)}],
    }


def only_track(*docs: dict) -> P.PlanTrack:
    return P.Plan.from_dict(a_plan_doc(*docs)).sides[0].tracks[0]


@pytest.mark.parametrize("who", [P.HAND, P.EAR])
def test_who_settled_a_track_survives_the_document(who: str) -> None:
    plan = P.Plan.from_dict(a_plan_doc(a_track_doc(settled=who)))
    assert P.Plan.from_dict(plan.to_dict()).sides[0].tracks[0].settled == who


def test_a_plan_older_than_the_field_loads_and_writes_nothing_new() -> None:
    plan = P.Plan.from_dict(a_plan_doc(a_track_doc(), a_track_doc(forced=True)))
    first, second = plan.sides[0].tracks
    assert (first.settled, first.forced) == ("", False)
    assert (second.settled, second.forced) == ("", True)
    assert "settled" not in json.dumps(plan.to_dict())


def test_a_persons_judgement_ends_the_doubt_in_any_document() -> None:
    track = only_track(a_track_doc(forced=True, settled=P.EAR))
    assert not track.forced


def test_a_settled_value_nobody_knows_cannot_clear_a_doubt() -> None:
    track = only_track(a_track_doc(forced=True, settled="maybe"))
    assert track.forced and not track.settled


def test_a_cut_is_refused_naming_each_track_still_to_check() -> None:
    plan = P.Plan.from_dict(
        a_plan_doc(
            a_track_doc(forced=True),
            a_track_doc(number=2, title="Two", forced=True, start=10.0, end=20.0),
            a_track_doc(number=3, title="Three", start=20.0, end=30.0),
        )
    )
    said = P.cut_refusal(plan)
    assert '1 "One"' in said and '2 "Two"' in said and "Three" not in said
    assert "Checked" in said


def test_a_cut_is_refused_on_times_from_a_replaced_capture_with_the_ways_out() -> None:
    plan = P.Plan.from_dict(
        a_plan_doc(
            a_track_doc(placed=P.EARLIER),
            a_track_doc(number=2, title="Two", start=10.0, end=20.0, placed=P.EARLIER),
            a_track_doc(number=3, title="Three", start=20.0, end=30.0, settled=P.HAND),
        )
    )
    said = P.cut_refusal(plan, ("a",))
    assert "side a was captured again" in said
    assert '1 "One"' in said and '2 "Two"' in said and "Three" not in said
    assert "First pass" in said and "Align from archive" in said and "Checked" in said
    saved_over = P.cut_refusal(plan)
    assert "Align" not in saved_over
    assert (
        "First pass" in saved_over and "Checked" in saved_over and "moved" in saved_over
    )


def test_a_track_from_a_replaced_capture_is_named_once_even_if_also_in_doubt() -> None:
    plan = P.Plan.from_dict(a_plan_doc(a_track_doc(forced=True, placed=P.EARLIER)))
    assert P.cut_refusal(plan).count('1 "One"') == 1
    assert "captured again" in P.cut_refusal(plan)


def test_a_replaced_capture_and_a_doubt_on_another_side_are_both_said() -> None:
    doc = a_plan_doc(a_track_doc(placed=P.EARLIER))
    doc["sides"].append(
        {
            "file": "side-b.flac",
            "tracks": [
                a_track_doc(number=2, title="Two", start=1.0, end=9.0, forced=True)
            ],
        }
    )
    said = P.cut_refusal(P.Plan.from_dict(doc))
    assert '1 "One"' in said and '2 "Two"' in said and "not yet checked" in said


def test_a_persons_mark_on_a_replaced_capture_is_not_refused() -> None:
    plan = P.Plan.from_dict(a_plan_doc(a_track_doc(placed=P.EARLIER, settled=P.EAR)))
    assert P.cut_refusal(plan) == ""


def stamped(stamp: str, **track: object) -> P.Plan:
    doc = a_plan_doc(a_track_doc(**track))
    doc["sides"][0]["audio"] = stamp
    return P.Plan.from_dict(doc)


def test_a_check_made_on_a_row_shown_as_earlier_survives_the_save_as_heard_here() -> (
    None
):
    """The page only says `earlier` after the re-rip, so this is not a stale tab."""
    plan, released = P.saved_over(
        stamped("", placed=P.EARLIER, settled=P.EAR),
        stamped("old"),
        {"side-a.flac": "new"},
    )
    track = plan.sides[0].tracks[0]
    assert (track.settled, track.placed) == (P.EAR, "")
    assert not released


def test_a_check_from_a_tab_older_than_the_re_rip_is_still_let_go() -> None:
    plan, released = P.saved_over(
        stamped("", settled=P.EAR),
        stamped("old", settled=P.EAR),
        {"side-a.flac": "new"},
    )
    track = plan.sides[0].tracks[0]
    assert (track.settled, track.placed) == ("", P.EARLIER)
    assert released


def test_a_check_on_a_side_already_saved_over_comes_out_heard_here() -> None:
    plan, _released = P.saved_over(
        stamped("", placed=P.EARLIER, settled=P.EAR),
        stamped("new", placed=P.EARLIER),
        {"side-a.flac": "new"},
    )
    assert plan.sides[0].tracks[0].placed == ""
    assert not P.cut_refusal(plan)


def test_a_plan_with_nothing_to_check_has_no_refusal() -> None:
    plan = P.Plan.from_dict(a_plan_doc(a_track_doc(settled=P.EAR), a_track_doc()))
    assert P.cut_refusal(plan) == ""
