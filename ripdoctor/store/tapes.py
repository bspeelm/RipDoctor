"""One directory per tape under `work/tapes/`, out of sight of the record picker.

A tape's upload and job are keyed by `key_of`, which cannot meet a record's
scratch file or job. ADR-063.
"""

from __future__ import annotations

import json
import os
import time
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
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    aside = under(layout.work, REMOVED)
    aside.mkdir(parents=True, exist_ok=True)
    target, suffix = aside / f"{tape}-{stamp}", 1
    while target.exists():
        suffix += 1
        target = aside / f"{tape}-{stamp}-{suffix}"
    dir_for(layout, tape).rename(target)
    IN.clear(layout, key_of(tape))
    return target


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
