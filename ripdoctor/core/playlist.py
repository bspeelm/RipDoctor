"""A tape's `.m3u8`: its exact text, its name, and who answers for each song.
ADR-063."""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from ripdoctor.core import filing as F
from ripdoctor.core import tape as TP
from ripdoctor.core.match import fold

FOLDER = "Playlists"

HERE, MISSING, AMBIGUOUS = "here", "missing", "ambiguous"
NOT_FILED, OUTSIDE, UNPRINTABLE = "not filed", "outside", "unprintable"
WRITTEN, UNCHANGED, UNKNOWN = "written", "unchanged", "unknown"
NOTHING, FAILED = "nothing", "failed"


class OutsideLibrary(ValueError):
    """A path that no relative line in the playlist should ever name."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One song's answer: `path` when it is here, otherwise why it has no line."""

    number: int
    path: str = ""
    state: str = HERE
    why: str = ""


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    songs: int
    left_out: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Held:
    """One library row and the ids a playlist is rebuilt by."""

    row: F.Row
    recording: str = ""
    release: str = ""


@dataclass(frozen=True, slots=True)
class State:
    """What is known about one tape's playlist. `file` is set once one is written;
    `outcome` and `note` are the last attempt, which may not have touched it."""

    file: str = ""
    songs: int = 0
    left_out: tuple[Entry, ...] = ()
    outcome: str = ""
    note: str = ""
    at: float = 0.0
    archived: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        gone = tuple(Entry(**x) for x in data["left_out"])
        return cls(**{**data, "left_out": gone})


def file_name(name: str, key: str) -> str:
    """Path-safe whatever the name holds. Names that clean up alike stay apart by
    the digest, and `key` keeps two tapes of one name apart too."""
    digest = hashlib.sha256(f"{key}\0{name}".encode()).hexdigest()[:12]
    return f"{TP.tape_id(name)}-{digest}.m3u8"


def file_of(tape: TP.Tape) -> str:
    """The creation time is in the key because a removed tape's file stays behind,
    and a new tape may be given the removed one's id."""
    return file_name(tape.name, f"{tape.id}@{tape.created}")


def _header(name: str) -> str:
    return "#EXTM3U\n#PLAYLIST:" + " ".join(name.splitlines()) + "\n"


def declares(text: str, name: str) -> bool:
    """Whether a file on disk says it is the playlist called `name`."""
    return text.startswith(_header(name))


def _line(path: str, library: str) -> str:
    """A path as the playlist's own directory sees it. Spelled out rather than left
    to `relpath`, which would consult the working directory."""
    root = posixpath.normpath(library).rstrip("/") + "/"
    whole = posixpath.normpath(posixpath.join(root, path))
    if not whole.startswith(root):
        raise OutsideLibrary(f"{path} is outside the library at {root}")
    inside = whole.removeprefix(root)
    first, _, rest = inside.partition("/")
    if first != FOLDER or not rest:
        return f"../{inside}"
    # A first character of `#` would read as a comment.
    return f"./{rest}" if rest.startswith("#") else rest


def render(name: str, entries: Iterable[Entry], library: str) -> Rendered:
    """The file's exact text, in list order, and every song that has no line.

    Raises for a path outside the library: that is a fault in what was asked, where
    a song that did not resolve is an ordinary answer.
    """
    lines: list[str] = []
    gone: list[Entry] = []
    for entry in entries:
        if entry.state != HERE:
            gone.append(entry)
        elif "\n" in entry.path or "\r" in entry.path:
            why = f"{entry.path!r} has a line break, which a playlist cannot hold"
            gone.append(Entry(entry.number, state=UNPRINTABLE, why=why))
        else:
            lines.append(_line(entry.path, library))
    text = _header(name) + "".join(f"{line}\n" for line in lines)
    return Rendered(text, len(lines), tuple(gone))


def _not_filed(song: TP.Song) -> Entry | None:
    if song.skip:
        return Entry(song.number, state=TP.SKIPPED, why="skipped")
    if song.filing == F.FAILED:
        why = f"did not file: {song.filing_note}"
        return Entry(song.number, state=NOT_FILED, why=why)
    if song.filing not in F.FINISHED:
        return Entry(song.number, state=NOT_FILED, why="not filed yet")
    return None


