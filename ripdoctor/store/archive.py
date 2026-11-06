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

import json
import shutil
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ripdoctor.audio.ffprobe import true_duration
from ripdoctor.audio.runner import Runner
from ripdoctor.audio.split import verify
from ripdoctor.core.plan import OldFormat, Plan
from ripdoctor.integrations.importer import Importer
from ripdoctor.store import cache as C
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout, read_plan, read_spec, save, write_json
from ripdoctor.store.safety import under

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


def cut_captures(layout: Layout, slug: str, plan: Plan) -> dict[str, str]:
    """Which capture the tracks waiting in review came from, by side. A cut saves first,
    so the plan's stamp is the audio it cut, unless the side was captured since."""
    cuts = [p.stat().st_mtime for p in layout.review_dir(slug).glob("*.flac")]
    if not cuts:
        return {}
    source = layout.raw / slug
    return {
        s.file: s.audio
        for s in plan.sides
        if s.audio
        and (
            not (source / s.file).is_file()
            or (source / s.file).stat().st_mtime <= min(cuts)
        )
    }


def note_filed(layout: Layout, slug: str, captures: dict[str, str]) -> None:
    """Which capture the library's tracks came from, for the gate. ADR-055."""
    write_json(layout.filed_file(slug), {**_filed_captures(layout, slug), **captures})


def _filed_captures(layout: Layout, slug: str) -> dict[str, str]:
    where = layout.filed_file(slug)
    try:
        return dict(json.loads(where.read_text()))
    except (
        OSError,
        ValueError,
    ):  # no proof reads as no proof, and the gate asks for one
        return {}


def _capture_doubt(layout: Layout, slug: str) -> str:
    """Why the tracks in the library may not be from what is in raw, or nothing.

    Counting rows cannot tell: a side captured again leaves the earlier import counted
    in full, and archiving then replaces the capture those tracks were cut from.
    """
    try:
        plan = read_plan(layout.plan_file(slug))
    except (OSError, ValueError, KeyError, OldFormat):
        return ""
    placed = {s.file: s.audio for s in plan.sides}
    filed = _filed_captures(layout, slug)
    for side in sorted((layout.raw / slug).glob("side-*.flac")):
        now = C.stamp_of(side)
        kept = layout.archive / slug / side.name
        proof = filed.get(side.name)
        if (
            placed.get(side.name, now) not in ("", now)
            or (proof and proof != now)
            or (not proof and kept.is_file() and C.stamp_of(kept) != now)
        ):
            letter = side.stem.removeprefix("side-")
            return (
                f"side {letter} was captured again, and the tracks in the library were "
                "not cut from this capture. Archiving now would replace the earlier "
                "capture they came from. Cut this one and import it over them first"
            )
    return ""


