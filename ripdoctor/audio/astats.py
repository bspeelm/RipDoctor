"""Windowed levels from ffmpeg, which is what makes analysis interactive.

The pure-Python equivalent in audio.decode is the oracle these are checked
against; this is the path that actually runs.
"""

from __future__ import annotations

import math
import re

from ripdoctor.audio.ffprobe import sample_rate
from ripdoctor.audio.runner import Runner
from ripdoctor.core.envelope import DB_MIN, Lanes, from_readings

WINDOW_MS = 50
BAND_LO_HZ, BAND_HI_HZ = 1000, 3000

RMS, PEAK = "RMS_level", "Peak_level"

# One `ametadata=print` writer, and values taken in order.
#
# An earlier form chained two of these, one per key, both writing to the same
# pipe. Their writes interleaved and spliced two frame headers into one line.
# The parser then sized its output array from the parsed frame index, tried to
# allocate 18.9 billion entries, reached 26 GB and was OOM-killed by the cgroup.
# systemd restarted the service, and that killed the capture running inside it -
# so a corrupt line in an analysis pass silently aborted a rip in progress.
#
# Hence both halves of the fix: a single writer, and never sizing an allocation
# from a parsed index. Frames are counted by appending, never by indexing.
_FRAME = re.compile(r"^frame:\d+\b")
_KV = re.compile(r"^lavfi\.astats\.Overall\.([A-Za-z_]+)=(.+)$")


def _reading(raw: str) -> float:
    """One level, or the floor.

    `-nan` is what ffmpeg prints for a window of digital silence, and it is a
    real reading rather than a fault. Catching only ValueError does not handle
    it: float("-nan") succeeds and returns a NaN, which then compares false
    against everything and gives an undefined order when the lane is sorted for
    a percentile. The predecessor was saved from this by passing every value
    through a quantiser that happened to test for NaN. ADR-027.
    """
    try:
        v = float(raw)
    except ValueError:
        return DB_MIN
    return v if math.isfinite(v) else DB_MIN


def parse(text: str, keys: tuple[str, ...]) -> dict[str, list[float]]:
    """Levels per window, in order, from `ametadata=print` output."""
    out: dict[str, list[float]] = {k: [] for k in keys}
    current: dict[str, float] = {}
    started = False

    def flush() -> None:
        # Every frame contributes exactly one reading per lane, even one whose
        # value was unreadable. Skipping it instead would shorten the lane and
        # shift everything after it by a window - a corrupt line is exactly what
        # the interleaving defect produced, so this is not hypothetical.
        for k in keys:
            out[k].append(current.get(k, DB_MIN))
        current.clear()

    for line in text.splitlines():
        if _FRAME.match(line):
            if started:
                flush()
            started = True
            continue
        m = _KV.match(line)
        if not m or m.group(1) not in keys:
            continue
        current[m.group(1)] = _reading(m.group(2))
    if started:
        flush()
    return out


def _chain(window_samples: int, prefix: list[str]) -> str:
    return ",".join(
        [
            *prefix,
            f"asetnsamples=n={window_samples}",
            # measure_perchannel=none keeps only the Overall keys, a third of
            # the output and a third of the parsing.
            "astats=metadata=1:reset=1:measure_perchannel=none",
            "ametadata=print:file=-",
        ]
    )


def _measure(
    runner: Runner,
    path: str,
    prefix: list[str],
    keys: tuple[str, ...],
    rate: int,
    timeout: float,
) -> dict[str, list[float]]:
    n = int(rate * WINDOW_MS / 1000)
    r = runner.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-af",
            _chain(n, prefix),
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    ).require()
    return parse(r.text, keys)


def lanes(
    runner: Runner,
    path: str,
    *,
    rate: int | None = None,
    timeout: float = 900.0,
) -> Lanes:
    """The three lanes for one side: full RMS, peak, and 1-3 kHz RMS.

    Two passes, because the band lane needs a filter the other two must not see.
    The rate is measured once and passed to both, so the two grids cannot end up
    describing different window lengths.
    """
    if rate is None:
        rate = sample_rate(runner, path)

    wide = _measure(runner, path, [], (RMS, PEAK), rate, timeout)
    band = _measure(
        runner,
        path,
        [f"highpass=f={BAND_LO_HZ}", f"lowpass=f={BAND_HI_HZ}"],
        (RMS,),
        rate,
        timeout,
    )
    return from_readings(wide[RMS], wide[PEAK], band[RMS], WINDOW_MS / 1000.0)
