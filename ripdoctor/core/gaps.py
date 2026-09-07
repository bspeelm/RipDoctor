"""Sustained quiet runs, and whether a cut is in one. docs/method.md."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ripdoctor.core.envelope import Envelope

# Measured on one signal chain. See docs/method.md before assuming they carry.
BELOW = 16.0  # full lane: dB below the music level
ABOVE = 12.0  # band lane: dB above the measured floor
MINGAP = 1.2  # seconds; shorter runs are not inter-track gaps
MUSIC_PCT = 0.85
FLOOR_PCT = 0.02

# Above a gap's own floor, when refining its edges. Tighter than BELOW: the gap
# is already located, and the question is only where the fade stops.
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
    """Gaps, plus the levels they were judged against."""

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
    """Where a proposed cut sits: `margin` inside a gap, `distance` outside."""

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

    Pass exactly one of `below` (full lane) or `above` (band lane).
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
    """Where the music stops and starts inside a located gap, judged against
    that gap's own floor so a fade survives.
    """
    if hi <= lo or not len(env):
        return lo, hi

    # Bounds are inclusive at both ends, and readings are addressed by their own
    # timestamps rather than by a floored index. An exclusive end, or a floor
    # where the reference rounds, moves an edge by one window - 0.05 s, which is
    # small enough to look like agreement and is not.
    quarter = (hi - lo) * 0.25
    first = math.ceil((lo + quarter) / env.window)
    last = math.floor((hi - quarter) / env.window)
    core = env.levels[first : last + 1]
    if not core:
        return lo, hi

    threshold = sum(core) / len(core) + REFINE_ABOVE
    mid = (lo + hi) / 2.0

    lo_i = math.ceil(lo / env.window)
    mid_i = math.floor(mid / env.window)
    hi_i = math.floor(hi / env.window)

    music_end = lo
    for i in range(max(lo_i, 0), min(mid_i, len(env.levels) - 1) + 1):
        if env.levels[i] > threshold:
            music_end = i * env.window

    music_start = hi
    for i in range(
        max(math.ceil(mid / env.window), 0), min(hi_i, len(env.levels) - 1) + 1
    ):
        if env.levels[i] > threshold:
            music_start = i * env.window
            break

    return music_end, music_start


def verdict(t: float, gapset: GapSet) -> Verdict:
    """Judge a proposed cut against the detected gaps."""
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
