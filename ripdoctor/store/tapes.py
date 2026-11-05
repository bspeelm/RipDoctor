"""One directory per tape under `work/tapes/`, out of sight of the record picker.

A tape's upload and job are keyed by `key_of`, which cannot meet a record's
scratch file or job. ADR-063.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.audio import astats
from ripdoctor.audio.ffprobe import sample_rate
from ripdoctor.audio.runner import Runner
from ripdoctor.core.envelope import (
    Envelope,
    decode,
    encode,
    window_count_is_plausible,
)
from ripdoctor.core.tape import Tape, named, tape_id
from ripdoctor.store import incoming as IN
from ripdoctor.store.files import Layout, write_json
from ripdoctor.store.safety import under

WHERE = "tapes"
REMOVED = "tapes-removed"
DOCUMENT = "tape.json"
AUDIO = "tape.flac"
ENVELOPE = "tape.env"
PREVIEW = "tape.opus"
CUTS = "cuts"
CLIPS = "clips"
PARTIAL = "partial"
SUPERSEDED = "superseded"
FILING = "filing"

# Nearly seven hours of audio has to fit, at a pace no faster than a slow disk.
LONG_RUN = 3 * 3600.0


@dataclass(frozen=True, slots=True)
class Listing:
    tapes: tuple[Tape, ...]
    unreadable: tuple[str, ...]


def key_of(tape: str) -> str:
    return f"{IN.TAPE_KEY}{tape}"


def dir_for(layout: Layout, tape: str) -> Path:
    return under(layout.work, WHERE, tape)


def audio_file(layout: Layout, tape: str) -> Path:
    return dir_for(layout, tape) / AUDIO


def _document(layout: Layout, tape: str) -> Path:
    return dir_for(layout, tape) / DOCUMENT


def _envelope(layout: Layout, tape: str) -> Path:
    return dir_for(layout, tape) / ENVELOPE


def preview_file(layout: Layout, tape: str) -> Path:
    return dir_for(layout, tape) / PREVIEW


def clip_file(layout: Layout, tape: str, name: str) -> Path:
    return dir_for(layout, tape) / CLIPS / name


def clips_on_disk(layout: Layout, tape: str) -> set[str]:
    return _names_in(dir_for(layout, tape) / CLIPS)


def _names_in(where: Path) -> set[str]:
    return {p.name for p in where.iterdir()} if where.is_dir() else set()


def cut_file(layout: Layout, tape: str, name: str) -> Path:
    return dir_for(layout, tape) / CUTS / name


def cuts_on_disk(layout: Layout, tape: str) -> set[str]:
    return _names_in(dir_for(layout, tape) / CUTS)


def _unused(wanted: Path) -> Path:
    """`wanted`, or that name with the first number after it that nothing holds yet."""
    where, suffix = wanted, 1
    while where.exists():
        suffix += 1
        where = wanted.with_name(f"{wanted.name}-{suffix}")
    return where


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def supersede(layout: Layout, tape: str, name: str, now: float) -> Path:
    """Move a cut aside, because something superseded is never deleted."""
    aside = dir_for(layout, tape) / SUPERSEDED
    aside.mkdir(exist_ok=True)
    target, suffix = aside / f"{_stamp(now)}-{name}", 1
    while target.exists():
        suffix += 1
        target = aside / f"{_stamp(now)}-{suffix}-{name}"
    os.replace(cut_file(layout, tape, name), target)
    return target


def take_cut(layout: Layout, tape: str, part: Path, name: str, now: float) -> None:
    """Put a verified cut under its real name. For a moment there may be no file
    there, which a resume reads as not cut, and never a half-written one."""
    if cut_file(layout, tape, name).exists():
        supersede(layout, tape, name, now)
    promote(part, cut_file(layout, tape, name))


def fresh_partial(layout: Layout, tape: str) -> Path:
    """Where a job writes before a file earns its real name.

    Emptied first: a job holds its tape, so what is here is what a crash left
    half-written, and that was never anything.
    """
    where = dir_for(layout, tape) / PARTIAL
    where.mkdir(exist_ok=True)
    for leftover in where.iterdir():
        leftover.unlink()
    return where


def promote(part: Path, target: Path) -> None:
    target.parent.mkdir(exist_ok=True)
    os.replace(part, target)


def filing_dir(layout: Layout, tape: str) -> Path:
    return dir_for(layout, tape) / FILING


def fresh_filing(layout: Layout, tape: str, name: str) -> Path:
    """An empty directory for one album run.

    The name is new for each attempt, because beets offers to resume a path it was
    interrupted on, and nothing here would answer that.
    """
    filing_dir(layout, tape).mkdir(exist_ok=True)
    where = _unused(filing_dir(layout, tape) / name)
    where.mkdir()
    return where


def clear_copies(layout: Layout, tape: str, names: Collection[str]) -> None:
    """Drop the copies of these cuts from every attempt, and whatever that empties.

    Asked only for songs the library has proved it holds. A failed song's copies
    stay until it succeeds, which costs a little room and only until then.
    """
    filing = filing_dir(layout, tape)
    for attempt in filing.iterdir() if filing.is_dir() else ():
        for name in names:
            (attempt / name).unlink(missing_ok=True)
        if not any(attempt.iterdir()):
            attempt.rmdir()
    if filing.is_dir() and not any(filing.iterdir()):
        filing.rmdir()


def copy_cut(layout: Layout, tape: str, name: str, into: Path) -> Path:
    """A copy and not a hard link: tags written to a link could change the cut,
    which has to remain as it was verified, and beets moves whatever it is given."""
    target = into / name
    shutil.copy2(cut_file(layout, tape, name), target)
    return target


def save(layout: Layout, tape: Tape) -> None:
    write_json(_document(layout, tape.id), tape.to_dict())


def read(layout: Layout, tape: str) -> Tape:
    """Raises FileNotFoundError for a tape that is not there."""
    return Tape.from_dict(json.loads(_document(layout, tape).read_text()))


def create(layout: Layout, name: str, now: float) -> Tape:
    """Two names can sanitise alike, so the directory is claimed, not assumed."""
    base = named(tape_id(name), name, now).id
    where = under(layout.work, WHERE)
    where.mkdir(parents=True, exist_ok=True)
    wanted, suffix = base, 1
    while True:
        try:
            dir_for(layout, wanted).mkdir()
        except FileExistsError:
            suffix += 1
            wanted = f"{base}-{suffix}"
            continue
        tape = named(wanted, name, now)
        save(layout, tape)
        return tape


def listed(layout: Layout) -> Listing:
    where = layout.work / WHERE
    tapes: list[Tape] = []
    unreadable: list[str] = []
    for found in sorted(where.iterdir()) if where.is_dir() else ():
        if not found.is_dir():
            continue
        try:
            tapes.append(read(layout, found.name))
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(found.name)
    return Listing(tuple(tapes), tuple(unreadable))


def take_audio(layout: Layout, tape: str) -> Path:
    """Move a verified encode into the tape, with no stale measurements behind it."""
    target = audio_file(layout, tape)
    _envelope(layout, tape).unlink(missing_ok=True)
    os.replace(IN.done_file(layout, key_of(tape)), target)
    return target


def measure(runner: Runner, layout: Layout, tape: str, duration: float) -> Envelope:
    """The full lane on the recording's real length, measured once per recording."""
    source = audio_file(layout, tape)
    target = _envelope(layout, tape)
    if not target.is_file():
        rate = sample_rate(runner, str(source))
        lanes = astats.lanes(runner, str(source), rate=rate, timeout=LONG_RUN)
        count = len(lanes.full)
        if not window_count_is_plausible(count, duration, lanes.window):
            raise RuntimeError(
                f"{count} windows for {duration:.1f}s - measuring gave an envelope "
                "that would not line up with the audio; try processing again"
            )
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(encode(lanes))
        tmp.replace(target)
    return decode(target.read_bytes()).full.rescaled(duration)


