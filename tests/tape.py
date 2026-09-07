"""A WAV file being written, one block at a time.

The live meter reads the tail of the file the recorder is still writing, so
testing it needs a file that grows. This is that file: a real header, real PCM,
and a `sleep` that appends the next block instead of waiting.

Blocks are one meter block long rather than one second. The loop takes its time
from an injected clock, not from the file, so a test can record twenty minutes
of vinyl in a few megabytes.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

from ripdoctor.core.meter import BLOCK

WIDTHS = {"S16_LE": 2, "S24_3LE": 3, "S32_LE": 4}


def header(rate: int = 48000, channels: int = 2, bits: int = 24) -> bytes:
    """A 44-byte WAV header with the sizes left unfilled, as a recorder writes
    it: the length is not known until the side ends."""
    block_align = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 0)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, bits
        )
        + b"data"
        + struct.pack("<I", 0)
    )


def block(
    hz: float | None, rate: int, width: int, channels: int, amplitude: float
) -> bytes:
    """One meter block: a tone, or silence when `hz` is None."""
    if hz is None:
        return bytes(BLOCK * channels * width)
    full = (1 << (width * 8 - 1)) - 1
    out = bytearray()
    for i in range(BLOCK):
        v = int(amplitude * full * math.sin(2 * math.pi * hz * i / rate))
        for _ in range(channels):
            if width == 2:
                out += struct.pack("<h", v)
            elif width == 4:
                out += struct.pack("<i", v)
            else:
                out += (v & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


class Tape:
    """A capture in progress, and the clock that goes with it.

    `script` is one letter per poll: 'm' music, 'q' groove noise well under it,
    'x' nothing at all. When it runs out the last entry repeats, because a
    needle left in the run-out does not stop making run-out.
    """

    def __init__(
        self,
        path: Path,
        script: str,
        *,
        rate: int = 48000,
        channels: int = 2,
        bits: int = 24,
        interrupt_after: int | None = None,
    ) -> None:
        self.path = path
        self.script = script
        self.polls = 0
        self.clock = 0.0
        self.interrupt_after = interrupt_after
        width = bits // 8
        path.write_bytes(header(rate, channels, bits))
        kw = {"rate": rate, "width": width, "channels": channels}
        self.blocks = {
            "m": block(1500.0, amplitude=0.4, **kw),
            "q": block(1500.0, amplitude=0.004, **kw),
            "x": block(None, amplitude=0.0, **kw),
        }

    def sleep(self, seconds: float) -> None:
        """Stand in for a second passing: the clock moves, the file grows."""
        self.clock += seconds
        if self.interrupt_after is not None and self.polls >= self.interrupt_after:
            raise KeyboardInterrupt
        letter = self.script[min(self.polls, len(self.script) - 1)]
        with self.path.open("ab") as f:
            f.write(self.blocks[letter])
        self.polls += 1

    def now(self) -> float:
        return self.clock
