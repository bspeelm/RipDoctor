"""The port against the tool it was ported from, on real records.

Six records, seventy-three tracks. For each, the reference implementation was
run on the same envelope with the same spec, and its answer stored. Anything
this suite does differently has to be a difference somebody chose.

That is the check a synthetic test cannot make. A port can be right about every
case anyone thought to write down and still be wrong about the record in front
of you.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from ripdoctor.core import gaps as G
from ripdoctor.core import plan as P
from ripdoctor.core.envelope import Envelope
from ripdoctor.core.fit import fit_side, to_side

FIXTURES = Path(__file__).parent / "fixtures" / "golden"


def records() -> list[tuple[str, dict]]:
    return [
        (p.name, json.loads(p.read_text()))
        for p in sorted(FIXTURES.glob("*.json"))
        if p.name != "manifest.json"
    ]


def envelope_of(side: dict) -> Envelope:
    raw = base64.b64decode(side["levels_b64"])
    return Envelope(tuple(v * 0.5 - 127.5 for v in raw), side["window"])


def refit(rec: dict) -> dict[tuple[str, int], tuple[float, float]]:
    """Run this implementation over a record, keyed by (side, track number)."""
    spec = P.Spec.from_dict(rec["detector_spec"])
    out: dict[tuple[str, int], tuple[float, float]] = {}
    for side in spec.sides:
        env = envelope_of(rec["sides"][side.letter])
        fitted = fit_side(
            side,
            env,
            G.find(env, below=G.BELOW),
            duration=rec["sides"][side.letter]["duration"],
            lead=spec.lead,
            tail=spec.tail,
        )
        for f in fitted:
            out[(side.letter, f.track.number)] = (f.track.start, f.track.end)
    return out


def reference(rec: dict) -> dict[tuple[str, int], tuple[float, float]]:
    out = {}
    letters = [s["letter"] for s in rec["detector_spec"]["sides"]]
    for letter, side in zip(letters, rec["detector_plan"]["sides"], strict=True):
        for t in side["tracks"]:
            out[(letter, t["number"])] = (t["start"], t["end"])
    return out


def test_the_fixture_set_is_what_it_claims() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    recs = records()
    assert len(recs) >= 6, "the golden set has shrunk; the pattern has drifted"
    assert set(manifest) == {n for n, _ in recs}
    assert sum(m["tracks"] for m in manifest.values()) >= 70


@pytest.mark.parametrize("name", [n for n, _ in records()])
def test_every_boundary_matches_the_reference(name: str) -> None:
    """Every placement is the reference's, except where ADR-016 changed it.

    The exception is exact rather than a tolerance: a pair of neighbours may
    differ only when the gap between their music is shorter than lead plus tail,
    which is the case the reference clamps each padding separately for. Anywhere
    the gap is roomy, agreement must be exact.
    """
    rec = dict(records())[name]
    spec = P.Spec.from_dict(rec["detector_spec"])
    letters = [s.letter for s in spec.sides]
    ref_sides = dict(zip(letters, rec["detector_plan"]["sides"], strict=True))

    roomy_mismatches, tight_pairs = [], 0
    for side in spec.sides:
        sd = rec["sides"][side.letter]
        env = envelope_of(sd)
        mine = fit_side(
            side,
            env,
            G.find(env, below=G.BELOW),
            duration=sd["duration"],
            lead=spec.lead,
            tail=spec.tail,
        )
        ref = ref_sides[side.letter]["tracks"]
        assert [f.track.number for f in mine] == [t["number"] for t in ref]

        for i, (f, t) in enumerate(zip(mine, ref, strict=True)):
            differs = (
                abs(f.track.start - t["start"]) > 0.011
                or abs(f.track.end - t["end"]) > 0.011
            )
            if not differs:
                continue
            # Is this boundary one the padding could not fit into?
            neighbours = [j for j in (i - 1, i) if 0 <= j < len(ref) - 1]
            tight = any(
                ref[j + 1]["start"] - ref[j]["end"] < spec.lead + spec.tail
                for j in neighbours
            )
            if tight:
                tight_pairs += 1
            else:
                roomy_mismatches.append(
                    f"  side {side.letter} track {t['number']}: "
                    f"this {f.track.start:.2f}-{f.track.end:.2f} "
                    f"reference {t['start']:.2f}-{t['end']:.2f}  [{f.reason}]"
                )

    assert not roomy_mismatches, (
        "boundaries differ where the gap was roomy:\n" + "\n".join(roomy_mismatches)
    )


def test_the_reference_overlaps_tracks_and_this_does_not() -> None:
    """ADR-016, measured on real records rather than a constructed gap.

    Where a gap is shorter than lead plus tail the reference clamps each padding
    against the gap and not against the other, so a track can end after its
    neighbour starts and the same audio is written into both files. Ten pairs
    across this fixture set do exactly that, by up to 0.90 s.
    """
    overlapping_in_reference = checked = 0
    for _name, rec in records():
        spec = P.Spec.from_dict(rec["detector_spec"])
        letters = [s.letter for s in spec.sides]
        ref_sides = dict(zip(letters, rec["detector_plan"]["sides"], strict=True))

        for side in spec.sides:
            sd = rec["sides"][side.letter]
            env = envelope_of(sd)
            mine = fit_side(
                side,
                env,
                G.find(env, below=G.BELOW),
                duration=sd["duration"],
                lead=spec.lead,
                tail=spec.tail,
            )
            ref = ref_sides[side.letter]["tracks"]
            for i in range(len(ref) - 1):
                checked += 1
                if ref[i + 1]["start"] < ref[i]["end"] - 1e-9:
                    overlapping_in_reference += 1
                assert mine[i + 1].track.start >= mine[i].track.end - 1e-9, (
                    f"this implementation overlapped tracks "
                    f"{ref[i]['number']}->{ref[i + 1]['number']}"
                )

    assert checked >= 50, "too few adjacent pairs; the pattern has drifted"
    assert overlapping_in_reference >= 8, (
        "the reference no longer overlaps anything - either the fixtures changed "
        "or the defect ADR-016 describes was not real"
    )


def test_the_detector_actually_ran() -> None:
    """A golden test that silently exercised no detection would pass forever."""
    total = detector = 0
    for _name, rec in records():
        spec = P.Spec.from_dict(rec["detector_spec"])
        for side in spec.sides:
            env = envelope_of(rec["sides"][side.letter])
            for f in fit_side(
                side,
                env,
                G.find(env, below=G.BELOW),
                duration=rec["sides"][side.letter]["duration"],
                lead=spec.lead,
                tail=spec.tail,
            ):
                total += 1
                if f.reason.startswith("gap") or f.reason == "no gap":
                    detector += 1
    assert total >= 70
    assert detector > total * 0.5, (
        f"only {detector} of {total} boundaries came from the detector"
    )


def test_a_fit_that_runs_out_of_side_is_refused_rather_than_written() -> None:
    """The safety net the reference does not have.

    On one record the detector gives a track an end 52 s past its catalogue
    duration, consuming the gap the last track needed. The last track then
    starts after the side's music ends: start 1268.55, end 1266.57. Cutting that
    runs ffmpeg from a later time to an earlier one and writes an empty file.

    This implementation produces the same numbers - the port is faithful - and
    then refuses the plan. The reference has no validation and writes it.
    """
    refused, validated = [], 0
    for name, rec in records():
        spec = P.Spec.from_dict(rec["detector_spec"])
        sides = []
        for side in spec.sides:
            sd = rec["sides"][side.letter]
            env = envelope_of(sd)
            sides.append(
                to_side(
                    side.letter,
                    fit_side(
                        side,
                        env,
                        G.find(env, below=G.BELOW),
                        duration=sd["duration"],
                        lead=spec.lead,
                        tail=spec.tail,
                    ),
                )
            )
        try:
            P.validate(P.Plan(slug=spec.slug, album="", artist="", sides=tuple(sides)))
            validated += 1
        except P.BadPlan as e:
            refused.append((name, str(e)))

    assert validated >= 4, "most first passes should produce a cuttable plan"
    assert refused, (
        "no first pass was refused - the fixture that runs out of side has gone"
    )
    for _name, why in refused:
        assert "not after" in why or "past the side" in why, why


def test_a_forced_boundary_is_reported() -> None:
    """The disagreement that precedes the failure above is not silent.

    Where the catalogue prediction lands outside the gap the detector chose, the
    miss is reported. That is the signal to listen to a boundary, and on the
    record that runs out of side it is the first thing that went wrong.
    """
    reported = total = 0
    for _name, rec in records():
        spec = P.Spec.from_dict(rec["detector_spec"])
        for side in spec.sides:
            sd = rec["sides"][side.letter]
            env = envelope_of(sd)
            for f in fit_side(
                side,
                env,
                G.find(env, below=G.BELOW),
                duration=sd["duration"],
                lead=spec.lead,
                tail=spec.tail,
            ):
                total += 1
                if f.want_outside is not None:
                    reported += 1

    assert total >= 70
    assert reported > 0, "no first pass disagreed with its catalogue at all"
