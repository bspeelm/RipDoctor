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

from collections.abc import Iterator
from pathlib import Path

from ripdoctor.audio.capture import HEADER_BYTES, Format, check_device
from ripdoctor.audio.runner import Process, Runner

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


# How far behind the write head a listener starts. Without a cushion the reader
# begins where the writer is and the two run at the same speed for the whole
# side, so the reader sits permanently at the end of the file: any jitter in
# either direction is an end-of-stream, and an Opus decoder resyncing after one
# is a burst of loud static. Fifteen seconds absorbs that and the drift between
# ffmpeg's pacing and the sound card's own clock - about a second over a
# twenty-minute side. Being fifteen seconds behind costs nothing: this is for
# hearing that the side sounds right, and cueing a needle uses the device
# directly with no lag at all.
LAG = 15.0


def tail_start(wav: str | Path, fmt: Format, lag: float = LAG) -> float:
    """Where in the capture a listener should join, given how much exists."""
    try:
        written = Path(wav).stat().st_size - HEADER_BYTES
    except OSError:
        return 0.0
    frame = fmt.channels * fmt.width
    if frame <= 0 or fmt.rate <= 0 or written <= 0:
        return 0.0
    return max(0.0, written / frame / fmt.rate - lag)


def tail_argv(wav: str | Path, start: float = 0.0) -> list[str]:
    """Listen to a capture in progress, from the file it is writing.

    `-re` matters: without it ffmpeg reads the file as fast as the disk allows,
    empties it in a second and then reports end of stream, so what a listener
    hears is the last few seconds at high speed followed by silence.

    `-ss` matters for the opposite reason - see LAG.
    """
    return [
        "ffmpeg",
        "-v",
        "error",
        "-re",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-i",
        str(wav),
        *_encode(),
    ]


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
