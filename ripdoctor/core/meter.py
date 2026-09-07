"""Levels for one block of PCM: the live meter. docs/method.md."""

from __future__ import annotations

import cmath
import math
import struct
from dataclasses import dataclass

# Frames per reading. Large enough that the 1-3 kHz band has bins to sit in at
# any supported rate, small enough to stay interactive.
BLOCK = 4096

FLOOR_DB = -120.0
BAND_LO_HZ = 1000.0
BAND_HI_HZ = 3000.0
MIN_FRAMES = 64


@dataclass(frozen=True, slots=True)
class Levels:
    """What the stylus is doing, in three numbers."""

    full: float
    band: float
    peak: float


def samples(raw: bytes, width: int) -> list[int]:
    """Signed ints from interleaved PCM of 2, 3 or 4 bytes per sample.

    `struct` and `array` have 16- and 32-bit members and nothing three bytes
    wide, so 24-bit is assembled by hand. `int.from_bytes` per sample would be
    clearer and is far too slow at four thousand frames a poll; slicing the
    three byte planes and zipping them keeps the loop in C.
    """
    if width == 2:
        return list(struct.unpack(f"<{len(raw) // 2}h", raw))
    if width == 4:
        return list(struct.unpack(f"<{len(raw) // 4}i", raw))
    if width != 3:
        raise ValueError(f"unsupported sample width: {width} bytes")
    lo, mid, hi = raw[0::3], raw[1::3], raw[2::3]
    return [
        a | (b << 8) | ((c - 256) << 16 if c > 127 else c << 16)
        for a, b, c in zip(lo, mid, hi, strict=True)
    ]


def fft(a: list[complex]) -> list[complex]:
    """In-place iterative radix-2 FFT. `len(a)` must be a power of two."""
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        step = cmath.exp(-2j * math.pi / size)
        half = size >> 1
        for i in range(0, n, size):
            w = 1 + 0j
            for k in range(i, i + half):
                u, v = a[k], a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= step
        size <<= 1
    return a


def levels_of(raw: bytes, rate: int, channels: int, width: int) -> Levels | None:
    """Full-band, 1-3 kHz and peak dB for one block of interleaved PCM.

    Returns None for a block too short to measure rather than a wrong number.
    """
    if channels < 1 or rate <= 0:
        raise ValueError("rate and channels must be positive")

    frames = len(raw) // (channels * width)
    if frames < MIN_FRAMES:
        return None

    values = samples(raw[: frames * channels * width], width)

    # Full scale follows the sample width, or a 24-bit capture reads 48 dB hot.
    full_scale = float(1 << (width * 8 - 1))

    mono = [0.0] * frames
    peak = 0
    for i in range(frames):
        frame = values[i * channels : (i + 1) * channels]
        mono[i] = sum(frame) / float(channels)
        loudest = max(abs(x) for x in frame)
        if loudest > peak:
            peak = loudest

    energy = sum(v * v for v in mono)
    full = (
        20 * math.log10(math.sqrt(energy / frames) / full_scale)
        if energy > 0
        else FLOOR_DB
    )

    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / frames) for i in range(frames)]
    spectrum = fft([complex(mono[i] * window[i], 0.0) for i in range(frames)])
    # Summing POWER across bins is corrected by the window's mean square, not by
    # its coherent gain squared. For Hann those are 0.375 and 0.25, so the wrong
    # one reads 1.76 dB hot at every level. ADR-024.
    window_power = sum(v * v for v in window) / frames

    # Bin width follows the real rate: at 96 kHz a hardcoded 48000 would measure
    # 2-6 kHz and report it as 1-3.
    bin_hz = rate / frames
    lo, hi = int(BAND_LO_HZ / bin_hz), int(BAND_HI_HZ / bin_hz)
    hi = min(hi, frames // 2)

    power = (
        sum(abs(spectrum[k]) ** 2 for k in range(lo, hi))
        / (frames * frames)
        * 2
        / window_power
    )
    band = 10 * math.log10(power / (full_scale**2)) if power > 0 else FLOOR_DB
    peak_db = 20 * math.log10(peak / full_scale) if peak else FLOOR_DB

    return Levels(round(full, 1), round(band, 1), round(peak_db, 1))


# The four states the meter is calibrated against, as a judgement rather than
# three numbers. See docs/method.md.
ARM_FLOOR = -60.0
MUSIC_PEAK = -30.0
SIGNAL_PEAK = -40.0
DEAD_BAND = -70.0
DEAD_FULL = -70.0


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a short test capture found."""

    ok: bool
    summary: str
    full_rms: float
    full_peak: float
    band_rms: float


def verdict(full_rms: float, full_peak: float, band_rms: float) -> Verdict:
    """Judge a test capture, in the order that distinguishes the cases.

    Music is checked first, then the wrong-input case - signal present but
    nothing musical in the band lane - then an empty room. Checking the floor
    first would call a wrong input "no signal" and send somebody to look at the
    cable instead of the input selector.
    """
    if band_rms > ARM_FLOOR and full_peak > MUSIC_PEAK:
        summary, ok = "music - this is what a good capture looks like", True
    elif full_peak > SIGNAL_PEAK and band_rms < DEAD_BAND:
        summary, ok = (
            "signal, but nothing musical in 1-3 kHz - this is what the wrong "
            "input sounds like",
            False,
        )
    elif full_rms < DEAD_FULL:
        summary, ok = "nothing but the noise floor - no needle, or no signal", False
    else:
        summary, ok = "quiet - groove noise, or a very quiet passage", False
    return Verdict(ok, summary, full_rms, full_peak, band_rms)
