"""The measured constants, in one place, with their provenance. ADR-008.

Every number here came from measurement on one signal chain rather than from
taste. They are shipped as defaults because they are the best evidence there is,
not because they are universal - docs/method.md says where each came from, and
`ripdoctor measure` reports what your own chain does beside them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from ripdoctor.core import autostop, gaps, sides, xcorr


@dataclass(frozen=True, slots=True)
class Thresholds:
    # Gap detection.
    gap_below: float = gaps.BELOW
    gap_above: float = gaps.ABOVE
    gap_minimum: float = gaps.MINGAP
    refine_above: float = gaps.REFINE_ABOVE

    # Where a side's music starts and ends.
    span_below: float = sides.SPAN_BELOW
    span_run: float = sides.SPAN_RUN

    # Ending a capture.
    autostop_below: float = autostop.BELOW
    autostop_dwell: float = autostop.DWELL
    autostop_max_seconds: float = autostop.MAX_SECONDS
    arm_floor: float = autostop.ARM_FLOOR

    # Carrying a cut across a re-rip.
    align_min_r: float = xcorr.MIN_R
    align_max_drift: float = xcorr.MAX_DRIFT

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def changed_from_default(self) -> dict[str, tuple[Any, Any]]:
        """What has been moved, and from what.

        A threshold that has been changed is the first thing to look at when
        detection behaves unexpectedly, so it is reported rather than buried.
        """
        base = Thresholds()
        return {
            f.name: (getattr(base, f.name), getattr(self, f.name))
            for f in fields(self)
            if getattr(base, f.name) != getattr(self, f.name)
        }


NAMES = tuple(f.name for f in fields(Thresholds))
