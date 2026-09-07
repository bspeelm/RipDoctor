"""The slow, obvious envelope. A test oracle, never a production path.

ADR-005: the fast path measures with ffmpeg's own filters, which is what makes
analysis interactive. This computes the same thing in a loop anyone can read,
so the two can be compared. If they disagree, one of them is wrong and the
argument is settled by arithmetic rather than by preference.
"""

from __future__ import annotations

import array
import math

from ripdoctor.audio.runner import Runner
from ripdoctor.core.envelope import DB_MIN, Envelope

ORACLE_RATE = 8000
FULL_SCALE = 32768.0


def envelope_of_samples(
    samples: array.array[int] | list[int],
    rate: int,
    window: float,
    full_scale: float = FULL_SCALE,
) -> list[float]:
    """RMS per window, in decibels. Pure, and deliberately unclever.

    A trailing partial window is dropped rather than measured short, which
    would read quieter than the audio actually is and put a false gap at the
    end of every side.
    """
    n = int(rate * window)
    if n <= 0 or len(samples) < n:
        return []
    out = []
    for i in range(0, len(samples) - n + 1, n):
        total = 0
        for j in range(i, i + n):
            total += samples[j] * samples[j]
        rms = math.sqrt(total / n)
        out.append(20 * math.log10(rms / full_scale) if rms > 0 else DB_MIN)
    return out


def decode_mono(
    runner: Runner,
    path: str,
    rate: int = ORACLE_RATE,
    timeout: float = 900.0,
    band: tuple[int, int] | None = None,
) -> array.array[int]:
    """One channel of signed 16-bit samples at `rate`.

    `band` applies the same filter the fast path uses, so the band lane can be
    compared as well as the full one.
    """
    filters = ["-af", f"highpass=f={band[0]},lowpass=f={band[1]}"] if band else []
    result = runner.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-ac",
            "1",
            "-ar",
            str(rate),
            *filters,
            "-f",
            "s16le",
            "-",
        ],
        timeout=timeout,
    ).require()
    raw = result.stdout
    out = array.array("h")
    out.frombytes(raw[: len(raw) // 2 * 2])
    return out


def envelope(
    runner: Runner,
    path: str,
    *,
    window: float = 0.05,
    rate: int = ORACLE_RATE,
    band: tuple[int, int] | None = None,
) -> Envelope:
    samples = decode_mono(runner, path, rate=rate, band=band)
    return Envelope(tuple(envelope_of_samples(samples, rate, window)), window)
