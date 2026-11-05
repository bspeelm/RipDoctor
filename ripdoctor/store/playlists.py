"""Where a tape's playlist is written, and what is remembered about it. ADR-063.

The memory is a file beside the tape's document rather than in it: a playlist is
touched by a rebuild long after the tape's own work has finished.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ripdoctor.core import playlist as P
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout, write_json
from ripdoctor.store.safety import under

STATE = "playlist.json"


class Taken(ValueError):
    """The file this playlist would be written over belongs to something else."""


def path_of(library: str, file: str) -> Path:
    return under(library, P.FOLDER, file)


def _state_file(layout: Layout, tape: str) -> Path:
    return T.dir_for(layout, tape) / STATE


def known(layout: Layout, tape: str) -> bool:
    return _state_file(layout, tape).is_file()


def read(layout: Layout, tape: str) -> P.State:
    where = _state_file(layout, tape)
    return (
        P.State.from_dict(json.loads(where.read_text()))
        if where.is_file()
        else P.State()
    )


def save(layout: Layout, tape: str, state: P.State) -> None:
    write_json(_state_file(layout, tape), state.to_dict())


def write(
    library: str, name: str, file: str, text: str, file_mode: int, dir_mode: int
) -> bool:
    """Put the text in place, and say whether the file changed.

    The same bytes leave it untouched, because the music server re-imports on any
    change. A library that is not there is an error rather than a directory made
    in its place, and the modes are set here because a service's umask would
    otherwise decide them. ADR-056.
    """
    folder, target = under(library, P.FOLDER), path_of(library, file)
    data = text.encode()
    if target.is_file():
        held = target.read_bytes()
        if held == data:
            return False
        if not P.declares(held.decode(errors="replace"), name):
            raise Taken(f"{target} is not the playlist {name} - nothing was written")
    if not folder.is_dir():
        folder.mkdir()
        folder.chmod(dir_mode)
    # Hidden and not an .m3u8, so the server never imports a half-written one.
    part = folder / f".{file}.tmp"
    try:
        with part.open("wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        part.chmod(file_mode)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return True
