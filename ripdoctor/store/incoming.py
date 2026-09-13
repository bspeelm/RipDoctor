"""A file arriving in pieces, before it is a side. ADR-053.

Everything here lives under `work/incoming/`, which is outside `raw/` on
purpose: `forget` refuses to drop a name while any file sits under the album
directory, so a scratch left there would pin a name belonging to nothing. It is
still on the same filesystem as `raw/`, which is what makes the placement a
rename rather than a copy.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.store.files import SIDE, Layout, write_json
from ripdoctor.store.safety import under

WHERE = "incoming"
PART = "{slug}.part"
NOTE = "{slug}.json"
DONE = "{slug}.flac"

# How long an abandoned upload is kept before a later one sweeps it.
STALE_SECONDS = 24 * 60 * 60

# Room for the arriving file and the encode it becomes, with margin. Refused up
# front rather than discovered by a full pool part-way through a cut.
HEADROOM = 2.5


class Mismatch(Exception):
    """The client and the scratch disagree about how much has arrived."""

    def __init__(self, have: int) -> None:
        super().__init__(f"the upload is at {have} bytes")
        self.have = have


@dataclass(frozen=True, slots=True)
class Incoming:
    name: str
    total: int
    have: int
    started: float

    def as_dict(self) -> dict[str, object]:
        return {
            "exists": True,
            "name": self.name,
            "total": self.total,
            "have": self.have,
            "started": self.started,
        }


def part_file(layout: Layout, slug: str) -> Path:
    return under(layout.work, WHERE, PART.format(slug=slug))


def note_file(layout: Layout, slug: str) -> Path:
    return under(layout.work, WHERE, NOTE.format(slug=slug))


def done_file(layout: Layout, slug: str) -> Path:
    return under(layout.work, WHERE, DONE.format(slug=slug))


def room_for(layout: Layout, size: int) -> bool:
    return shutil.disk_usage(layout.root).free >= size * HEADROOM


def begin(layout: Layout, slug: str, *, name: str, size: int, now: float) -> None:
    """Start an upload, discarding whatever was there under this name."""
    part = part_file(layout, slug)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"")
    write_json(note_file(layout, slug), {"name": name, "total": size, "started": now})


def state(layout: Layout, slug: str) -> Incoming | None:
    """What has arrived, or nothing. The whole of reload recovery."""
    note, part = note_file(layout, slug), part_file(layout, slug)
    if not note.is_file() or not part.is_file():
        return None
    try:
        held = json.loads(note.read_text())
    except (OSError, ValueError):
        return None
    return Incoming(
        name=str(held.get("name", "")),
        total=int(held.get("total", 0)),
        have=part.stat().st_size,
        started=float(held.get("started", 0.0)),
    )


def append(layout: Layout, slug: str, at: int, data: bytes) -> int:
    """Add one piece, refusing anything that does not follow what is there.

    The check is advisory rather than a lock - a client that lies still wins -
    and the backstop is that a wrongly assembled file will not decode.
    """
    part = part_file(layout, slug)
    have = part.stat().st_size if part.is_file() else 0
    if at != have:
        raise Mismatch(have)
    with part.open("ab") as fh:
        fh.write(data)
    return have + len(data)


def place(layout: Layout, slug: str, letter: str = "a") -> Path:
    """Move the finished encode under a side name, in one instant.

    Rename rather than copy, so the first moment anything can see a side it is
    whole. Nothing is ever written under the side name itself.
    """
    album = under(layout.raw, slug)
    album.mkdir(parents=True, exist_ok=True)
    target = album / SIDE.format(letter=letter)
    os.replace(done_file(layout, slug), target)
    return target


def clear(layout: Layout, slug: str) -> int:
    """Remove every trace of an upload. Returns the bytes reclaimed."""
    freed = 0
    for path in (
        part_file(layout, slug),
        note_file(layout, slug),
        done_file(layout, slug),
    ):
        if path.is_file():
            freed += path.stat().st_size
            path.unlink()
    return freed


def sweep(layout: Layout, keep: str, now: float) -> list[str]:
    """Drop uploads nobody came back to, never the one in hand."""
    swept: list[str] = []
    where = layout.work / WHERE
    if not where.is_dir():
        return swept
    for part in sorted(where.glob("*.part")):
        slug = part.name[: -len(".part")]
        if slug == keep or now - part.stat().st_mtime < STALE_SECONDS:
            continue
        clear(layout, slug)
        swept.append(slug)
    return swept
