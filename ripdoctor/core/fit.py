"""Placing the cuts: where a measured gap and the catalogue agree.

A boundary is trusted only where two independent constraints point at the same
place. Where they disagree the cut is clamped and the disagreement REPORTED,
never absorbed - a forced boundary is precisely the one to listen to.

Resolution order per track, highest first:

  1. an edge the human set by ear          - always wins, never overridden
  2. the side's own end, for the last track
  3. a manual override of one gap's edges
  4. the nearest measured gap to the catalogue prediction
  5. the catalogue prediction alone, when no gap is anywhere near

Only 4 consults a detector. See docs/method.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ripdoctor.core.envelope import Envelope
from ripdoctor.core.gaps import Gap, GapSet, refine
from ripdoctor.core.plan import LEAD, TAIL, PlanSide, PlanTrack, SpecSide

# A cut is never placed within this of its neighbour. Reaching the clamp means
# padding wanted more room than the gap has, which is a real answer - the gap is
# genuinely tight - not an error.
MIN_SEPARATION = 0.2

# A gap is only a candidate if it ends more than this after the track started.
# Without it the gap the track just began in is its own best match, and every
# track collapses to nothing.
MIN_ADVANCE = 1.0

# Beyond this, a track's measured length disagrees with the catalogue enough to
# be worth flagging in the report rather than merely recording.
OUTLIER = 15.0


@dataclass(frozen=True, slots=True)
class Fitted:
    """One placed track, and why it landed where it did.

    `reason` is kept because a plan without it cannot be argued with. When a
    boundary turns out wrong, the first question is always whether a detector
    chose it or a person did.
    """

    track: PlanTrack
    reason: str
    want_outside: float | None = None  # catalogue prediction missed the gap by

    @property
    def is_outlier(self) -> bool:
        return abs(self.track.delta) > OUTLIER

    @property
    def from_ear(self) -> bool:
        return self.reason == "ear"


def _pick_gap(gapset: GapSet, want: float, after: float) -> Gap | None:
    """The gap nearest the catalogue prediction, ignoring ones already passed.

    A gap containing the prediction scores zero and wins outright. Otherwise the
    nearest edge decides, so a prediction landing between two gaps goes to the
    closer one rather than always forward.
    """
    candidates = [g for g in gapset.gaps if g.hi > after + MIN_ADVANCE]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda g: (
            0.0 if g.lo <= want <= g.hi else min(abs(want - g.lo), abs(want - g.hi))
        ),
    )


def _share_gap(
    music_end: float, next_start: float, lead: float, tail: float
) -> tuple[float, float]:
    """Divide one gap between the track ending and the track starting.

    Normally each neighbour takes its padding and whatever is left over is
    groove that belongs to neither and is not written.

    When the gap is shorter than lead plus tail there is not enough to satisfy
    both, and the two paddings must be reconciled against each other. The
    predecessor clamped each one separately, which does not: on a gap of 1.6 s
    with 1.5 s of tail and 1.3 s of lead it placed the first track's end at
    71.35 and the second's start at 70.25, so the two tracks overlapped by more
    than a second and the same audio was written into both. See ADR-016.

    The fix is to divide the gap in proportion to the padding asked for, so both
    tracks give up the same fraction and the cuts meet rather than cross. There
    is no groove left to discard, which is correct - a gap that short has none.
    """
    if next_start <= music_end:
        return music_end, music_end

    if music_end + tail <= next_start - lead:
        return music_end + tail, next_start - lead

    span = next_start - music_end
    share = tail / (lead + tail) if lead + tail > 0 else 0.5
    meeting = music_end + span * share
    return meeting, meeting


def fit_side(
    side: SpecSide,
    env: Envelope,
    gapset: GapSet,
    *,
    duration: float,
    lead: float = LEAD,
    tail: float = TAIL,
) -> tuple[Fitted, ...]:
    """Place every track on one side.

    `env` and `gapset` must describe the same side and the same lane. The gapset
    is passed in rather than computed here because choosing the lane and its
    anchor is a decision the caller has to make explicitly - see core.gaps.
    """
    out: list[Fitted] = []
    cur = side.start

    for i, track in enumerate(side.tracks):
        last = i == len(side.tracks) - 1

        # 1. An ear-set start replaces whatever the previous track handed over.
        if track.start is not None:
            cur = track.start

        nxt: float | None = None
        want_outside: float | None = None

        if track.end is not None:
            end, reason = track.end, "ear"

        elif last:
            end, reason = side.end, "side end"

        elif track.number in side.fix:
            music_end, next_start = side.fix[track.number]
            end = music_end + tail
            nxt = next_start - lead
            reason = f"fix {music_end:.2f}/{next_start:.2f}"

        else:
            want = cur + track.cat
            gap = _pick_gap(gapset, want, cur)
            if gap is None:
                end = nxt = min(want, duration)
                reason = "no gap"
            else:
                music_end, next_start = refine(env, gap.lo, gap.hi)
                end, nxt = _share_gap(music_end, next_start, lead, tail)
                reason = f"gap {gap.lo:.2f}-{gap.hi:.2f}"
                if not gap.lo <= want <= gap.hi:
                    want_outside = want - (gap.lo + gap.hi) / 2.0

        out.append(
            Fitted(
                track=PlanTrack(
                    number=track.number,
                    title=track.title,
                    start=round(cur, 2),
                    end=round(end, 2),
                    cat=track.cat,
                ),
                reason=reason,
                want_outside=want_outside,
            )
        )

        if not last:
            following = side.tracks[i + 1]
            if following.start is not None:
                cur = following.start
            elif nxt is not None:
                cur = nxt
            else:
                cur = end

    return tuple(out)


def to_side(letter: str, fitted: tuple[Fitted, ...]) -> PlanSide:
    return PlanSide(file=f"side-{letter}.flac", tracks=tuple(f.track for f in fitted))


def report(fitted: tuple[Fitted, ...]) -> str:
    """A table of what was placed where, and on what grounds.

    The delta column is the scoreboard. A steady negative bias down a side is
    normal - quiet heads and tails fall below any threshold. An outlier is the
    bug signal, and equal-and-opposite deltas on adjacent tracks point straight
    at the boundary between them.
    """
    lines = [
        f"  {'#':<3}{'title':<28}{'start':>9}{'end':>9}"
        f"{'len':>8}{'cat':>8}{'delta':>8}  boundary"
    ]
    for f in fitted:
        t = f.track
        note = f.reason
        if f.want_outside is not None:
            note += f"  WANT {f.want_outside:+.2f} OUTSIDE"
        lines.append(
            f"  {t.number:<3}{t.title[:27]:<28}{t.start:>9.2f}{t.end:>9.2f}"
            f"{t.length:>8.2f}{t.cat:>8.2f}{t.delta:>+8.2f}  {note}"
            + ("  <<<" if f.is_outlier else "")
        )
    measured = sum(f.track.length for f in fitted)
    catalogue = sum(f.track.cat for f in fitted)
    lines.append(
        f"  total measured {measured:.1f}s   catalogue {catalogue:.1f}s   "
        f"delta {measured - catalogue:+.1f}s"
    )
    return "\n".join(lines)
