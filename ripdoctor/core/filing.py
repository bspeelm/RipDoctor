"""What filing a tape's songs takes, and how each song came out. ADR-063."""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ripdoctor.core.match import Choice, fold, names, search_album, search_artists
from ripdoctor.core.tape import (
    CUT,
    LENGTH_TOLERANCE,
    PENDING,
    SKIPPED,
    Song,
    Tape,
    cut_refusal,
    cut_status,
    label,
)

ALREADY, FILED, FAILED = "already", "filed", "failed"
FILE = "file"
FINISHED = frozenset((ALREADY, FILED))
_SHOWN = {FAILED: 0, PENDING: 1, FILED: 2, ALREADY: 3, SKIPPED: 4}


@dataclass(frozen=True, slots=True)
class Row:
    """One library row. `present` is whether its file is on disk, and `home` whether
    that path is under the library's root and outside the tape's own directories."""

    title: str
    artist: str
    album: str
    length: float | None
    path: str
    present: bool = True
    home: bool = True


@dataclass(frozen=True, slots=True)
class Track:
    song: Song
    number: int
    title: str


@dataclass(frozen=True, slots=True)
class Run:
    """One import of one directory. `release` is empty for an album from a list."""

    release: str
    artist: str
    album: str
    tracks: tuple[Track, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    action: str
    path: str = ""
    note: str = ""


def lead(artist: str) -> str:
    return search_artists(artist)[-1]


def choice_of(song: Song) -> Choice | None:
    choice = song.proposal.choice if song.proposal else None
    return choice if choice and choice.release else None


def recording_of(song: Song) -> str:
    choice = choice_of(song)
    return choice.recording if choice else ""


def runs(tape: Tape) -> tuple[Run, ...]:
    """What is unfinished, one run per album.

    Two releases in one run makes beets ask which, and a second lookup-off run for
    one album makes a second album row. A from-list song is numbered among all its
    album's songs, finished ones too, so a retry gives it the same number.
    """
    albums: dict[tuple[str, str, str], list[Track]] = {}
    for song in tape.songs:
        if song.skip:
            continue
        choice = choice_of(song)
        if choice:
            key = (choice.release, choice.album_artist, choice.release_title)
            track = Track(song, choice.number, choice.track_title or song.title)
        else:
            key = ("", fold(lead(song.artist)), fold(song.album))
            track = Track(song, len(albums.get(key, ())) + 1, song.title)
        albums.setdefault(key, []).append(track)

    planned = []
    for (release, artist, album), tracks in albums.items():
        first = tracks[0].song
        unfinished = tuple(t for t in tracks if t.song.filing not in FINISHED)
        if unfinished:
            names = (artist, album) if release else (lead(first.artist), first.album)
            planned.append(Run(release, *names, unfinished))
    return tuple(planned)


def row_length(said: str) -> float | None:
    """`M:SS`, or seconds where beets is set to raw lengths. None when not known."""
    minutes, _, seconds = said.strip().rpartition(":")
    try:
        length = int(minutes or 0) * 60 + float(seconds)
    except ValueError:
        return None
    return length if math.isfinite(length) and length > 0 else None


def _cleaned(title: str) -> str:
    return fold(search_album(title))


def _titles(song: Song) -> set[str]:
    choice = choice_of(song)
    named = (song.title, choice.track_title) if choice else (song.title,)
    return {_cleaned(t) for t in named if t}


def _same_artist(listed: str, row: str) -> bool:
    """Any shared name: a list and a library credit one song's guests differently."""
    return bool(names(fold(listed), loosely=True) & names(fold(row), loosely=True))


def _unusable(row: Row) -> Verdict:
    if not row.present:
        return Verdict(
            FAILED,
            note=f"beets has a row for this song at {row.path}, and the file is not "
            "there - clear the stale row, then file again",
        )
    return Verdict(
        FAILED,
        note=f"beets has a row for this song at {row.path}, which is not in the "
        "library - move it in with beet move, then file again, or skip this song",
    )


def precheck(song: Song, by_id: Sequence[Row], by_name: Sequence[Row]) -> Verdict:
    """Whether the library has this song. Merging a copy of an owned one files it
    as another track, so this answers before every import.

    A recording id is enough. Without one a cleaned title also hits a live album,
    so the length must agree, and a row with no length to compare is said aloud.
    Only a file that is there and in the library counts as owning it.
    """
    for row in by_id:
        if row.present and row.home:
            return Verdict(ALREADY, row.path)
    if by_id:
        return _unusable(by_id[0])

    titles = _titles(song)
    alike = [
        r
        for r in by_name
        if _cleaned(r.title) in titles and _same_artist(song.artist, r.artist)
    ]
    agreeing = [
        r
        for r in alike
        if r.length is not None and abs(r.length - song.length) <= LENGTH_TOLERANCE
    ]
    for row in agreeing:
        if row.present and row.home:
            return Verdict(ALREADY, row.path, "matched by title, artist and length")
    if agreeing:
        return _unusable(agreeing[0])
    for row in alike:
        if row.length is None:
            return Verdict(
                FAILED,
                note=f"{row.path} has this title and artist and no length to compare "
                "- listen to it: skip this song if it is the same, or run beet update "
                "on that row and file again",
            )
    if not choice_of(song) and not song.album:
        return Verdict(
            FAILED,
            note="the list names no album to file this under - "
            "choose a release, or skip it",
        )
    return Verdict(FILE)


def _is_the_song(song: Song, row: Row) -> bool:
    if recording_of(song):
        return True
    from_list = choice_of(song) is None
    return _cleaned(row.title) in _titles(song) and (
        not from_list or fold(row.album) == fold(song.album)
    )


def _row_of(
    song: Song, rows: Sequence[Row], before: Collection[str], *, home: bool
) -> str:
    for row in rows:
        fresh = row.present and row.path not in before
        if fresh and row.home == home and _is_the_song(song, row):
            return row.path
    return ""


def arrived(song: Song, rows: Sequence[Row], before: Collection[str] = ()) -> str:
    """Where the song is now, or nothing: a row that was already there is not it,
    and neither is one whose file is missing or is not in the library."""
    return _row_of(song, rows, before, home=True)


def astray(song: Song, rows: Sequence[Row], before: Collection[str] = ()) -> str:
    """Where an import put the song if that is not in the library, or nothing."""
    return _row_of(song, rows, before, home=False)


def second_row(run: Run, rows: Sequence[Row]) -> str:
    """A lookup-off run never joins an album, so anything already under this one
    means two album rows."""
    if any(fold(r.album) == fold(run.album) for r in rows):
        return f"beets already held {run.album}, so it now has two album rows for it"
    return ""


def recorded(
    tape: Tape, number: int, state: str, path: str = "", note: str = ""
) -> Tape:
    """One song's outcome. Pending or failed may become anything; finished stays."""

    def told(song: Song) -> Song:
        if song.number != number:
            return song
        if song.filing in FINISHED:
            raise ValueError(f"{label(song)} is already {song.filing}")
        return replace(
            song, filing=state, path=path, filing_note=note, import_begun=False
        )

    return replace(tape, songs=tuple(told(s) for s in tape.songs))


def begun(tape: Tape, numbers: Collection[int]) -> Tape:
    """These songs are going to beets. One found in the library after a restart, with
    this still set, got there by this tape, unlike a song it merely already owned."""
    return replace(
        tape,
        songs=tuple(
            replace(s, import_begun=True) if s.number in numbers else s
            for s in tape.songs
        ),
    )


def file_refusal(tape: Tape, on_disk: Collection[str]) -> str:
    """Why this tape may not be filed yet, naming every song, or nothing."""
    waiting = cut_refusal(tape)
    if waiting:
        return waiting
    count = len(tape.songs)
    uncut = [
        s for s in tape.songs if not s.skip and cut_status(s, count, on_disk) != CUT
    ]
    if uncut:
        names = ", ".join(label(s) for s in uncut)
        return f"{tape.name} cannot be filed yet. {names}: not cut - cut them first"
    return ""


def status(song: Song) -> str:
    return SKIPPED if song.skip and song.filing not in FINISHED else song.filing


def all_filed(tape: Tape) -> bool:
    active = [s for s in tape.songs if not s.skip]
    return bool(active) and all(s.filing in FINISHED for s in active)


def outcomes(tape: Tape) -> list[dict[str, Any]]:
    """Each song's outcome, the ones that need a person first."""
    ordered = sorted(tape.songs, key=lambda s: (_SHOWN[status(s)], s.number))
    return [
        {
            "number": s.number,
            "title": s.title,
            "artist": s.artist,
            "state": status(s),
            "path": s.path,
            "note": s.filing_note,
        }
        for s in ordered
    ]
