"""A filed copy held inside the library while its replacement is proved.

Same filesystem, so holding and putting back are renames. The manifest is written
before anything moves, which is what lets a restart find a copy a kill left here.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

WHERE = ".ripdoctor-replacing"
MANIFEST = "manifest.json"


@dataclass(frozen=True, slots=True)
class Held:
    where: Path
    rows: tuple[str, ...]
    files: tuple[str, ...]
    covers: tuple[str, ...] = ()
    directory: str = ""
    artist: str = ""
    album: str = ""
    mbid: str = ""
    proved: bool = False

    def _write(self) -> None:
        doc = {
            "rows": self.rows,
            "files": self.files,
            "covers": self.covers,
            "directory": self.directory,
            "artist": self.artist,
            "album": self.album,
            "mbid": self.mbid,
            "proved": self.proved,
        }
        tmp = self.where / f"{MANIFEST}.tmp"
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, self.where / MANIFEST)

    def _aside(self, n: int) -> Path:
        return self.where / str(n)

    @property
    def everything(self) -> tuple[str, ...]:
        return (*self.files, *self.covers)


def hold(library: str, held: Held) -> Held:
    """Move these files aside, all or none. `held.where` is filled in here."""
    base = Path(library) / WHERE
    base.mkdir(parents=True, exist_ok=True)
    made = replace(held, where=Path(tempfile.mkdtemp(dir=base)))
    made._write()
    try:
        for n, path in enumerate(made.everything):
            if Path(path).is_file():
                Path(path).rename(made._aside(n))
    except OSError:
        put_back(made)
        raise
    return made


def proved(held: Held) -> Held:
    done = replace(held, proved=True)
    done._write()
    return done


def put_back(held: Held) -> list[str]:
    """Every file to where it was, and those that could not go, which stay held. A file
    is never written over: something at that path is not known to be spare."""
    stuck = []
    for n, path in enumerate(held.everything):
        source, target = held._aside(n), Path(path)
        if not source.is_file():
            continue
        if target.exists():
            stuck.append(path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    if not stuck:
        drop(held)
    return stuck


def drop(held: Held) -> None:
    """The held copy is gone for good. Only for one proved, or already put back."""
    shutil.rmtree(held.where)
    with suppress(OSError):  # another replace is still held here
        held.where.parent.rmdir()


def found(library: str) -> list[Held]:
    """What a kill left. A manifest that will not read is left for a person: what it
    held cannot be said, and so nothing can be done with it safely."""
    base = Path(library) / WHERE
    if not base.is_dir():
        return []
    left = []
    for where in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            doc = json.loads((where / MANIFEST).read_text())
            left.append(
                Held(
                    where,
                    tuple(doc["rows"]),
                    tuple(doc["files"]),
                    tuple(doc["covers"]),
                    doc["directory"],
                    doc["artist"],
                    doc["album"],
                    doc["mbid"],
                    bool(doc["proved"]),
                )
            )
        except (OSError, ValueError, KeyError):
            left.append(Held(where, ("?",), ()))
    return left
