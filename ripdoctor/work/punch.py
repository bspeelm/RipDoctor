"""Re-record one track and swap it into the library.

A side comes out fine and one track does not - a skip, a click, a passage the
stylus fought. Re-ripping the whole side to fix four minutes of it is absurd,
and re-cutting is not the answer either: the boundaries were already right, the
audio was dirty.

So: drop the needle before the bad track, record it again, replace that one
file. Three things make that safe rather than fiddly.

The boundaries are not typed, they are fitted. The saved cut already says where
the track starts and ends, and audio/align maps those times onto the new capture,
so the replacement begins and ends where the original did.

The tags come from the file being replaced. Not from the catalogue, not from the
plan - from the library file itself, cover included.

Nothing is deleted. The replaced file moves aside and the punch capture goes
with it, so a take that turns out worse is still recoverable.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ripdoctor.audio.align import fit_punch
from ripdoctor.audio.ffprobe import true_duration
from ripdoctor.audio.runner import Runner
from ripdoctor.audio.split import verify
from ripdoctor.core.naming import token
from ripdoctor.core.plan import Plan, SpecTrack
from ripdoctor.integrations.tagger import carry_tags, placements
from ripdoctor.store.files import Layout, read_spec

# A punch is not a side and must never be mistaken for one. `punch-7.flac` does
# not match `side-*.flac`, so the side listing, the album picker and the archive
# gate cannot see it. That invisibility is the whole reason for the separate
# name.
PUNCH = "punch-{number}.flac"
KEPT = "_punched"

# Shorter than this is not a track, and asking for it is a typo.
MIN_SECONDS = 1.0

# How far the cut may miss the length that was asked for before it is refused.
LENGTH_TOLERANCE = 0.10


class PunchError(Exception):
    """A punch could not be placed, or should not be."""


@dataclass(frozen=True, slots=True)
class Located:
    """Where the archived track lands inside the punch capture."""

    number: int
    title: str
    side: str
    old_start: float
    old_end: float
    start: float
    end: float
    seconds: float
    scale_assumed: bool
    r: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "side": self.side,
            "old_start": self.old_start,
            "old_end": self.old_end,
            "start": self.start,
            "end": self.end,
            "punch_seconds": self.seconds,
            "scale_assumed": self.scale_assumed,
            "r": round(self.r, 4),
        }


def capture_path(layout: Layout, slug: str, number: int) -> Path:
    return layout.raw / token(slug) / PUNCH.format(number=int(number))


def recorded(layout: Layout, slug: str, number: int) -> Path | None:
    where = capture_path(layout, slug, number)
    return where if where.is_file() else None


def kept_dir(layout: Layout, slug: str) -> Path:
    return layout.archive / token(slug) / KEPT


def _track(layout: Layout, slug: str, number: int) -> tuple[str, SpecTrack]:
    where = layout.spec_file(slug)
    if not where.is_file():
        raise PunchError(f"no saved cut for {slug}")
    for side in read_spec(where).sides:
        for track in side.tracks:
            if track.number == int(number):
                return side.letter, track
    raise PunchError(f"no track {number} in the saved cut for {slug}")


def library_file(plan: Plan, library: str, number: int) -> Path:
    """The file in the library this track was written as.

    Found through the same naming the import used, rather than by searching:
    if the two ever disagree the answer is to fix the naming, not to guess.
    """
    for placement in placements(plan, "", library):
        if placement.track.number == int(number):
            return placement.dest
    raise PunchError(f"track {number} is not in the plan")


def locate(runner: Runner, layout: Layout, slug: str, number: int) -> Located:
    """Fit the archived track's boundaries onto the punch capture."""
    letter, track = _track(layout, slug, number)
    if track.start is None or track.end is None:
        raise PunchError(f"track {number} has no saved boundaries to fit")
    capture = recorded(layout, slug, number)
    if capture is None:
        raise PunchError(f"no punch recorded for track {number} yet")
    old = layout.side_file(slug, letter)

    seconds = true_duration(runner, str(capture))
    transform, probes, assumed = fit_punch(
        runner, str(old), str(capture), track.start, track.end, seconds
    )
    start, end = transform.apply(track.start), transform.apply(track.end)
    if start < 0 or end > seconds or end <= start:
        raise PunchError(
            f"the track maps to {start:.2f}-{end:.2f}, outside the punch capture "
            f"(0-{seconds:.2f}). Record a longer punch, starting before the "
            "track and ending after it."
        )
    return Located(
        number=int(number),
        title=track.title,
        side=letter,
        old_start=track.start,
        old_end=track.end,
        start=round(start, 3),
        end=round(end, 3),
        seconds=round(seconds, 2),
        scale_assumed=assumed,
        r=min(p.r for p in probes),
    )