def survey(
    runner: Runner,
    layout: Layout,
    importer: Importer,
    library: str,
    slug: str,
    artist: str,
    album: str,
    expected: int,
    *,
    read: bool = False,
    mbid: str = "",
) -> dict[str, Any]:
    """What archiving would do, and whether it is allowed to.

    `read` decodes every side, which takes seconds each - worth it before the
    irreversible step, not worth it for a button's tooltip.
    """
    # Asked of the importer rather than derived: beets owns its own naming, and
    # a gate that checked the path this project would have chosen would be
    # checking the wrong directory on every install that configured it.
    # By its release first: beets may keep the artist under another name than the
    # plan's. A record filed with no release has only its names to be found by, and a
    # row that does name a release is another edition, which proves nothing about this.
    where, count = importer.locate(runner, library, artist, album, mbid=mbid)
    editions = 0
    if where is None and mbid:
        where, count = importer.locate(runner, library, artist, album, unreleased=True)
        if where is None:
            editions = importer.locate(runner, library, artist, album)[1]
    source = layout.raw / slug
    sides = []
    if source.is_dir():
        for p in sorted(source.glob("side-*.flac")):
            sides.append(
                Side(p.name, p.stat().st_size, verify(runner, str(p)) if read else None)
            )
    doubt = _capture_doubt(layout, slug) if where and count == expected else ""
    ready = where is not None and count == expected and bool(sides) and not doubt
    why = doubt
    if not sides:
        why = f"no sides in raw for {slug}"
    elif editions:
        why = (
            f"the library has {editions} tracks called {artist} - {album}, but of "
            "another release than this record names. Import this one, or pin that one."
        )
    elif where is None:
        why = "the record is not in the library yet"
    elif count < expected:
        why = f"only {count} of {expected} tracks are in the library"
    elif count > expected:
        # Clearing raw/ on the strength of this archives over a record the
        # library holds twice. ADR-057.
        why = (
            f"the library holds {count} tracks and this record has {expected} - "
            "an earlier copy was not replaced. Remove it, then archive."
        )
    return {
        "ready": ready,
        "why": why,
        "captured_again": bool(doubt),
        "library_path": None if where is None else str(where),
        "library_tracks": count,
        "expected": expected,
        "will_archive_to": str(layout.archive / slug),
        "sides": [s.as_dict() for s in sides],
        "will_remove": [str(p) for p in removable(layout, slug) if p.exists()],
        # Named before the irreversible step rather than reported after it.
        "will_replace": [
            p.name
            for p in sorted((layout.archive / slug).glob("side-*.flac"))
            if (layout.raw / slug / p.name).is_file()
        ],
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


# Where a re-ripped side's predecessor waits while its replacement is written
# and read back. It is the rollback: a side that does not verify is undone by
# putting this one back. What happens to it afterwards is ADR-055.
SUPERSEDED = "_superseded"

# A leading underscore is what no record's slug can have, and nothing in here is
# named as a side, so a tape never appears among the archive's records.
TAPE_DIR = "_tape-{tape}"
# A byte-for-byte copy decodes to the same length. This allows only for rounding.
SAME_LENGTH = 0.1


def step_aside(target: Path) -> Path | None:
    """Move an archived side aside, returning where, so it can be put back."""
    if not target.exists():
        return None
    kept = target.parent / SUPERSEDED
    kept.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = kept / f"{stamp}--{target.name}"
    target.replace(moved)
    return moved


def read_back(runner: Runner, target: Path, timeout: float | None = None) -> float:
    """Seconds of audio at `target`, or nothing if it does not play and verify."""
    limits = {} if timeout is None else {"timeout": timeout}
    seconds = true_duration(runner, str(target), **limits)
    return seconds if seconds > 0 and verify(runner, str(target), **limits) else 0.0


def put_away(
    runner: Runner,
    layout: Layout,
    slug: str,
    *,
    keep_superseded: bool = False,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Copy every side into the archive, read it back, then clear the rest.

    A side being re-ripped has its predecessor moved aside first, and put back
    if the replacement does not verify. Once every side has read back, the
    predecessors are gone unless asked for: by then the new take has been cut,
    tagged, filed and proved to be in the library, so the copy it insured
    against losing is no longer the only one. ADR-055.
    """
    source = layout.raw / slug
    if not source.is_dir():
        raise NotReady(f"nothing in raw for {slug}")
    dest = layout.archive / slug
    dest.mkdir(parents=True, exist_ok=True)

    def say(what: str, detail: str) -> None:
        if progress:
            progress(what, detail)

    archived, notes = [], []
    reencoded: dict[str, tuple[str, str]] = {}
    # Held until every side has read back, not just this one: a later side
    # failing must still be able to put the earlier ones back.
    stepped: list[Path] = []
    for side in sorted(source.glob("side-*.flac")):
        target = dest / side.name
        superseded = step_aside(target)
        if superseded:
            stepped.append(superseded)
        if verify(runner, str(side)):
            say(side.name, "copying")
            shutil.copy2(side, target)
        else:
            # Not a failure: it is what a capture ended with a signal looks
            # like, and re-encoding is how it stops being that.
            say(side.name, "re-encoding")
            reencoded[side.name] = (C.stamp_of(side), "")
            runner.run(repair_argv(str(side), str(target)), timeout=3600).require()
            notes.append(f"{side.name} was truncated and has been re-encoded")

        seconds = read_back(runner, target)
        if not seconds:
            target.unlink(missing_ok=True)
            if superseded:
                superseded.replace(target)
            raise NotReady(f"{side.name} did not read back from the archive")
        if side.name in reencoded:
            reencoded[side.name] = (reencoded[side.name][0], C.stamp_of(target))
        archived.append(
            {
                "side": side.name,
                "bytes": target.stat().st_size,
                "seconds": round(seconds, 2),
            }
        )

    if not archived:
        raise NotReady(f"no sides in raw for {slug}")

    _same_audio(layout, slug, reencoded)

    # Only now, with every side read back from where it will live.
    for old_take in stepped:
        if keep_superseded:
            notes.append(f"the previous {old_take.name} is in {SUPERSEDED}/")
            continue
        old_take.unlink(missing_ok=True)
        notes.append(f"the previous {old_take.name} was replaced")
    with suppress(OSError):
        (dest / SUPERSEDED).rmdir()

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


def _same_audio(
    layout: Layout, slug: str, reencoded: dict[str, tuple[str, str]]
) -> None:
    """A re-encode changes the file and not the music, so the plan follows it.

    Times placed on a truncated capture are still right on its repaired copy. A
    plan that does not read is left for whoever opens it to be told about.
    """
    where = layout.plan_file(slug)
    if not reencoded or not where.is_file():
        return
    try:
        plan = read_plan(where)
        spec = read_spec(layout.spec_file(slug))
    except (OSError, ValueError, KeyError, OldFormat):
        return
    sides = tuple(
        replace(s, audio=reencoded[s.file][1])
        if s.file in reencoded and s.audio == reencoded[s.file][0]
        else s
        for s in plan.sides
    )
    save(layout, slug, spec, replace(plan, sides=sides))


def keep_tape(runner: Runner, layout: Layout, tape: str) -> dict[str, Any]:
    """Copy a tape's uncut file in and read it back against the source's own length.

    Clears nothing. An earlier copy is stepped aside only once this one has read
    back, and stays aside: a tape has no other record of what it superseded.
    """
    source = T.audio_file(layout, tape)
    dest = under(layout.archive, TAPE_DIR.format(tape=tape))
    dest.mkdir(parents=True, exist_ok=True)
    final, part = dest / T.AUDIO, dest / f"part-{T.AUDIO}"
    seconds = true_duration(runner, str(source), timeout=T.LONG_RUN)
    try:
        shutil.copy2(source, part)
        back = read_back(runner, part, T.LONG_RUN)
        if seconds <= 0 or not back or abs(back - seconds) > SAME_LENGTH:
            raise NotReady(
                f"the copy of {tape} did not read back as {seconds:.2f} s "
                f"(read {back:.2f} s) - nothing was cleared"
            )
        step_aside(final)
        part.replace(final)
    finally:
        part.unlink(missing_ok=True)
    return {
        "file": str(final),
        "bytes": final.stat().st_size,
        "seconds": round(back, 2),
    }
