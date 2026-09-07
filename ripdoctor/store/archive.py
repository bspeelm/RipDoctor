"""Putting a record away: verify it arrived, then clear what is safe to clear.

The gate is the whole point. Twenty minutes a side is not recoverable from a
mistake here, so nothing is removed until the record is provably somewhere else
and every side has been read back from where it now lives.

Truncated captures are re-encoded on the way rather than copied. A stream ended
with a signal has no length in its header, and archiving it as-is preserves that
for as long as the file exists - the archive is the copy that has to still make
sense in five years.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ripdoctor.audio.ffprobe import true_duration
from ripdoctor.audio.runner import Runner
from ripdoctor.audio.split import verify
from ripdoctor.integrations.tagger import locate
from ripdoctor.store import cache as C
from ripdoctor.store.files import Layout

# What is cleared once the record is safe: the cut tracks, the tick clips and
# the measurements. All of them are derived, and all of them are large.
REMOVABLE = ("review", "clips", "cache")


class NotReady(Exception):
    """The record is not provably in the library yet."""


@dataclass(frozen=True, slots=True)
class Side:
    name: str
    bytes: int
    valid: bool | None  # None when it was not read

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "bytes": self.bytes, "valid": self.valid}


def removable(layout: Layout, slug: str) -> list[Path]:
    return [
        layout.review_dir(slug),
        layout.clips_dir(slug),
        C.dir_for(layout, slug),
    ]


def survey(
    runner: Runner,
    layout: Layout,
    library: str,
    slug: str,
    artist: str,
    album: str,
    expected: int,
    *,
    read: bool = False,
) -> dict[str, Any]:
    """What archiving would do, and whether it is allowed to.

    `read` decodes every side, which takes seconds each - worth it before the
    irreversible step, not worth it for a button's tooltip.
    """
    where, count = locate(library, artist, album)
    source = layout.raw / slug
    sides = []
    if source.is_dir():
        for p in sorted(source.glob("side-*.flac")):
            sides.append(
                Side(p.name, p.stat().st_size, verify(runner, str(p)) if read else None)
            )
    ready = where is not None and count >= expected and bool(sides)
    why = ""
    if not sides:
        why = f"no sides in raw for {slug}"
    elif where is None:
        why = "the record is not in the library yet"
    elif count < expected:
        why = f"only {count} of {expected} tracks are in the library"
    return {
        "ready": ready,
        "why": why,
        "library_path": None if where is None else str(where),
        "library_tracks": count,
        "expected": expected,
        "will_archive_to": str(layout.archive / slug),
        "sides": [s.as_dict() for s in sides],
        "will_remove": [str(p) for p in removable(layout, slug) if p.exists()],
    }


def repair_argv(source: str, dest: str) -> list[str]:
    """Re-encode a side whose header never got its length."""
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        dest,
    ]


def put_away(
    runner: Runner,
    layout: Layout,
    slug: str,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Copy every side into the archive, read it back, then clear the rest."""
    source = layout.raw / slug
    if not source.is_dir():
        raise NotReady(f"nothing in raw for {slug}")
    dest = layout.archive / slug
    dest.mkdir(parents=True, exist_ok=True)

    def say(what: str, detail: str) -> None:
        if progress:
            progress(what, detail)

    archived, notes = [], []
    for side in sorted(source.glob("side-*.flac")):
        target = dest / side.name
        if target.exists():
            raise NotReady(f"{target} already exists; move it first")
        if verify(runner, str(side)):
            say(side.name, "copying")
            shutil.copy2(side, target)
        else:
            # Not a failure: it is what a capture ended with a signal looks
            # like, and re-encoding is how it stops being that.
            say(side.name, "re-encoding")
            runner.run(repair_argv(str(side), str(target)), timeout=3600).require()
            notes.append(f"{side.name} was truncated and has been re-encoded")

        seconds = true_duration(runner, str(target))
        if seconds <= 0 or not verify(runner, str(target)):
            target.unlink(missing_ok=True)
            raise NotReady(f"{side.name} did not read back from the archive")
        archived.append(
            {
                "side": side.name,
                "bytes": target.stat().st_size,
                "seconds": round(seconds, 2),
            }
        )

    if not archived:
        raise NotReady(f"no sides in raw for {slug}")

    # Only now, with every side read back from where it will live.
    removed = []
    for directory in (source, *removable(layout, slug)):
        if directory.is_dir():
            shutil.rmtree(directory)
            removed.append(str(directory))
    return {
        "archive_dir": str(dest),
        "archived": archived,
        "notes": notes,
        "removed": removed,
    }
