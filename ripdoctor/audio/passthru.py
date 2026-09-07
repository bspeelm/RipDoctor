"""Listening to the input, live.

The only way to hear what the stylus is doing while you cue it. The meter says
there is signal and the probe says it is musical; neither tells you the arm is
tracking or that the record is the one you meant.

One process, not two. ffmpeg reads ALSA directly rather than being fed by
arecord through a pipe, which removes the plumbing and the half of it that could
block. During a capture the device is already taken, so the growing file is read
instead - the same trick the meter uses.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

from ripdoctor.audio.capture import (
    HEADER_BYTES,
    CaptureError,
    Format,
    check_device,
)
from ripdoctor.audio.runner import Process, Runner


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


CHUNK = 8192

# Opus at a bitrate that is about listening, not archiving, with the smallest
# frame the encoder offers: latency here is the delay between the needle and
# the headphones, and anything above a fifth of a second feels broken.
BITRATE = "96k"
FRAME_MS = "10"


def _encode() -> list[str]:
    return [
        "-c:a",
        "libopus",
        "-b:a",
        BITRATE,
        "-frame_duration",
        FRAME_MS,
        "-application",
        "lowdelay",
        "-f",
        "ogg",
        "-",
    ]


def device_argv(device: str, fmt: Format) -> list[str]:
    """Listen to the card itself, when nothing else has it."""
    check_device(device)
    return [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "alsa",
        "-sample_rate",
        str(int(fmt.rate)),
        "-channels",
        str(int(fmt.channels)),
        "-i",
        device,
        *_encode(),
    ]


# How far behind the write head a listener starts. Small, because the reader
# below waits for the writer rather than racing it: this is a head start, not a
# safety margin. Half a second is enough to have something to play at the moment
# the stream opens.
LAG = 0.5

# How long the reader waits at the end of the file before deciding the capture
# has finished. A capture that has stopped will never produce another byte, and
# a listener held open forever on a side that ended is a process nobody closes.
IDLE_LIMIT = 10.0

# How long to wait when there is nothing new. Short enough that the delay stays
# imperceptible, long enough not to spin on the disk.
POLL = 0.05

# ffmpeg's names for what arecord writes. A capture fed to the wrong one is
# noise at the wrong speed - the same mistake, one layer down, that reading a
# 24-bit file as 16 makes.
RAW = {"S16_LE": "s16le", "S24_3LE": "s24le", "S32_LE": "s32le"}


def pipe_argv(fmt: Format) -> list[str]:
    """Encode raw PCM arriving on standard input.

    The format has to be declared: there is no header on a pipe, and ffmpeg
    guessing would guess wrong.
    """
    if fmt.sample_format not in RAW:
        raise CaptureError(f"cannot stream {fmt.sample_format}")
    return [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        RAW[fmt.sample_format],
        "-ar",
        str(int(fmt.rate)),
        "-ac",
        str(int(fmt.channels)),
        "-i",
        "-",
        *_encode(),
    ]


def tail_start(wav: str | Path, fmt: Format, lag: float = LAG) -> int:
    """The byte to start reading at: `lag` behind the end, on a frame boundary.

    Starting mid-frame swaps the channels and shifts every sample by a byte,
    which sounds like noise rather than like a mistake.
    """
    frame = fmt.channels * fmt.width
    try:
        written = max(0, Path(wav).stat().st_size - HEADER_BYTES)
    except OSError:
        return HEADER_BYTES
    behind = int(lag * fmt.rate) * frame
    return HEADER_BYTES + max(0, (written - behind) // frame * frame)


def tail(
    wav: str | Path,
    fmt: Format,
    *,
    lag: float = LAG,
    idle_limit: float = IDLE_LIMIT,
    chunk: int = CHUNK,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Iterator[bytes]:
    """Raw samples from a file that is still being written.

    The reader waits at the end of the file instead of stopping there, which is
    the whole point. ffmpeg reading the file directly treats end-of-file as end
    of stream, so a monitor that keeps pace with the writer ends every time it
    catches up - and a decoder resyncing after that sounds like loud static on a
    recording that is perfect.
    """
    position = tail_start(wav, fmt, lag)
    quiet_since = now()
    with Path(wav).open("rb") as f:
        f.seek(position)
        while True:
            block = f.read(chunk)
            if block:
                quiet_since = now()
                yield block
                continue
            if now() - quiet_since >= idle_limit:
                return
            sleep(POLL)


def follow(
    runner: Runner,
    wav: str | Path,
    fmt: Format,
    *,
    lag: float = LAG,
    spawn: Callable[[Callable[[], None]], None] | None = None,
    **kw: object,
) -> Iterator[bytes]:
    """Encoded audio from a capture in progress, without racing it.

    One thread walks the file and feeds the encoder; this generator hands back
    what the encoder produces. Closing it - which is what a browser navigating
    away does - stops both.
    """
    process: Process = runner.start(pipe_argv(fmt), reading=True, writing=True)
    sink, source = process.stdin, process.stdout
    if sink is None or source is None:  # pragma: no cover - a runner that cannot
        return
    stopping = threading.Event()

    def feed() -> None:
        try:
            for block in tail(wav, fmt, lag=lag, **kw):  # type: ignore[arg-type]
                if stopping.is_set():
                    break
                sink.write(block)
                sink.flush()
        except (OSError, ValueError):
            # The encoder went away, or the capture file did. Either way there
            # is nothing left to feed and the reader below will see the end.
            pass
        finally:
            with suppress(OSError, ValueError):
                sink.close()

    (spawn or _thread)(feed)
    try:
        while True:
            block = source.read(CHUNK)
            if not block:
                return
            yield block
    finally:
        stopping.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def stream(runner: Runner, argv: list[str], chunk: int = CHUNK) -> Iterator[bytes]:
    """Chunks of encoded audio, until the listener goes away.

    The process is stopped when the generator is closed, which is what happens
    when a browser navigates away mid-stream. Without that a listener who
    closed a tab would leave ffmpeg holding the sound card.
    """
    process: Process = runner.start(argv, reading=True)
    out = process.stdout
    if out is None:  # pragma: no cover - a Runner that cannot be read from
        return
    try:
        while True:
            block = out.read(chunk)
            if not block:
                return
            yield block
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
