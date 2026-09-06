"""Level envelopes: the unit the whole algorithm computes over.

An envelope is a list of decibel readings on a uniform time grid. Everything
downstream - gap detection, edge refinement, fitting, alignment - takes one of
these rather than audio. That boundary is what lets the algorithm be tested with
no decoder, no sound card and no files. See ADR-005.

Three lanes are measured over the same grid, and the middle one is the reason
this project exists:

    full   RMS across the whole spectrum
    peak   the largest sample in each window
    band   RMS restricted to 1-3 kHz

On a sparse pressing the full lane cannot tell an inter-track gap from a quiet
passage; both land near -43 dB and no threshold separates them. In the band lane
the same two things sit about 20 dB apart, because vinyl's noise - plinth
rumble, arm handling, warp - is bass-heavy, and above 8 kHz a quiet pressing has
nothing at all. The band lane is where music is and noise is not.

Peak exists as a separate lane because RMS cannot find a fade. RMS averages a
decaying tail into its own window, so the end it reports is where the tail stops
dominating rather than where the music stops - typically three to four seconds
early.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

# On-disk envelope cache format. The magic and version are part of the file, so
# a reader can refuse a format it does not understand rather than misreading it.
MAGIC = b"CAE1"
VERSION = 1
HEADER_BYTES = 16
DEFAULT_WINDOW_MS = 50

# Decibels are stored one byte per window per lane: v = round((dB + 127.5) * 2),
# giving half-decibel steps across -127.5..0 dB. Half a decibel is far finer
# than any judgement made from these numbers, and a byte keeps a 22-minute side
# to about 26 kB per lane.
DB_MIN = -127.5
DB_STEP = 0.5
DB_OFFSET = 127.5


class FormatError(ValueError):
    """A blob is not a readable envelope file."""


def q_db(db: float | None) -> int:
    """Quantise one decibel reading to a byte.

    None, NaN and -inf all mean "nothing measurable here" and collapse to the
    floor. ffmpeg emits all three: a silent window gives -inf, and astats prints
    `-nan` for a window it could not measure.
    """
    if db is None or db != db or db == float("-inf"):
        db = DB_MIN
    v = round((max(DB_MIN, min(0.0, db)) + DB_OFFSET) / DB_STEP)
    return max(0, min(255, v))


def deq_db(v: int) -> float:
    """Inverse of q_db, to within half a step."""
    return v * DB_STEP - DB_OFFSET


@dataclass(frozen=True, slots=True)
class Envelope:
    """Decibel readings on a uniform grid, and the grid spacing."""

    levels: tuple[float, ...]
    window: float  # seconds per reading

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window must be positive")

    def __len__(self) -> int:
        return len(self.levels)

    @property
    def duration(self) -> float:
        """Seconds covered. This is the envelope's own view of the side, which
        is not necessarily the audio's true length - see rescaled()."""
        return len(self.levels) * self.window

    def index(self, t: float) -> int:
        """The reading covering time t, clamped to the envelope."""
        return max(0, min(len(self.levels) - 1, int(t / self.window)))

    def at(self, t: float) -> float:
        return self.levels[self.index(t)]

    def between(self, lo: float, hi: float) -> tuple[float, ...]:
        """Readings covering [lo, hi). Empty if the range is inverted."""
        return self.levels[self.index(lo) : self.index(hi)]

    def percentile(self, p: float) -> float:
        """The p-th percentile reading, 0.0 to 1.0.

        Detection anchors on the 85th percentile - the music - and never on the
        minimum. A side has at least three distinct floors: the electrical noise
        of the converter, the lead-in and run-out groove, and the inter-track
        gaps, and they differ by tens of decibels. Anchoring low picks whichever
        floor happens to be quietest on that side and finds nothing.
        """
        if not self.levels:
            raise ValueError("empty envelope")
        ordered = sorted(self.levels)
        return ordered[min(len(ordered) - 1, max(0, int(p * len(ordered))))]

    def rescaled(self, true_duration: float) -> Envelope:
        """Stretch the grid onto the audio's real length.

        ffmpeg's resampler adds latency padding, so an envelope taken from its
        output runs roughly 0.25 per cent long. Left uncorrected that is about
        three seconds of drift across a twenty-minute side, and every cut lands
        late by a growing amount. The readings do not move; only the spacing
        between them changes.
        """
        if true_duration <= 0 or not self.levels:
            return self
        return Envelope(self.levels, true_duration / len(self.levels))


