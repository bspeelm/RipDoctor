"""Finding the quiet places, and judging whether a cut is in one.

A gap is a sustained quiet run. Finding them is the whole job, and the only
subtle part is choosing what to measure "quiet" against.

**The two lanes need opposite anchors, and this is not a detail.**

The full lane thresholds *down from the music*: the 85th percentile minus a
margin. It cannot anchor on the floor, because the quietest reading on a vinyl
side is the needle-up electrical noise, well below actual groove noise, and
anchoring there reports no gaps at all on any side.

The band lane has to anchor *up from the floor*, because its dynamic range is
much wider - music around -35 and floor around -85, against -24 to -50 full
band. Thresholding at music-minus-16 in the band lane lands near -51 and
swallows every quiet passage: on one measured side that produced 57 gaps where
the record has a handful. Floor-plus-12 keeps only what is genuinely silent,
which is what a snap target has to be.

Get these the wrong way round and detection does not degrade, it inverts.
"""

from __future__ import annotations

from dataclasses import dataclass

from ripdoctor.core.envelope import Envelope

# Measured on the 48 kHz/16-bit chain. They are real measurements rather than
# preferences, but they are one chain's measurements - see docs/thresholds.md
# before treating any of them as universal.
BELOW = 16.0  # full lane: dB below the music level
ABOVE = 12.0  # band lane: dB above the measured floor
MINGAP = 1.2  # seconds; shorter runs are not inter-track gaps
MUSIC_PCT = 0.85
FLOOR_PCT = 0.02

# How far above a gap's own floor still counts as music, when refining its
# edges. Deliberately tighter than BELOW: by this point the gap has been
# located, and the question is where the fade actually stops.
REFINE_ABOVE = 8.0


@dataclass(frozen=True, slots=True)
class Gap:
    lo: float
    hi: float
    mean: float

    @property
    def length(self) -> float:
        return self.hi - self.lo

    def contains(self, t: float) -> bool:
        return self.lo <= t <= self.hi


@dataclass(frozen=True, slots=True)
class GapSet:
    """Gaps, plus the levels they were judged against.

    The thresholds are reported rather than discarded because a gap list on its
    own cannot be argued with. Knowing that music sat at -26 and the threshold
    at -42 is what makes a wrong answer diagnosable.
    """

    gaps: tuple[Gap, ...]
    threshold: float
    music: float
    floor: float

    def __len__(self) -> int:
        return len(self.gaps)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.gaps)

    def containing(self, t: float) -> Gap | None:
        for g in self.gaps:
            if g.contains(t):
                return g
        return None


@dataclass(frozen=True, slots=True)
class Verdict:
    """Where a proposed cut sits relative to the gaps.

    `margin` is how far the cut is from the nearer edge of the gap it is in -
    the number that says whether a boundary is comfortable or marginal.
    `distance` is how far outside the nearest gap it is, when it is not in one.
    """

    inside: bool
    lo: float | None = None
    hi: float | None = None
    margin: float | None = None
    distance: float | None = None

    def __str__(self) -> str:
        if self.inside:
            return f"in gap {self.lo:.2f}-{self.hi:.2f}, margin {self.margin:.2f}s"
        if self.lo is None:
            return "NOT in a gap; no gaps detected"
        return f"NOT in a gap; {self.distance:.2f}s from {self.lo:.2f}-{self.hi:.2f}"


def find(
    env: Envelope,
    *,
    below: float | None = None,
    above: float | None = None,
    mingap: float = MINGAP,
) -> GapSet:
    """Sustained quiet runs in one lane.

    Pass exactly one of `below` (anchor down from the music, for the full lane)
    or `above` (anchor up from the floor, for the band lane). Requiring the
    caller to choose is deliberate: a default would silently be wrong for one of
    the two lanes, and the failure is not obvious in the output.
    """
    if (below is None) == (above is None):
        raise ValueError("pass exactly one of below= (full lane) or above= (band lane)")
    if not len(env):
        return GapSet((), 0.0, 0.0, 0.0)

    music = env.percentile(MUSIC_PCT)
    floor = env.percentile(FLOOR_PCT)
    threshold = floor + above if above is not None else music - float(below or 0.0)

    runs: list[tuple[float, float]] = []
    start: float | None = None
    last = 0.0
    for i, level in enumerate(env.levels):
        t = i * env.window
        if level < threshold:
            if start is None:
                start = t
            last = t
        else:
            if start is not None and last - start >= mingap:
                runs.append((start, last))
            start = None
    if start is not None and last - start >= mingap:
        runs.append((start, last))

    out = []
    for lo, hi in runs:
        seg = env.levels[env.index(lo) : env.index(hi) + 1]
        out.append(Gap(lo, hi, sum(seg) / len(seg) if seg else threshold))
    return GapSet(tuple(out), threshold, music, floor)


def refine(env: Envelope, lo: float, hi: float) -> tuple[float, float]:
    """Where the music really stops and starts, inside a located gap.

    Returns (music_end, music_start). The gap has already been found; this asks
    a narrower question, and answers it against *the gap's own floor* rather
    than the side-wide threshold.

    That distinction is what preserves a fade. A decaying tail drops below the
    side-wide threshold well before it stops being music, so a side-wide answer
    truncates it - measured at three to four seconds early, on six track ends in
    a single pass. Judging against the floor of this particular gap keeps it.

    The middle half of the gap is used to establish that floor, so the fade at
    one end and the lead-in at the other do not contaminate the measurement.
    """
    if hi <= lo or not len(env):
        return lo, hi

    quarter = (hi - lo) * 0.25
    core = env.between(lo + quarter, hi - quarter)
    if not core:
        return lo, hi

    threshold = sum(core) / len(core) + REFINE_ABOVE
    mid = (lo + hi) / 2.0

    music_end = lo
    for i in range(env.index(lo), env.index(mid) + 1):
        if i < len(env.levels) and env.levels[i] > threshold:
            music_end = i * env.window

    music_start = hi
    for i in range(env.index(mid), env.index(hi) + 1):
        if i < len(env.levels) and env.levels[i] > threshold:
            music_start = i * env.window
            break

    return music_end, music_start


def verdict(t: float, gapset: GapSet) -> Verdict:
    """Judge a proposed cut against the detected gaps.

    This is the number the ear check is answering. A cut inside a gap with a
    healthy margin is safe to leave alone; one outside a gap, or inside with a
    margin of a tenth of a second, is one to listen to.
    """
    inside = gapset.containing(t)
    if inside is not None:
        return Verdict(
            inside=True,
            lo=inside.lo,
            hi=inside.hi,
            margin=min(t - inside.lo, inside.hi - t),
        )

    nearest: tuple[float, Gap] | None = None
    for g in gapset.gaps:
        d = min(abs(t - g.lo), abs(t - g.hi))
        if nearest is None or d < nearest[0]:
            nearest = (d, g)

    if nearest is None:
        return Verdict(inside=False)
    return Verdict(
        inside=False, lo=nearest[1].lo, hi=nearest[1].hi, distance=nearest[0]
    )
