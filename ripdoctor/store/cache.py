"""Prepared sides: the envelope and a seekable preview, built once and kept.

Two facts drive this.

A capture is a truncated stream. The header is never backfilled, so the file
reports no duration, and a browser's audio element gets an infinite one and
refuses to seek. Duration is therefore measured by decoding, and the browser is
given a re-encoded preview rather than the capture.

Measuring the envelope is expensive - seconds per side, per lane - and every
view of a record needs it. So it is written next to the preview and keyed to the
source file, which means a re-rip invalidates it without anyone remembering to.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ripdoctor.audio import astats
from ripdoctor.audio.ffprobe import sample_rate, true_duration
from ripdoctor.audio.runner import Runner
from ripdoctor.core.envelope import Lanes, decode, encode, window_count_is_plausible
from ripdoctor.core.naming import token
from ripdoctor.store.files import Layout, write_json
from ripdoctor.store.safety import under

WINDOW = astats.WINDOW_MS / 1000.0

# One folder per record under the cache root, and one set of files per side.
ENVELOPE = "{side}.env"
PREVIEW = "{side}.opus"
META = "{side}.meta.json"

# Enough to hear a click, small enough that a browser on the network can seek
# around a twenty-minute side without waiting for it.
PREVIEW_BITRATE = "128k"


class StillRecording(Exception):
    """The side is still being written; measuring it now is meaningless."""


class NotPrepared(Exception):
    """Nothing has been built for this side yet."""


@dataclass(frozen=True, slots=True)
class Prepared:
    """What was built for one side, and what it was built from."""

    slug: str
    side: str
    duration: float
    windows: int
    window: float
    rate: int
    stamp: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "side": self.side,
            "duration": round(self.duration, 3),
            "windows": self.windows,
            "window_ms": round(self.window * 1000),
            "rate": self.rate,
            "stamp": self.stamp,
            "source": self.source,
        }


def stamp_of(path: str | Path) -> str:
    """What the source looked like when this was built.

    Size and modification time together: a re-rip of the same side changes at
    least one, and the cache invalidates itself without anyone remembering to.
    """
    st = Path(path).stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def is_growing(
    path: str | Path, dwell: float = 1.0, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Is this file still being written?

    Two sizes a beat apart. Our own captures are written under a name no side
    scan can see, so this is not about them - it is about a file arriving over
    the network, which looks exactly like a side and is not one yet. Unlike
    looking for a process it also works inside a container, which has its own
    PID namespace and cannot see the host's.
    """
    try:
        first = Path(path).stat().st_size
    except OSError:
        return False
    sleep(dwell)
    try:
        return Path(path).stat().st_size != first
    except OSError:
        return False


def dir_for(layout: Layout, slug: str) -> Path:
    where = under(layout.cache, token(slug))
    where.mkdir(parents=True, exist_ok=True)
    return where


def _at(layout: Layout, slug: str, side: str, pattern: str) -> Path:
    return dir_for(layout, slug) / pattern.format(side=token(side))


def envelope_path(layout: Layout, slug: str, side: str) -> Path:
    return _at(layout, slug, side, ENVELOPE)


def preview_path(layout: Layout, slug: str, side: str) -> Path:
    return _at(layout, slug, side, PREVIEW)


def meta_path(layout: Layout, slug: str, side: str) -> Path:
    return _at(layout, slug, side, META)


def prepared(layout: Layout, slug: str, side: str) -> Prepared | None:
    """What is cached for this side, if it still describes the file on disk."""
    where = meta_path(layout, slug, side)
    try:
        data = json.loads(where.read_text())
        source = layout.side_file(slug, side)
        if data.get("stamp") != stamp_of(source):
            return None
    except (OSError, ValueError, FileNotFoundError):
        return None
    if not (
        envelope_path(layout, slug, side).is_file()
        and preview_path(layout, slug, side).is_file()
    ):
        # The metadata outliving what it describes is how a prepared side turns
        # into a 404 halfway through a page rather than a rebuild.
        return None
    return Prepared(
        slug=str(data["slug"]),
        side=str(data["side"]),
        duration=float(data["duration"]),
        windows=int(data["windows"]),
        window=float(data["window_ms"]) / 1000.0,
        rate=int(data["rate"]),
        stamp=str(data["stamp"]),
        source=str(data.get("source", "")),
    )


def preview_argv(source: str, dest: str) -> list[str]:
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-c:a",
        "libopus",
        "-b:a",
        PREVIEW_BITRATE,
        "-vbr",
        "on",
        "-f",
        "ogg",
        dest,
    ]


def build(
    runner: Runner,
    layout: Layout,
    slug: str,
    side: str,
    *,
    progress: Callable[[str], None] | None = None,
    dwell: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Prepared:
    """Measure and encode one side. Idempotent: a prepared side is returned."""
    already = prepared(layout, slug, side)
    if already:
        return already

    source = layout.side_file(slug, side)
    if is_growing(source, dwell, sleep):
        raise StillRecording(f"side {side} is still being written")

    def say(what: str) -> None:
        if progress:
            progress(what)

    say("measuring duration")
    duration = true_duration(runner, str(source))
    rate = sample_rate(runner, str(source))

    say("measuring the envelope")
    lanes = astats.lanes(runner, str(source), rate=rate)
    windows = len(lanes.full)
    if not windows:
        raise RuntimeError(f"no windows were measured for {source.name}")
    if not window_count_is_plausible(windows, duration, lanes.window):
        # An envelope that does not line up with the audio is worse than none:
        # every cut taken from it is wrong, and nothing about the plan looks
        # wrong until somebody listens. ADR-014.
        raise RuntimeError(
            f"{source.name}: {windows} windows for a {duration:.1f}s side - "
            "refusing to cache an envelope that will not line up with the audio"
        )

    # Written beside the target and moved into place: a half-written envelope
    # under the real name is one the next view reads as a whole side.
    target = envelope_path(layout, slug, side)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(encode(lanes))
    tmp.replace(target)

    say("encoding the preview")
    target = preview_path(layout, slug, side)
    tmp = target.with_name(target.name + ".tmp")
    runner.run(preview_argv(str(source), str(tmp)), timeout=1800).require()
    if not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"the preview encode produced nothing for {source.name}")
    tmp.replace(target)

    built = Prepared(
        slug=slug,
        side=side,
        duration=duration,
        windows=windows,
        window=lanes.window,
        rate=rate,
        stamp=stamp_of(source),
        source=source.name,
    )
    write_json(meta_path(layout, slug, side), built.as_dict())
    say("done")
    return built


def lanes_of(layout: Layout, slug: str, side: str) -> Lanes:
    """The three envelopes for a prepared side."""
    where = envelope_path(layout, slug, side)
    if not where.is_file():
        raise NotPrepared(f"nothing is prepared for {slug} side {side}")
    return decode(where.read_bytes())


def forget(layout: Layout, slug: str, side: str | None = None) -> int:
    """Discard what is cached, so the next view rebuilds it."""
    where = dir_for(layout, slug)
    names = (
        [p.format(side=token(side)) for p in (ENVELOPE, PREVIEW, META)]
        if side
        else [p.name for p in where.iterdir()]
    )
    gone = 0
    for name in names:
        target = where / name
        if target.is_file():
            target.unlink()
            gone += 1
    return gone