class Lanes(NamedTuple):
    """The three lanes measured over one side, sharing a grid."""

    full: Envelope
    peak: Envelope
    band: Envelope

    @property
    def window(self) -> float:
        return self.full.window

    def rescaled(self, true_duration: float) -> Lanes:
        return Lanes(*(e.rescaled(true_duration) for e in self))


# Two per cent. The predecessor allowed ten, which let through an envelope
# measured at 44.1 kHz matched against audio at 48 kHz - a ratio of 0.919, well
# inside the tolerance and wrong by a growing amount across the side. Measured
# across 29 real sides the worst honest deviation is 0.072 per cent, so two per
# cent is a 28-fold margin over anything real while rejecting every sample-rate
# mismatch (44.1/48 is 8.1 per cent out; 48/96 is 50). See ADR-014.
WINDOW_COUNT_SLACK = 0.02


def window_count_is_plausible(
    windows: int, duration: float, window: float, slack: float = WINDOW_COUNT_SLACK
) -> bool:
    """Does this many readings match a side of this length?

    An envelope that does not line up with its audio is worse than no envelope,
    because every cut derived from it is confidently wrong, and wrong by an
    amount that grows along the side. The check is a ratio, not a difference, so
    it holds for a thirty-second re-rip and a twenty-two-minute side alike.
    """
    if duration <= 0 or window <= 0:
        return False
    expected = duration / window
    return expected * (1 - slack) <= windows <= expected * (1 + slack)


def decode(blob: bytes) -> Lanes:
    """Read a cache file into three envelopes.

    Refuses anything it does not recognise rather than guessing. A truncated or
    foreign file read as an envelope produces plausible-looking decibels.
    """
    if len(blob) < HEADER_BYTES or blob[:4] != MAGIC:
        raise FormatError("not an envelope file")
    version = int.from_bytes(blob[4:8], "little")
    if version != VERSION:
        raise FormatError(f"envelope version {version}; this reads {VERSION}")

    n = int.from_bytes(blob[8:12], "little")
    window_ms = int.from_bytes(blob[12:16], "little")
    if window_ms <= 0:
        raise FormatError("envelope declares a non-positive window")

    expected = HEADER_BYTES + 3 * n
    if len(blob) != expected:
        raise FormatError(
            f"envelope declares {n} windows over 3 lanes "
            f"({expected} bytes) but is {len(blob)} bytes"
        )

    window = window_ms / 1000.0
    lanes = []
    for i in range(3):
        start = HEADER_BYTES + i * n
        raw = blob[start : start + n]
        lanes.append(Envelope(tuple(deq_db(v) for v in raw), window))
    return Lanes(*lanes)


def encode(lanes: Lanes) -> bytes:
    """Write three envelopes to a cache file."""
    n = len(lanes.full)
    if not (len(lanes.peak) == len(lanes.band) == n):
        raise FormatError("lanes must be the same length")
    if n == 0:
        raise FormatError("refusing to write an empty envelope")

    window_ms = round(lanes.window * 1000)
    out = bytearray(MAGIC)
    out += VERSION.to_bytes(4, "little")
    out += n.to_bytes(4, "little")
    out += window_ms.to_bytes(4, "little")
    for lane in lanes:
        out += bytes(q_db(v) for v in lane.levels)
    return bytes(out)


def from_readings(
    full: list[float], peak: list[float], band: list[float], window: float
) -> Lanes:
    """Build lanes from raw measurements, trimming to the shortest.

    A measuring pass can return lanes that differ by a window at the end. The
    shortest is the one every lane actually covers.
    """
    n = min(len(full), len(peak), len(band))
    if n == 0:
        raise ValueError("no readings")
    return Lanes(
        Envelope(tuple(full[:n]), window),
        Envelope(tuple(peak[:n]), window),
        Envelope(tuple(band[:n]), window),
    )


def db_of_rms(rms: float, full_scale: float = 1.0) -> float:
    """Decibels relative to full scale, with a floor instead of -inf."""
    if rms <= 0 or full_scale <= 0:
        return DB_MIN
    return max(DB_MIN, 20.0 * math.log10(rms / full_scale))
