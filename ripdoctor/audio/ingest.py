"""Bringing an arbitrary audio file to the shape a side has. ADR-053."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ripdoctor.audio.runner import Runner
from ripdoctor.audio.split import CODEC

# What a side may be. Anything wider becomes a track the library plays back
# through two speakers with four channels missing, which is found out late.
MAX_CHANNELS = 2


class NotAudio(Exception):
    """The file is not something a side can be made from, and why."""


@dataclass(frozen=True, slots=True)
class Probed:
    codec: str
    channels: int
    rate: int

    def as_dict(self) -> dict[str, object]:
        return {"codec": self.codec, "channels": self.channels, "rate": self.rate}


def probe_argv(path: str) -> list[str]:
    """Ask about the first audio stream, and only about it.

    One call rather than three: whether there is audio at all, how wide it is
    and what rate it runs at are all wanted before anything is encoded.
    """
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,channels,sample_rate",
        "-of",
        "json",
        path,
    ]


def probe(runner: Runner, path: str, timeout: float = 120.0) -> Probed:
    """What the file is, or NotAudio with something a person can act on."""
    result = runner.run(probe_argv(path), timeout=timeout)
    try:
        streams = json.loads(result.text or "{}").get("streams") or []
    except ValueError as e:
        raise NotAudio(f"could not read {path}") from e
    if not streams:
        raise NotAudio("that file has no audio in it")
    first = streams[0]
    channels = int(first.get("channels") or 0)
    if channels > MAX_CHANNELS:
        raise NotAudio(f"that file has {channels} channels and this is a stereo tool")
    if channels < 1:
        raise NotAudio("that file reports no channels")
    return Probed(
        codec=str(first.get("codec_name") or ""),
        channels=channels,
        rate=int(first.get("sample_rate") or 0),
    )


def normalise_argv(source: str, dest: str) -> list[str]:
    """Re-encode to the shape a side has. Never a rename, whatever it is.

    `-map 0:a:0` is load-bearing rather than tidy: an embedded cover otherwise
    becomes a stream of its own, and the placed side's duration then probes as
    zero - a fault that surfaces three steps from its cause. ADR-053.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source,
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        *CODEC,
        dest,
    ]


def normalise(runner: Runner, source: str, dest: str, timeout: float = 3600.0) -> None:
    """Encode, or raise with what the encoder said."""
    runner.run(normalise_argv(source, dest), timeout=timeout).require()