def remove(layout: Layout, tape: str, now: float) -> Path:
    """Only ever asked for by a person, and returns where the tape went.

    It holds the only audio and any boundaries set by ear, so it moves aside.
    An upload not yet taken in still exists wherever it was sent from, and goes.
    """
    aside = under(layout.work, REMOVED)
    aside.mkdir(parents=True, exist_ok=True)
    target = _unused(aside / f"{tape}-{_stamp(now)}")
    dir_for(layout, tape).rename(target)
    IN.clear(layout, key_of(tape))
    return target


def clear_proved(
    layout: Layout, tape: str, proved: Collection[str], now: float
) -> list[str]:
    """Clear what has been proved to be elsewhere. A cut nothing answers for moves
    aside, and the uncut file goes last so an interrupted clear can be run again."""
    cleared = []
    for name in sorted(cuts_on_disk(layout, tape)):
        if name in proved:
            cut_file(layout, tape, name).unlink()
            cleared.append(name)
        else:
            supersede(layout, tape, name, now)
    audio_file(layout, tape).unlink()
    return [*cleared, AUDIO]


def sweep(layout: Layout) -> list[str]:
    """A tape's upload lasts as long as its tape, however old: drop only orphans."""
    swept: list[str] = []
    where = layout.work / IN.WHERE
    for part in sorted(where.glob(f"{IN.TAPE_KEY}*.part")) if where.is_dir() else ():
        key = part.name[: -len(".part")]
        if not dir_for(layout, key.removeprefix(IN.TAPE_KEY)).is_dir():
            IN.clear(layout, key)
            swept.append(key)
    return swept