def from_recorded(tape: TP.Tape, on_disk: Collection[str]) -> tuple[Entry, ...]:
    """Each song's outcome as the filing job recorded it, believed only if the file
    is there now."""

    def entry(song: TP.Song) -> Entry:
        unfiled = _not_filed(song)
        if unfiled:
            return unfiled
        if song.path not in on_disk:
            why = f"{song.path or 'its recorded path'} is not there"
            return Entry(song.number, state=MISSING, why=why)
        return Entry(song.number, song.path)

    return tuple(entry(s) for s in tape.songs)


def to_ask(tape: TP.Tape) -> tuple[str, ...]:
    """One beets query for a whole tape. An artist is asked for wherever an id will
    not find the row: no id, or a song owned by name under whatever id its row has.
    A trailing comma would read as beets' own separator."""
    terms: dict[str, None] = {}
    for song in tape.songs:
        if song.skip:
            continue
        recording = F.recording_of(song)
        if recording:
            terms[f"mb_trackid:{recording}"] = None
        if not recording or song.filing == F.ALREADY:
            terms[f"artist:{F.lead(song.artist).rstrip(',')}"] = None
    return tuple(terms)


def _matching(song: TP.Song, held: Sequence[Held]) -> list[Held]:
    recording = F.recording_of(song)
    if recording:
        same = [h for h in held if h.recording == recording]
        return same or [h for h in held if h.row.path == song.path]
    choice = F.choice_of(song)
    albums = {fold(song.album), *([fold(choice.release_title)] if choice else [])}
    return [h for h in held if F.alike(song, h.row) and fold(h.row.album) in albums]


def resolve(song: TP.Song, held: Sequence[Held]) -> Entry:
    """The one file that is this song, or why not. More than one candidate narrows
    to the chosen release, then to the path filing proved, and is never guessed."""
    if song.skip:
        return Entry(song.number, state=TP.SKIPPED, why="skipped")
    same = _matching(song, held)
    here = [h for h in same if h.row.present]
    if not here:
        if same:
            why = f"beets has a row at {same[0].row.path}, and the file is not there"
            return Entry(song.number, state=MISSING, why=why)
        unfiled = _not_filed(song)
        return unfiled or Entry(
            song.number, state=MISSING, why="the library has no row for this song"
        )
    choice = F.choice_of(song)
    release = [h for h in here if choice and h.release == choice.release]
    for pool in (here, release, [h for h in here if h.row.path == song.path]):
        if len(pool) == 1:
            return Entry(song.number, pool[0].row.path)
    named = ", ".join(h.row.path for h in here)
    why = f"{len(here)} rows could be this song: {named}"
    return Entry(song.number, state=AMBIGUOUS, why=why)


def with_paths(tape: TP.Tape, entries: Iterable[Entry]) -> TP.Tape:
    """A filed song's recorded path follows the library when it moves."""
    found = {e.number: e.path for e in entries if e.state == HERE}

    def moved(song: TP.Song) -> TP.Song:
        path = found.get(song.number, song.path)
        return replace(song, path=path) if song.filing in F.FINISHED else song

    return replace(tape, songs=tuple(moved(s) for s in tape.songs))


def archived(state: State, audio: bool) -> bool:
    """Done: the uncut file has gone from here. With `audio` still here, a clear was
    interrupted and archiving again finishes it."""
    return state.archived is not None and not audio


def archive_refusal(
    tape: TP.Tape, state: State, *, audio: bool, playlist_there: bool
) -> str:
    """Why this tape's uncut file may not be cleared yet, naming every song, or
    nothing. A skipped song was the person's decision, and holds nothing up."""
    if archived(state, audio):
        return f"{tape.name} is already archived"
    why = [] if audio else ["its uncut file is not here"]
    if not state.file:
        why.append("no playlist has been written")
    elif not playlist_there:
        why.append(f"its playlist {state.file} is gone - write it again")
    titles = {s.number: TP.label(s) for s in tape.songs}
    why += [
        f"{titles.get(g.number, f'song {g.number}')}: {g.why}"
        for g in state.left_out
        if g.state != TP.SKIPPED
    ]
    return f"{tape.name} cannot be archived yet. {'; '.join(why)}" if why else ""


def summary(tape: TP.Tape, state: State) -> list[str]:
    """What an album import's notes say about this tape: anything worth a look."""
    said = [f"{tape.name}: {state.note}"] if state.note else []
    if state.outcome == WRITTEN:
        said.append(f"{tape.name}: playlist rewritten, {state.songs} songs")
    if state.outcome not in (UNKNOWN, FAILED):
        said += [
            f"{tape.name}: song {g.number} is not in its playlist: {g.why}"
            for g in state.left_out
            if g.state != TP.SKIPPED
        ]
    return said
