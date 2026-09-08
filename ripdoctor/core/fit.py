"""Placing cuts where a measured gap and the catalogue agree. docs/method.md."""

from __future__ import annotations

from dataclasses import dataclass

from ripdoctor.core import gaps
from ripdoctor.core.envelope import Envelope
from ripdoctor.core.gaps import Gap, GapSet, refine
from ripdoctor.core.plan import LEAD, TAIL, Plan, PlanSide, PlanTrack, Spec, SpecSide

# A cut is never placed within this of its neighbour.
MIN_SEPARATION = 0.2

# A candidate gap must end this far after the track started, or the gap it just
# began in is its own best match and every track collapses.
MIN_ADVANCE = 1.0

# How far from its predicted place a gap may be and still be this track's
# boundary, as a fraction of the track's catalogue length. ADR-045.
REACH = 0.5


class OutOfSide(ValueError):
    """The tracks a side was given need more of it than the side has. ADR-045."""

    def __init__(self, side: SpecSide, reached: float) -> None:
        want = sum(t.cat for t in side.tracks)
        span = side.end - side.start
        super().__init__(
            f"side {side.letter}: the {len(side.tracks)} tracks the catalogue "
            f"puts here run {want:.0f}s against {span:.0f}s of music, and the "
            f"last one would start at {reached:.0f}s with the side ending at "
            f"{side.end:.0f}s. The release's durations do not match this "
            f"pressing - try another release, or place this side by ear."
        )


# Beyond this the disagreement with the catalogue is worth flagging, not just
# recording.
OUTLIER = 15.0


@dataclass(frozen=True, slots=True)
class Fitted:
    """One placed track, and why it landed there."""

    track: PlanTrack
    reason: str
    want_outside: float | None = None  # catalogue prediction missed the gap by

    @property
    def is_outlier(self) -> bool:
        return abs(self.track.delta) > OUTLIER

    @property
    def from_ear(self) -> bool:
        return self.reason == "ear"


def _pick_gap(
    gapset: GapSet, want: float, after: float, reach: float | None = None
) -> Gap | None:
    """The gap nearest the prediction, if it is near enough to be this one.

    A boundary the detector never found has no gap near it, and the nearest is
    then the next boundary - which taken swallows a whole track. ADR-045.
    """
    candidates = [g for g in gapset.gaps if g.hi > after + MIN_ADVANCE]
    if not candidates:
        return None
    nearest = min(
        candidates,
        key=lambda g: (
            0.0 if g.lo <= want <= g.hi else min(abs(want - g.lo), abs(want - g.hi))
        ),
    )
    if reach is None or nearest.lo <= want <= nearest.hi:
        return nearest
    away = min(abs(want - nearest.lo), abs(want - nearest.hi))
    return nearest if away <= reach else None


def _share_gap(
    music_end: float, next_start: float, lead: float, tail: float
) -> tuple[float, float]:
    """Divide one gap between the track ending and the one starting.

    Too small for both paddings, it is split in proportion. ADR-016.
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
    """Place every track on one side. `env` and `gapset` must match."""
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
            gap = _pick_gap(gapset, want, cur, reach=track.cat * REACH)
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


def fit_plan(
    spec: Spec,
    envelopes: dict[str, Envelope],
    *,
    above: float | None = None,
    below: float | None = None,
) -> tuple[Plan, dict[str, tuple[Fitted, ...]]]:
    """Fit every side of a spec, and return the plan with its reasoning.

    The anchor travels with the lane the envelopes are in: gaps.find refuses to
    guess which one it was handed, and so does this. ADR-030.
    """
    anchor = {"above": above} if above is not None else {"below": below}
    sides, working = [], {}
    for side in spec.sides:
        env = envelopes.get(side.letter)
        if env is None:
            raise KeyError(f"no envelope for side {side.letter}")
        fitted = fit_side(
            side,
            env,
            gaps.find(env, **anchor),
            duration=env.duration,
            lead=spec.lead,
            tail=spec.tail,
        )
        # Checked here rather than inside the fit, which stays faithful to the
        # numbers the reference produced. A track that would start after its
        # side ends is the one shape those numbers cannot be cut from.
        bad = next((f for f in fitted if f.track.end <= f.track.start), None)
        if bad is not None:
            raise OutOfSide(side, bad.track.start)
        working[side.letter] = fitted
        sides.append(to_side(side.letter, fitted))
    plan = Plan(
        slug=spec.slug,
        album=spec.album,
        artist=spec.artist,
        sides=tuple(sides),
        date=spec.date,
    )
    return plan, working


def to_side(letter: str, fitted: tuple[Fitted, ...]) -> PlanSide:
    return PlanSide(file=f"side-{letter}.flac", tracks=tuple(f.track for f in fitted))


def report(fitted: tuple[Fitted, ...]) -> str:
    """A table of what was placed where, and on what grounds."""
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
