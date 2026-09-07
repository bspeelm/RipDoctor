"""Carrying a verified cut across a re-rip.

Ripping a record again - a better converter, or replacing a bad take - does not
invalidate boundaries someone already listened to. Needle-drop timing drifts by
seconds between sessions; timing *within* a side does not, because it is the
same record on the same platter. So an approved cut is a template needing a
transform, not work to be thrown away.

The transform is fitted rather than assumed:

    new_time = offset + scale * old_time

Scale matters. A belt running 0.1 per cent different between sessions drifts
about a second across a sixteen-minute side, which is audible at a boundary.

Correlation is used to LOCATE, never to judge. Pearson r collapses on dense
material and falls as a probe gets longer, while the lag it picks stays correct
- so probes are short and r is only asked one question, "is this the same music
at all", which it answers well. Correctness is established separately, by
fitting on two probes and predicting a third the fit has never seen.

docs/method.md carries the measurements behind all of that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Measured: the same side against itself, shifted, and 0.1 per cent fast, scores
# 1.000 / 1.000 / 0.882. A needle from one side against a different side of the
# same record scores 0.391, and against a different record 0.379. 0.65 sits in
# the empty gap between those two groups.
MIN_R = 0.65

# A platter differing by more than this between sessions is not credible; the
# fit is more likely wrong than the turntable.
MAX_DRIFT = 0.02

# How far the predicted midpoint may land from where it actually is before the
# transform is refused.
CHECK_TOLERANCE = 0.20

# Probes sit inside the music, never in lead-in or run-out, where every record
# sounds like every other record and correlation means nothing.
PROBE_AT = (0.20, 0.50, 0.80)


class AlignError(Exception):
    """The re-rip could not be aligned to the original."""


@dataclass(frozen=True, slots=True)
class Probe:
    """Where a moment from the old side landed in the new capture."""

    old_t: float
    new_t: float
    r: float

    @property
    def shift(self) -> float:
        return self.new_t - self.old_t


@dataclass(frozen=True, slots=True)
class Transform:
    """Maps a time on the old side onto the new capture."""

    offset: float
    scale: float

    def apply(self, t: float) -> float:
        return self.offset + self.scale * t

    @property
    def drift_ms_per_min(self) -> float:
        """Speed difference in a unit a person can picture."""
        return (self.scale - 1.0) * 60000.0


def best_lag(hay: list[float], needle: list[float]) -> tuple[float, int]:
    """Pearson r of `needle` at every offset in `hay`. Returns (r, index).

    Prefix sums keep each window's mean and variance O(1), so only the dot
    product is per-lag and the whole search stays linear in the search range.
    """
    m, n = len(needle), len(hay)
    if m < 8 or n < m:
        raise AlignError("not enough audio to correlate")

    sum_b = sum(needle)
    sum_bb = sum(v * v for v in needle)

    cum = [0.0] * (n + 1)
    cum_sq = [0.0] * (n + 1)
    for i, v in enumerate(hay):
        cum[i + 1] = cum[i] + v
        cum_sq[i + 1] = cum_sq[i] + v * v

    best_r, best_i = -2.0, 0
    for lag in range(n - m + 1):
        sum_a = cum[lag + m] - cum[lag]
        sum_aa = cum_sq[lag + m] - cum_sq[lag]
        dot = 0.0
        for k in range(m):
            dot += needle[k] * hay[lag + k]
        num = dot - sum_b * sum_a / m
        den = math.sqrt(
            max(sum_bb - sum_b * sum_b / m, 1e-9)
            * max(sum_aa - sum_a * sum_a / m, 1e-9)
        )
        r = num / den if den > 0 else 0.0
        if r > best_r:
            best_r, best_i = r, lag
    return best_r, best_i


def probe_points(
    first: float, last: float, min_probe: float = 30.0
) -> tuple[float, ...]:
    """Where to take the three probes on a side, given where its music runs."""
    span = last - first
    if span < 3 * min_probe:
        raise AlignError(
            f"side is too short to fit three probes ({span:.0f}s of music)"
        )
    return tuple(first + span * f for f in PROBE_AT)


def require_match(probe: Probe, what: str, min_r: float = MIN_R) -> None:
    """Refuse a probe that did not find the same music."""
    if probe.r < min_r:
        raise AlignError(
            f"{what} only matched at r={probe.r:.2f} - not the same side, "
            "or the capture is bad"
        )


def fit(a: Probe, b: Probe, max_drift: float = MAX_DRIFT) -> Transform:
    """Fit offset and scale through two probes.

    Refuses an implied speed difference beyond `max_drift`. Two probes always
    produce *a* line; this is the only thing standing between a nonsense pair
    and every boundary on the side being moved by it.
    """
    if b.old_t == a.old_t:
        raise AlignError("both probes were taken at the same moment")

    scale = (b.new_t - a.new_t) / (b.old_t - a.old_t)
    if abs(scale - 1.0) > max_drift:
        raise AlignError(
            f"implied speed difference of {(scale - 1) * 100:.1f}% is not "
            "credible - refusing to shift boundaries on it"
        )
    return Transform(offset=a.new_t - scale * a.old_t, scale=scale)


def verify(
    transform: Transform, check: Probe, tolerance: float = CHECK_TOLERANCE
) -> float:
    """Test a fit against a probe it has never seen. Returns the miss.

    Two probes can only ever agree with themselves - any two points define a
    line, including two wrong ones. Predicting a third and measuring where the
    music actually is is the first real test of the transform, and an internally
    consistent but wrong fit fails it.
    """
    miss = check.new_t - transform.apply(check.old_t)
    if abs(miss) > tolerance:
        raise AlignError(
            f"midpoint lands {miss:+.2f}s from where the fit predicts - "
            "refusing a transform that does not hold across the side"
        )
    return miss
