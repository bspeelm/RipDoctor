"""Carrying a verified cut across a re-rip. docs/method.md."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Sits in the empty gap between "same music" (0.88 to 1.00) and "different
# music" (0.38 to 0.39), measured across a real library. docs/method.md.
MIN_R = 0.65

# Beyond this the fit is likelier wrong than the turntable.
MAX_DRIFT = 0.02

# How far the predicted midpoint may miss before the transform is refused.
CHECK_TOLERANCE = 0.20

# Inside the music: in lead-in or run-out every record correlates with any other.
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
    """Pearson r of `needle` at every offset in `hay`. Returns (r, index)."""
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
    """Fit offset and scale through two probes, refusing incredible drift."""
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
    """Test a fit against a probe it has never seen. Returns the miss."""
    miss = check.new_t - transform.apply(check.old_t)
    if abs(miss) > tolerance:
        raise AlignError(
            f"midpoint lands {miss:+.2f}s from where the fit predicts - "
            "refusing a transform that does not hold across the side"
        )
    return miss
