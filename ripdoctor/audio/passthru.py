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

from ripdoctor.audio.capture import Format, check_device
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


def tail_argv(wav: str | Path) -> list[str]:
    """Listen to a capture in progress, from the file it is writing.

    `-re` matters: without it ffmpeg reads the file as fast as the disk allows,
    empties it in a second and then reports end of stream, so what a listener
    hears is the last few seconds at high speed followed by silence.
    """
    return [
        "ffmpeg",
        "-v",
        "error",
        "-re",
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