def cut_argv(source: str, start: float, end: float, dest: str) -> list[str]:
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        dest,
    ]


@dataclass(frozen=True, slots=True)
class Applied:
    number: int
    path: Path
    was: float
    now: float
    art_carried: bool
    kept: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "number": self.number,
            "path": str(self.path),
            "was_seconds": round(self.was, 2),
            "now_seconds": round(self.now, 2),
            "art_carried": self.art_carried,
            "kept": str(self.kept),
        }


def apply(
    runner: Runner,
    layout: Layout,
    plan: Plan,
    library: str,
    slug: str,
    number: int,
    start: float,
    end: float,
    *,
    now: Callable[[], float] = time.time,
) -> Applied:
    """Cut the punch and put it in the library in place of the old file.

    Cut, verify, measure, and only then touch the library. Nothing is removed
    until the replacement has been proved good - the same rule the cover art
    installer learned.
    """
    if end - start < MIN_SECONDS:
        raise PunchError(f"a {end - start:.2f}s track is not a track")
    capture = recorded(layout, slug, number)
    if capture is None:
        raise PunchError(f"no punch recorded for track {number}")
    seconds = true_duration(runner, str(capture))
    if start < 0 or end > seconds + 0.05:
        raise PunchError(
            f"{start:.2f}-{end:.2f} is outside the punch capture (0-{seconds:.2f})"
        )

    old = library_file(plan, library, number)
    if not old.is_file():
        raise PunchError(f"the library file for track {number} is missing: {old}")
    was = true_duration(runner, str(old))

    fresh = capture.with_name(f".punch-{int(number)}.new.flac")
    runner.run(cut_argv(str(capture), start, end, str(fresh)), timeout=1800).require()
    try:
        if not verify(runner, str(fresh)):
            raise PunchError("the cut file will not decode - the library is untouched")
        got = true_duration(runner, str(fresh))
        if abs(got - (end - start)) > LENGTH_TOLERANCE:
            raise PunchError(
                f"the cut is {got:.2f}s but {end - start:.2f}s was asked for"
            )
        art = carry_tags(runner, str(old), str(fresh))
    except Exception:
        fresh.unlink(missing_ok=True)
        raise

    # Moved aside, never overwritten. It is the only copy of that take that is
    # not buried inside a twenty-minute side.
    keep = kept_dir(layout, slug)
    keep.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    backup = keep / f"{stamp}--{old.name}"
    shutil.move(str(old), str(backup))
    try:
        shutil.move(str(fresh), str(old))
    except OSError:
        shutil.move(str(backup), str(old))  # put it back before raising
        raise
    shutil.move(str(capture), str(keep / f"{stamp}--{capture.name}"))
    return Applied(int(number), old, was, got, art, backup)


def discard(layout: Layout, slug: str, number: int) -> int:
    """Throw away a punch capture so the track can be recorded again."""
    capture = recorded(layout, slug, number)
    if capture is None:
        raise PunchError(f"no punch recorded for track {number}")
    size = capture.stat().st_size
    capture.unlink()
    return size


def state(
    runner: Runner, layout: Layout, plan: Plan, library: str, slug: str
) -> dict[str, Any]:
    """Every track, whether it can be punched, and what is already recorded."""
    rows = []
    for side in plan.sides:
        letter = side.file[len("side-") : -len(".flac")]
        for track in side.tracks:
            capture = recorded(layout, slug, track.number)
            try:
                in_library = library_file(plan, library, track.number).is_file()
            except PunchError:
                in_library = False
            rows.append(
                {
                    "number": track.number,
                    "title": track.title,
                    "side": letter,
                    "start": track.start,
                    "end": track.end,
                    "length": round(track.end - track.start, 2),
                    "in_library": in_library,
                    "punch": None
                    if capture is None
                    else {
                        "bytes": capture.stat().st_size,
                        "seconds": round(true_duration(runner, str(capture)), 2),
                    },
                }
            )
    return {"slug": slug, "album": plan.album, "artist": plan.artist, "tracks": rows}
