"""Tagging and placing files without beets. ADR-007.

The base install has to be able to finish a record, or the archive step can
never be satisfied and raw sides accumulate with nowhere to go. This is that
path: write tags with metaflac, put the files where the library wants them, and
report what is there so the archive gate has something to check.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.audio.runner import Runner
from ripdoctor.core.naming import safe_filename, track_filename
from ripdoctor.core.plan import Plan, PlanTrack

# Vorbis comments, in the spelling players actually read.
TAG_TITLE = "TITLE"
TAG_ARTIST = "ARTIST"
TAG_ALBUM = "ALBUM"
TAG_ALBUMARTIST = "ALBUMARTIST"
TAG_TRACK = "TRACKNUMBER"
TAG_TOTAL = "TRACKTOTAL"
TAG_DATE = "DATE"

# Cover art block: type 3 is the front cover, and the description is empty on
# purpose - some players show it as a caption.
PICTURE_SPEC = "3|image/jpeg||{width}x{height}x24|{path}"


@dataclass(frozen=True, slots=True)
class Placement:
    """One track, and where in the library it belongs."""

    track: PlanTrack
    source: Path
    dest: Path


def tags_for(plan: Plan, track: PlanTrack, total: int) -> dict[str, str]:
    return {
        TAG_TITLE: track.title,
        TAG_ARTIST: plan.artist,
        TAG_ALBUMARTIST: plan.artist,
        TAG_ALBUM: plan.album,
        TAG_TRACK: str(track.number),
        TAG_TOTAL: str(total),
        **({TAG_DATE: plan.date} if plan.date else {}),
    }


def write_tags_argv(path: str, tags: dict[str, str]) -> list[str]:
    """Replace these tags and leave every other one alone.

    Each key is removed before it is set, or a second run leaves two values on
    the same tag and players show whichever they read first.
    """
    argv = ["metaflac"]
    for key in tags:
        argv.append(f"--remove-tag={key}")
    for key, value in tags.items():
        argv.append(f"--set-tag={key}={value}")
    argv.append(path)
    return argv


def write_tags(runner: Runner, path: str, tags: dict[str, str]) -> None:
    runner.run(write_tags_argv(path, tags), timeout=60).require()


def embed_art_argv(path: str, art: str, width: int, height: int) -> list[str]:
    spec = PICTURE_SPEC.format(width=width, height=height, path=art)
    return ["metaflac", f"--import-picture-from={spec}", path]


def embed_art(runner: Runner, path: str, art: str, width: int, height: int) -> None:
    """Replace the cover art, checking the new one first.

    The removal and the import are two commands, and doing them in that order
    strips the art a file already has and then fails if the new one is bad -
    leaving nothing. The file is verified before anything is taken away.
    """
    if not Path(art).is_file() or Path(art).stat().st_size < 1024:
        raise ValueError(f"not a usable image: {art}")
    runner.run(["metaflac", "--remove", "--block-type=PICTURE", path], timeout=60)
    runner.run(embed_art_argv(path, art, width, height), timeout=60).require()


def has_art(runner: Runner, path: str) -> bool:
    result = runner.run(
        ["metaflac", "--list", "--block-type=PICTURE", path], timeout=60
    )
    return bool(result.text.strip())


def album_dir(library: str, artist: str, album: str) -> Path:
    """Where a record lives: <library>/<artist>/<album>."""
    return (
        Path(library)
        / safe_filename(artist or "Unknown Artist")
        / safe_filename(album or "Unknown Album")
    )


def placements(plan: Plan, review: str, library: str) -> list[Placement]:
    dest_dir = album_dir(library, plan.artist, plan.album)
    return [
        Placement(
            track=t,
            source=Path(review) / track_filename(t.number, t.title),
            dest=dest_dir / track_filename(t.number, t.title),
        )
        for side in plan.sides
        for t in side.tracks
    ]


def locate(library: str, artist: str, album: str) -> tuple[Path | None, int]:
    """The destination and how many tracks are in it.

    This is what the archive gate asks: does the destination hold what it should
    before the raw sides are cleared. beets answers the same question its own
    way, which is why the gate takes an answer rather than a library.
    """
    where = album_dir(library, artist, album)
    if not where.is_dir():
        return None, 0
    return where, len([p for p in where.iterdir() if p.suffix.lower() == ".flac"])


def apply(
    runner: Runner,
    plan: Plan,
    review: str,
    library: str,
    *,
    file_mode: int = 0o664,
    dir_mode: int = 0o775,
) -> list[Placement]:
    """Tag every track and move it into the library."""
    moved = placements(plan, review, library)
    total = sum(len(s.tracks) for s in plan.sides)
    if not moved:
        return []

    moved[0].dest.parent.mkdir(parents=True, exist_ok=True)
    moved[0].dest.parent.chmod(dir_mode)

    for p in moved:
        if not p.source.is_file():
            raise FileNotFoundError(f"no cut track at {p.source}")
        write_tags(runner, str(p.source), tags_for(plan, p.track, total))
        shutil.move(str(p.source), str(p.dest))
        # Left readable by the group on purpose: a library only the owning
        # process can read is one a share cannot serve, and nothing reports it
        # because playback still works for whoever owns the files.
        p.dest.chmod(file_mode)
    return moved
