"""What your own signal chain does, beside the numbers this ships with. ADR-008.

The shipped thresholds were derived by replaying archived sides from one
turntable through one converter. They are measurements rather than preferences,
but they are that chain's measurements. This compares them against three
reference recordings from yours and says where they disagree.

It reports and suggests. It does not rewrite configuration, because a command
that computed new thresholds would be shipping a guess with the authority of a
measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

from ripdoctor.core.meter import Levels

# How much room a threshold needs on either side to be worth trusting. A
# threshold sitting one decibel off a measured level is a threshold that will be
# on the wrong side of it on a different record.
MARGIN = 6.0

# The peak the shipped numbers were calibrated at. Not a threshold - a statement
# about where the gain was set.
CALIBRATED_PEAK = -12.3


@dataclass(frozen=True, slots=True)
class Reference:
    """Three recordings that describe a signal chain.

    `dead` is the electrical floor with nothing playing. `groove` is the needle
    down on silent vinyl - lead-in or run-out - which is the level an
    inter-track gap actually sits at. `music` is a loud passage.
    """

    dead: Levels
    groove: Levels
    music: Levels

    @property
    def band_separation(self) -> float:
        """How far music sits above a silent groove in the 1-3 kHz lane."""
        return round(self.music.band - self.groove.band, 1)

    @property
    def full_separation(self) -> float:
        """The same distance measured full-band, which is the whole point.

        On a sparse pressing this is small enough that no threshold exists
        between a gap and a quiet passage. That is why the band lane exists,
        and this is where somebody sees it on their own equipment.
        """
        return round(self.music.full - self.groove.full, 1)


@dataclass(frozen=True, slots=True)
class Finding:
    """One threshold, what this chain measured, and whether they agree."""

    name: str
    shipped: float
    measured: float
    note: str
    suggestion: float | None = None

    @property
    def agrees(self) -> bool:
        return self.suggestion is None


def _offset(name: str, shipped: float, separation: float, note: str) -> Finding:
    """A threshold expressed as a distance below or above a measured level.

    It has to fit inside the separation this chain actually has, with room to
    spare, or it is on the wrong side of every gap on the record.
    """
    room = separation - MARGIN
    return Finding(
        name=name,
        shipped=shipped,
        measured=separation,
        note=note,
        suggestion=None if shipped <= room else round(max(1.0, room), 1),
    )


def compare(ref: Reference, shipped: dict[str, float]) -> list[Finding]:
    """Every threshold a set of reference recordings can speak to."""
    sep = ref.band_separation
    found = [
        _offset(
            "gap_above",
            shipped["gap_above"],
            sep,
            "band lane: how far above the groove floor a gap stops being a gap",
        ),
        _offset(
            "span_below",
            shipped["span_below"],
            sep,
            "band lane: how far under the music the side's edges are found",
        ),
        _offset(
            "autostop_below",
            shipped["autostop_below"],
            sep,
            "band lane: how far under the music counts as run-out",
        ),
    ]

    # arm_floor is an absolute level rather than a distance, so it has to land
    # between two measured ones: above the electrical floor, or the detector
    # arms on an empty room, and below the music, or it never arms at all.
    floor = shipped["arm_floor"]
    low, high = ref.dead.band, ref.music.band
    fits = low + MARGIN <= floor <= high - MARGIN
    found.append(
        Finding(
            name="arm_floor",
            shipped=floor,
            measured=round((low + high) / 2, 1),
            note=(
                f"must sit between dead air ({low:.1f}) and music ({high:.1f}) "
                "in the band lane"
            ),
            suggestion=None if fits else round((low + high) / 2, 1),
        )
    )
    return found


def snippet(findings: list[Finding]) -> str:
    """Configuration to paste, for the thresholds that disagreed. Nothing more.

    A chain that agrees with the defaults gets no snippet at all, because
    writing values that are already the defaults into a configuration file
    makes them look deliberate.
    """
    moved = [f for f in findings if not f.agrees]
    if not moved:
        return ""
    lines = ["[thresholds]"]
    lines += [f"{f.name} = {f.suggestion}" for f in moved]
    return "\n".join(lines)


def report(ref: Reference, findings: list[Finding]) -> str:
    lines = [
        "  measured on this chain",
        f"    dead air     full {ref.dead.full:>7.1f}  1-3k {ref.dead.band:>7.1f}",
        f"    silent groove full {ref.groove.full:>6.1f}  1-3k {ref.groove.band:>7.1f}",
        f"    music        full {ref.music.full:>7.1f}  1-3k {ref.music.band:>7.1f}"
        f"  peak {ref.music.peak:>7.1f}",
        "",
        f"    music above a silent groove: {ref.band_separation:>5.1f} dB in 1-3 kHz,"
        f" {ref.full_separation:>5.1f} dB full band",
    ]
    if ref.band_separation > ref.full_separation:
        # The founding observation, on the user's own equipment.
        lines.append(
            "    the band lane separates them by "
            f"{ref.band_separation - ref.full_separation:.1f} dB more, which is "
            "why it exists"
        )
    lines += [
        "",
        f"    peak {ref.music.peak:.1f} dBFS; the shipped numbers were calibrated "
        f"at {CALIBRATED_PEAK}",
        "",
        "  thresholds",
    ]
    for f in findings:
        mark = " " if f.agrees else "!"
        suggested = "" if f.agrees else f"  -> {f.suggestion}"
        lines.append(f"   {mark}{f.name:<16} {f.shipped:>7.1f}{suggested}")
        lines.append(f"      {f.note}")
    return "\n".join(lines)
