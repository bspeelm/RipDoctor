"""Taking the tracks out of a bought download. ADR-062."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

AUDIO = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif"})
ART = frozenset({".jpg", ".jpeg", ".png"})

# A download is one album. These are not tuning knobs; they are the point past
# which the file is not the thing it claims to be.
MAX_FILES = 200
MAX_UNPACKED = 4 * 1024 * 1024 * 1024


class NotAnAlbum(ValueError):
    """The upload is not a download of an album this can take."""


@dataclass(frozen=True, slots=True)
class Unpacked:
    where: Path
    tracks: tuple[str, ...]
    art: tuple[str, ...]


def _wanted(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[zipfile.ZipInfo]]:
    audio: list[zipfile.ZipInfo] = []
    art: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        # macOS zips carry a parallel tree of resource forks whose names match
        # the real ones; taken as tracks they double the album.
        if Path(info.filename).name.startswith("._") or "__MACOSX/" in info.filename:
            continue
        suffix = Path(info.filename).suffix.lower()
        if suffix in AUDIO:
            audio.append(info)
        elif suffix in ART:
            art.append(info)
    return audio, art


def inspect(archive: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What the download holds, by name, refusing what cannot be an album."""
    if not zipfile.is_zipfile(archive):
        raise NotAnAlbum("that file is not a zip")
    with zipfile.ZipFile(archive) as zf:
        audio, art = _wanted(zf)
        if not audio:
            raise NotAnAlbum("that zip holds no audio")
        if len(audio) + len(art) > MAX_FILES:
            raise NotAnAlbum(f"that zip holds more than {MAX_FILES} files")
        total = sum(i.file_size for i in audio + art)
        if total > MAX_UNPACKED:
            raise NotAnAlbum(
                f"that zip unpacks to more than {MAX_UNPACKED // 2**30} GB"
            )
    return tuple(i.filename for i in audio), tuple(i.filename for i in art)


def unpack(archive: Path, into: Path) -> Unpacked:
    """Write the album's files into `into`, flat.

    Only the base name is ever used, so a path in the archive cannot reach
    outside the directory and a download wrapped in a folder lands the same as
    one that is not.
    """
    inspect(archive)
    into.mkdir(parents=True, exist_ok=True)
    tracks: list[str] = []
    art: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        audio, pictures = _wanted(zf)
        for info, kind in [(i, tracks) for i in audio] + [(i, art) for i in pictures]:
            name = Path(info.filename).name
            dest = into / name
            if dest.exists():
                raise NotAnAlbum(f"two files in that zip are both called {name}")
            with zf.open(info) as src, dest.open("wb") as handle:
                while chunk := src.read(1024 * 1024):
                    handle.write(chunk)
            kind.append(name)
    return Unpacked(where=into, tracks=tuple(sorted(tracks)), art=tuple(sorted(art)))
