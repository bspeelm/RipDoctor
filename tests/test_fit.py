"""Fitting: the precedence order, and the reporting of forced boundaries.

The single most important behaviour in this project is that an edge a person set
by ear is never overridden. It is tested twice - once constructed, so the
mechanism is pinned, and once against a real album's spec and the plan that was
actually fitted from it, so the guarantee is shown to hold end to end.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from ripdoctor.core import fit as FIT
from ripdoctor.core import gaps as G
from ripdoctor.core import plan as P
from ripdoctor.core.envelope import Envelope
from ripdoctor.core.fit import Fitted, _share_gap, fit_side, report, to_side

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def build(segments: list[tuple[float, float]], window: float = 0.05) -> Envelope:
    levels: list[float] = []
    for secs, db in segments:
        levels.extend([db] * round(secs / window))
    return Envelope(tuple(levels), window)


def side_with_gaps(n_tracks: int = 3, track: float = 60.0, gap: float = 6.0):
    """A side of equal tracks separated by clean gaps."""
    segs: list[tuple[float, float]] = [(10.0, -80)]  # lead-in
    for i in range(n_tracks):
        segs.append((track, -25))
        if i < n_tracks - 1:
            segs.append((gap, -80))
    env = build(segs)
    return env, G.find(env, below=G.BELOW)


def spec_side(
    n: int = 3,
    cat: float = 60.0,
    start: float = 10.0,
    end: float = 208.0,
    **kw: object,
) -> P.SpecSide:
    tracks = tuple(
        P.SpecTrack(number=i + 1, title=f"Track {i + 1}", cat=cat) for i in range(n)
    )
    return P.SpecSide(letter="a", start=start, end=end, tracks=tracks, **kw)  # type: ignore[arg-type]


# ----------------------------------------------- the precedence order


def test_an_ear_set_end_is_never_overridden() -> None:
    """The rule the whole spec/plan split exists to enforce.

    The gap here sits at 70-76 s and the catalogue says the track runs 60 s, so
    every detector and every arithmetic check agrees the cut belongs near 70.
    The human said 64.5. The human wins, with no argument and no adjustment.
    """
    env, gapset = side_with_gaps()
    side = P.SpecSide(
        letter="a",
        start=10.0,
        end=208.0,
        tracks=(
            P.SpecTrack(number=1, title="", cat=60.0, end=64.5),
            P.SpecTrack(number=2, title="", cat=60.0),
            P.SpecTrack(number=3, title="", cat=60.0),
        ),
    )
    fitted = fit_side(side, env, gapset, duration=208.0)

    assert fitted[0].track.end == 64.5
    assert fitted[0].reason == "ear"
    assert fitted[0].from_ear


def test_an_ear_set_start_overrides_what_the_previous_track_handed_over() -> None:
    env, gapset = side_with_gaps()
    side = P.SpecSide(
        letter="a",
        start=10.0,
        end=208.0,
        tracks=(
            P.SpecTrack(number=1, title="", cat=60.0),
            P.SpecTrack(number=2, title="", cat=60.0, start=99.0),
            P.SpecTrack(number=3, title="", cat=60.0),
        ),
    )
    fitted = fit_side(side, env, gapset, duration=208.0)
    assert fitted[1].track.start == 99.0


def test_the_last_track_ends_at_the_side_end() -> None:
    """Nothing detects the end of a side; a person measures it."""
    env, gapset = side_with_gaps()
    fitted = fit_side(spec_side(end=201.5), env, gapset, duration=208.0)
    assert fitted[-1].track.end == 201.5
    assert fitted[-1].reason == "side end"


def test_a_manual_gap_override_beats_the_detector() -> None:
    """For a gap the detector cannot see, measured by hand."""
    env, gapset = side_with_gaps()
    side = spec_side(fix={1: (68.0, 74.0)})
    fitted = fit_side(side, env, gapset, duration=208.0, lead=1.3, tail=1.5)

    assert fitted[0].track.end == pytest.approx(69.5)  # music_end + tail
    assert fitted[1].track.start == pytest.approx(72.7)  # next_start - lead
    assert fitted[0].reason.startswith("fix")


def test_precedence_is_ear_over_fix_over_gap() -> None:
    """All three offered at once; the ear must win."""
    env, gapset = side_with_gaps()
    side = P.SpecSide(
        letter="a",
        start=10.0,
        end=208.0,
        tracks=(
            P.SpecTrack(number=1, title="", cat=60.0, end=64.5),
            P.SpecTrack(number=2, title="", cat=60.0),
            P.SpecTrack(number=3, title="", cat=60.0),
        ),
        fix={1: (68.0, 74.0)},
    )
    fitted = fit_side(side, env, gapset, duration=208.0)
    assert fitted[0].track.end == 64.5 and fitted[0].reason == "ear"


# --------------------------------------------------- the gap-anchored path


def test_a_cut_lands_inside_a_measured_gap() -> None:
    env, gapset = side_with_gaps()
    fitted = fit_side(spec_side(), env, gapset, duration=208.0)

    assert fitted[0].reason.startswith("gap")
    assert fitted[0].want_outside is None, "catalogue and gap agreed"
    v = G.verdict(fitted[0].track.end, gapset)
    assert v.inside, "the placed cut is inside a gap that was actually measured"


def test_disagreement_is_reported_rather_than_absorbed() -> None:
    """The boundary that had to be forced is the one to listen to.

    Here the catalogue claims 40 s but the only gap is at 70 s. The cut goes in
    the gap - there is nowhere else - and the report says by how much the
    arithmetic disagreed.
    """
    env, gapset = side_with_gaps()
    fitted = fit_side(spec_side(cat=40.0), env, gapset, duration=208.0)

    assert fitted[0].want_outside is not None
    assert fitted[0].want_outside < -20, "the prediction fell well short of the gap"
    assert "OUTSIDE" in report(fitted)


def test_with_no_gap_anywhere_the_catalogue_is_used_and_labelled() -> None:
    env = build([(10, -80), (300, -25)])  # one unbroken block of music
    gapset = G.find(env, below=G.BELOW)
    fitted = fit_side(spec_side(cat=60.0), env, gapset, duration=310.0)

    assert fitted[0].reason == "no gap"
    assert fitted[0].track.end == pytest.approx(70.0, abs=0.5)


def test_a_gap_already_passed_is_not_reused() -> None:
    """Without this the track collapses into the gap it just started after."""
    env, gapset = side_with_gaps()
    fitted = fit_side(spec_side(), env, gapset, duration=208.0)
    ends = [f.track.end for f in fitted]
    assert ends == sorted(ends), "boundaries advance down the side"
    assert all(b > a for a, b in pairwise(ends))


def test_cuts_never_cross_when_the_gap_is_tighter_than_the_padding() -> None:
    """The overlap bug, reproduced.

    A 1.6 s gap cannot hold 1.5 s of tail and 1.3 s of lead. Clamping each
    padding separately - as the predecessor did - put the first track's end
    after the second track's start, so both files contained the same second of
    audio. Dividing the gap in proportion instead makes the cuts meet.
    """
    env, gapset = side_with_gaps(gap=1.6)
    fitted = fit_side(spec_side(), env, gapset, duration=200.0, lead=1.3, tail=1.5)

    for a, b in pairwise(fitted):
        assert b.track.start >= a.track.end - 1e-9, (
            f"track {b.track.number} starts at {b.track.start} before "
            f"track {a.track.number} ends at {a.track.end}"
        )


def test_a_tight_gap_is_divided_in_proportion_to_the_padding() -> None:
    """Both neighbours give up the same fraction of what they asked for."""
    lead, tail = 1.3, 1.5
    end, nxt = _share_gap(100.0, 101.6, lead, tail)
    assert end == nxt, "with no spare groove the cuts meet"
    assert end == pytest.approx(100.0 + 1.6 * (tail / (lead + tail)), abs=1e-6)


def test_a_roomy_gap_gives_each_neighbour_its_full_padding() -> None:
    end, nxt = _share_gap(100.0, 110.0, 1.3, 1.5)
    assert end == pytest.approx(101.5) and nxt == pytest.approx(108.7)
    assert nxt - end > 0, "the groove between them is discarded"


def test_an_inverted_or_empty_gap_collapses_to_a_point() -> None:
    assert _share_gap(100.0, 100.0, 1.3, 1.5) == (100.0, 100.0)
    assert _share_gap(100.0, 99.0, 1.3, 1.5) == (100.0, 100.0)


def test_every_fitted_side_survives_validation() -> None:
    """The invariant validate() enforces, checked across a range of gap widths."""
    for gap in (0.5, 1.6, 2.8, 4.0, 9.0):
        env, gapset = side_with_gaps(gap=gap)
        fitted = fit_side(spec_side(), env, gapset, duration=400.0)
        P.validate(P.Plan(slug="s", album="", artist="", sides=(to_side("a", fitted),)))


# ------------------------------------------------------------- reporting


def test_the_report_names_the_grounds_for_every_boundary() -> None:
    env, gapset = side_with_gaps()
    text = report(fit_side(spec_side(), env, gapset, duration=208.0))
    assert "boundary" in text and "delta" in text
    assert "total measured" in text
    assert text.count("gap ") >= 1


def test_an_outlier_is_flagged() -> None:
    env, gapset = side_with_gaps()
    fitted = fit_side(spec_side(cat=20.0), env, gapset, duration=208.0)
    assert any(f.is_outlier for f in fitted)
    assert "<<<" in report(fitted)


# --------------------------------------------------------- real album


def test_refitting_the_real_spec_reproduces_every_ear_set_edge_exactly() -> None:
    """The guarantee, end to end, on the album it was learned from.

    All eleven tracks on this record carry edges a person set by ear. Re-fitting
    the spec must return that plan unchanged - which is what makes a re-fit safe
    to run after someone has spent an evening listening.

    No envelope is needed: with every edge already decided, no detector is
    consulted at all. That is itself the point being tested.
    """
    spec = P.Spec.from_dict(json.loads((FIXTURES / "ear-set.spec.json").read_text()))
    expected = P.Plan.from_dict(
        json.loads((FIXTURES / "ear-set.plan.json").read_text())
    )

    empty = Envelope((-25.0,) * 100, 0.05)
    no_gaps = G.GapSet((), 0.0, 0.0, 0.0)

    checked = 0
    for side, want in zip(spec.sides, expected.sides, strict=True):
        fitted = fit_side(
            side,
            empty,
            no_gaps,
            duration=1e6,
            lead=spec.lead,
            tail=spec.tail,
        )
        got = to_side(side.letter, fitted)
        assert got.file == want.file
        for g, w in zip(got.tracks, want.tracks, strict=True):
            assert g.number == w.number
            assert g.start == pytest.approx(w.start, abs=0.005), f"{w.number} start"
            assert g.end == pytest.approx(w.end, abs=0.005), f"{w.number} end"
            checked += 1

    assert checked == 11, "every track was compared"


def test_the_real_refit_consults_no_detector() -> None:
    """Every boundary on that record is attributed to a person or the side end."""
    spec = P.Spec.from_dict(json.loads((FIXTURES / "ear-set.spec.json").read_text()))
    empty = Envelope((-25.0,) * 100, 0.05)
    no_gaps = G.GapSet((), 0.0, 0.0, 0.0)

    reasons: list[str] = []
    for side in spec.sides:
        reasons += [
            f.reason
            for f in fit_side(side, empty, no_gaps, duration=1e6, lead=spec.lead)
        ]

    assert len(reasons) == 11
    assert set(reasons) <= {"ear", "side end"}, reasons
    assert reasons.count("ear") >= 8


def test_the_fitted_real_plan_validates() -> None:
    spec = P.Spec.from_dict(json.loads((FIXTURES / "ear-set.spec.json").read_text()))
    empty = Envelope((-25.0,) * 100, 0.05)
    no_gaps = G.GapSet((), 0.0, 0.0, 0.0)

    sides = tuple(
        to_side(s.letter, fit_side(s, empty, no_gaps, duration=1e6, lead=spec.lead))
        for s in spec.sides
    )
    P.validate(P.Plan(slug=spec.slug, album="", artist="", sides=sides))


def test_fitted_is_a_value_that_explains_itself() -> None:
    f = Fitted(
        track=P.PlanTrack(number=1, title="t", start=0.0, end=10.0, cat=10.0),
        reason="ear",
    )
    assert f.from_ear and not f.is_outlier and f.track.delta == 0.0


def test_a_side_that_cannot_hold_its_tracks_says_so_in_words() -> None:
    """`end 1164.65 is not after start 1180.00` is arithmetic, and true, and
    tells nobody what to do. The catalogue being a different master from the
    pressing is the actual finding, and it is one a person can act on."""
    env, _gapset = side_with_gaps(n_tracks=3, track=60.0)
    # Three tracks the catalogue says are far longer than the side holds.
    side = spec_side(n=3, cat=200.0, start=10.0, end=200.0)
    spec = P.Spec(slug="album", album="A", artist="B", sides=(side,))
    with pytest.raises(FIT.OutOfSide) as caught:
        FIT.fit_plan(spec, {"a": env}, below=G.BELOW)
    said = str(caught.value)
    assert "side a" in said and "3 tracks" in said
    assert "another release" in said and "by ear" in said


def test_a_gap_too_far_from_the_prediction_is_not_this_boundary() -> None:
    """A boundary the detector never found - a fade, an intro running straight
    in - has no gap near it, and the nearest one is the next boundary. Taking it
    swallows a whole track: one record lost two to a 69-second intro."""
    # Three 60s tracks with gaps, but the catalogue says the first is 10s long.
    env, gapset = side_with_gaps(n_tracks=3, track=60.0, gap=6.0)
    near = FIT._pick_gap(gapset, want=20.0, after=10.0, reach=5.0)
    assert near is None, "a gap 50s from a 10s track is a neighbour's"
    assert FIT._pick_gap(gapset, want=20.0, after=10.0) is not None


def test_a_gap_containing_the_prediction_always_wins() -> None:
    """Reach only decides between gaps that do not contain it."""
    env, gapset = side_with_gaps(n_tracks=3, track=60.0, gap=6.0)
    inside = FIT._pick_gap(gapset, want=71.0, after=10.0, reach=0.0)
    assert inside is not None and inside.lo <= 71.0 <= inside.hi


def test_a_track_with_no_gap_near_it_falls_back_to_the_catalogue() -> None:
    """Which is what the fallback was always for, and never reached while the
    nearest gap anywhere on the side counted as near."""
    env, gapset = side_with_gaps(n_tracks=3, track=60.0, gap=6.0)
    side = spec_side(n=3, cat=10.0, start=10.0, end=208.0)
    fitted = fit_side(side, env, gapset, duration=208.0)
    assert fitted[0].reason == "no gap"
    assert fitted[0].track.end == 20.0
