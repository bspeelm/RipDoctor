"""What filing a tape's songs takes, and how each song came out. ADR-063."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ripdoctor.core.match import (
    ELSEWHERE,
    MATCHED,
    NONE,
    OWNED,
    PERSON,
    REVIEW,
    VARIOUS,
    Choice,
    Copy,
    Held,
    Proposal,
    album_agreement,
    fold,
    names,
    options,
    search_album,
    search_artists,
)
from ripdoctor.core.tape import (
    ALREADY as ALREADY,
)
from ripdoctor.core.tape import (
    CUT,
    LENGTH_TOLERANCE,
    NO_CUT,
    PENDING,
    SKIPPED,
    Song,
    Tape,
    cut_refusal,
    cut_status,
    label,
    settle,
)
from ripdoctor.core.tape import (
    FILED as FILED,
)
from ripdoctor.core.tape import (
    FINISHED as FINISHED,
)
from ripdoctor.core.tape import (
    UNKNOWN as UNKNOWN,
)
from ripdoctor.core.tape import (
    wanted as wanted_of,
)

FAILED = "failed"
FILE = "file"
NEW_ALBUM, JOIN, BESIDE, ADDING = "new album", "join", "beside", "adding"
_SHOWN = {FAILED: 0, UNKNOWN: 0, PENDING: 1, FILED: 2, ALREADY: 3, SKIPPED: 4}


@dataclass(frozen=True, slots=True)
class Row:
    """One library row. `present` is whether its file is on disk, and `home` whether
    that path is under the library's root and outside the tape's own directories.
    `track` is 0 and an id empty where beets holds none."""

    title: str
    artist: str
    album: str
    length: float | None
    path: str
    present: bool = True
    home: bool = True
    recording: str = ""
    track: int = 0
    release: str = ""
    album_artist: str = ""
    row: str = ""
    album_row: str = ""


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
    """`unconfirmed` is a row that only a person can say is this song."""

    action: str
    path: str = ""
    note: str = ""
    unconfirmed: str = ""


def lead(artist: str) -> str:
    return search_artists(artist)[-1]


def term(field: str, value: str) -> str:
    """A beets query for any row whose field holds `value`, and what comes back is
    judged here. A plain substring is compared by SQLite, which folds the case of ASCII
    alone and so misses an accented capital. A regular expression does not, and escaping
    takes care of what beets reads as syntax: a comma is an OR with the whole library,
    and `\\:` it unescapes before the expression is compiled."""
    escaped = re.escape(value).replace(",", r"\x2c").replace(":", r"\x3a")
    return f"{field}::(?i){escaped}"


def choice_of(song: Song) -> Choice | None:
    choice = song.proposal.choice if song.proposal else None
    return choice if choice and choice.release else None


def recording_of(song: Song) -> str:
    choice = choice_of(song)
    return choice.recording if choice else ""


def _albums(tape: Tape) -> dict[tuple[str, str, str], list[Track]]:
    """A from-list song is numbered among all its album's songs, finished ones too,
    so a retry gives it the same number."""
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
    return albums


def track_numbers(tape: Tape) -> dict[int, int]:
    """The number each song not skipped is filed under, by its place on the tape."""
    return {
        t.song.number: t.number for tracks in _albums(tape).values() for t in tracks
    }


def runs(tape: Tape) -> tuple[Run, ...]:
    """What is unfinished, one run per album. Two releases in one run makes beets ask
    which, and a second lookup-off run for one album makes a second album row."""
    planned = []
    for (release, artist, album), tracks in _albums(tape).items():
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


def alike(song: Song, row: Row) -> bool:
    """The same title and artist, by the reading the pre-check uses."""
    return _cleaned(row.title) in _titles(song) and _same_artist(
        song.artist, row.artist
    )


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


def same_length(song: Song, row: Row) -> bool:
    return row.length is not None and abs(row.length - song.length) <= LENGTH_TOLERANCE


def _unconfirmed(row: Row, recording: str) -> Verdict:
    return Verdict(
        FAILED,
        note=f"{row.path} has this title, artist and length and no recording id - "
        "listen to it, and if it is this song use that copy, or set its mb_trackid "
        f"with beet modify to {recording} and file again, or skip this song",
        unconfirmed=row.path,
    )


def precheck(
    song: Song,
    by_id: Sequence[Row],
    by_name: Sequence[Row],
    taken: Collection[str] = (),
) -> Verdict:
    """Whether the library has this song. Merging a copy of an owned one files it
    as another track, so this answers before every import.

    A recording id is enough, and a song that has one is owned by nothing else: a
    row of another id is another recording, and a row of none is for a person to
    confirm. With no id to go by, a cleaned title also hits a live album, so the
    length must agree, and a row with no length to compare is said aloud. Only a
    file that is there and in the library counts as owning it, and a row in `taken`
    answers for another song on this tape.
    """
    free = [r for r in by_id if r.path not in taken]
    for row in free:
        if row.present and row.home:
            return Verdict(ALREADY, row.path)
    if free:
        return _unusable(free[0])
    if by_id:
        return Verdict(
            FAILED,
            note=f"the only row with this recording, {by_id[0].path}, answers for "
            "another song on this tape - skip one of them",
        )

    recording = recording_of(song)
    named = [
        r
        for r in by_name
        if r.path not in taken and alike(song, r) and not (recording and r.recording)
    ]
    agreeing = [r for r in named if same_length(song, r)]
    for row in agreeing:
        if row.present and row.home:
            if recording:
                return _unconfirmed(row, recording)
            return Verdict(ALREADY, row.path, "matched by title, artist and length")
    if agreeing:
        return _unusable(agreeing[0])
    for row in named:
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


def claimed(tape: Tape, song: Song) -> set[str]:
    """Rows other songs on this tape have been recorded against, or found to own."""
    paths = set()
    for other in tape.songs:
        copy = other.proposal.copy if other.proposal else None
        if other.number != song.number and other.filing in FINISHED:
            paths.add(other.path)
        elif other.number != song.number and copy:
            paths.add(copy.path)
    return paths


def held_albums(rows: Sequence[Row]) -> tuple[Held, ...]:
    """What these rows say the library has, one to an album row."""
    counted: dict[tuple[str, str, str], list[int]] = {}
    for row in rows:
        key = (row.release, row.album, row.album_artist or row.artist)
        counted.setdefault(key, []).append(row.track)
    return tuple(
        Held(*key, len(numbers), tuple(sorted(n for n in numbers if n)))
        for key, numbers in counted.items()
    )


def _owning(made: Proposal, row: Row, choice: Choice | None) -> Proposal:
    copy = Copy(row.album_artist or row.artist, row.album, row.path, row.release)
    return replace(made, status=OWNED, reason="", note="", choice=choice, copy=copy)


def _maybe(made: Proposal, path: str) -> tuple[Proposal, str]:
    """A copy only a person can vouch for. A failed lookup stays one, to be retried."""
    said = (
        f"your library may already have this at {path}, and nothing proves it is this "
        "song - listen, then use that copy or choose a release"
    )
    if made.status == FAILED:
        return replace(made, library_note=said), path
    return replace(made, status=REVIEW, reason=said), path


def at_lookup(
    song: Song,
    made: Proposal,
    by_id: Sequence[Row],
    by_name: Sequence[Row],
    taken: Collection[str] = (),
) -> tuple[Proposal, str]:
    """A proposal, as the pre-check would answer it at File, and any copy only a person
    can vouch for. A song is owned with nobody looking only on the recording id a match
    chose. Anything else on offer was held back for a reason, another version often
    enough, and a row of that id or of none that only agrees on title, artist and length
    can be just that version, so it is for a person."""
    if made.status == MATCHED:
        said = precheck(replace(song, proposal=made), by_id, by_name, taken)
        if said.action == ALREADY:
            row = next(r for r in (*by_id, *by_name) if r.path == said.path)
            return _owning(made, row, made.choice if row in by_id else None), ""
        if said.unconfirmed:
            return _maybe(made, said.unconfirmed)
    else:
        said = precheck(replace(song, proposal=None), (), by_name, taken)
        if said.action == ALREADY:
            return _maybe(made, said.path)
    if said.action == FAILED and (made.status == MATCHED or song.album):
        return replace(made, library_note=said.note), ""
    return made, ""


def found_owned(song: Song) -> bool:
    """A lookup said the library had this. Nothing about it was ever chosen to be filed,
    so if that stops being true it is a person's question again."""
    return bool(song.proposal and song.proposal.status == OWNED)


def copy_gone(tape: Tape, song: Song) -> Tape:
    """Failed, and choosing an album again, because filing it from the list or from the
    lookup's guess is not what anybody was asked."""
    where = song.proposal.copy.path if song.proposal and song.proposal.copy else ""
    said = (
        f"the copy found in your library{f' at {where}' if where else ''} is not there "
        "any more, so this was not filed - choose an album for it"
    )
    told = recorded(tape, song.number, FAILED, note=said)

    def asked(other: Song) -> Song:
        if other.number != song.number or other.proposal is None:
            return other
        made = replace(other.proposal, status=REVIEW, reason=said, copy=None)
        return replace(other, proposal=made, settled_by="")

    return replace(told, songs=tuple(asked(s) for s in told.songs))


def _beside_an_album(song: Song, choice: Choice) -> tuple[Held, Choice] | None:
    """The album held and the release of it that has this song, whichever of that
    release or its single is chosen. None when the choice is what the library holds."""
    made = song.proposal
    if made is None:
        return None
    held = {h.release for h in made.held}
    if choice.release in held:
        return None
    for c in (choice, *made.candidates):
        if c.kind == "single" or c.release in held or not c.number:
            continue
        if c is not choice and c.recording != choice.recording:
            continue
        for album in made.held:
            if album_agreement(album.album, c.release_title) != ELSEWHERE:
                return album, c
    return None


def _where_it_goes(made: Proposal, held: Held, album: Choice) -> str:
    """The same folder needs paths with no `%aunique`, and the same album artist and
    album title. A release by Various Artists takes beets' `comp` path instead, and with
    no answer about paths this says only what is true either way."""
    place = f"track {album.number}"
    keeps = f"Your {held.tracks} songs don't change."
    artist = fold(album.album_artist)
    if made.apart is None or artist == VARIOUS:
        return (
            f"goes in with your {held.album} or in a folder beside it, depending on "
            f"your beets paths, as {place}. {keeps}"
        )
    alike = artist == fold(held.album_artist) and (
        fold(album.release_title) == fold(held.album)
    )
    if made.apart or not alike:
        return (
            f"goes in a folder of its own, apart from your {held.album}, as {place}. "
            f"{keeps}"
        )
    beside = f", next to your other {place}" if album.number in held.numbers else ""
    return f"goes in your {held.album} folder as {place}{beside}. {keeps}"


def edition_note(song: Song) -> dict[str, Any] | None:
    """The choice a release beside an album already held puts to a person, in what each
    answer does to their folders."""
    choice = choice_of(song)
    found = None if choice is None else _beside_an_album(song, choice)
    if choice is None or found is None or song.proposal is None:
        return None
    held, album = found
    single = next(
        (
            c
            for c in song.proposal.candidates
            if c.kind == "single"
            and c.recording == album.recording
            and c.release != album.release
        ),
        None,
    )
    version = f"{album.tracks}-track version" if album.tracks else "other version"
    choices = [
        {
            "label": f"Add to {held.album}",
            "said": _where_it_goes(song.proposal, held, album),
            "release": album.release,
            "recording": album.recording,
            "current": choice.release == album.release,
        }
    ]
    if single is not None:
        choices.append(
            {
                "label": "File as a single",
                "said": (
                    "goes in its own folder, "
                    f"{single.album_artist} / {single.release_title}."
                ),
                "release": single.release,
                "recording": single.recording,
                "current": choice.release == single.release,
            }
        )
    return {
        "text": (
            f"You have the {held.tracks}-track {held.album}. "
            f"This song is only on the {version}."
        ),
        "album": held.album,
        "track": album.number,
        "tracks": album.tracks,
        "single": None if single is None else single.release,
        "choices": choices,
    }


def release_view(song: Song) -> dict[str, Any]:
    """What a song's row shows of its release, beyond the document's own fields."""
    made = song.proposal
    copy = made.copy if made else None
    return {
        "options": options(wanted_of(song), made),
        "copy": copy
        and {
            "album_artist": copy.album_artist,
            "album": copy.album,
            "path": copy.path,
            "release": copy.release,
        },
        "maybe_copy": song.unconfirmed,
        "edition_note": edition_note(song),
        "note": made.note if made else "",
        "library_note": made.library_note if made else "",
    }


def _is_the_song(song: Song, row: Row, track: int) -> bool:
    """A from-list song's number is ours, written to the file and kept by a lookup-off
    run, where a release may number its tracks otherwise."""
    if recording_of(song):
        return True
    if choice_of(song):
        return _cleaned(row.title) in _titles(song)
    return (
        _cleaned(row.title) in _titles(song)
        and fold(row.album) == fold(song.album)
        and (not track or not row.track or row.track == track)
    )


def _rows_of(
    song: Song,
    rows: Sequence[Row],
    before: Collection[str],
    taken: Collection[str],
    track: int,
    *,
    home: bool,
) -> list[Row]:
    """The rows this could be, told apart by track number and then by length, since
    one album may have the same title twice."""
    pool = [
        r
        for r in rows
        if r.present
        and r.path not in before
        and r.path not in taken
        and r.home == home
        and _is_the_song(song, r, track)
    ]
    pool = [r for r in pool if track and r.track == track] or pool
    return [r for r in pool if same_length(song, r)] or pool


def arrivals(
    song: Song,
    rows: Sequence[Row],
    before: Collection[str] = (),
    *,
    taken: Collection[str] = (),
    track: int = 0,
) -> list[str]:
    """Where the song could be now, more than one when nothing tells them apart. A row
    that was already there is not it, and neither is one another song has claimed,
    whose file is missing or is not in the library."""
    return [r.path for r in _rows_of(song, rows, before, taken, track, home=True)]


def arrived(song: Song, rows: Sequence[Row], before: Collection[str] = ()) -> str:
    found = arrivals(song, rows, before)
    return found[0] if len(found) == 1 else ""


def astray(
    song: Song,
    rows: Sequence[Row],
    before: Collection[str] = (),
    *,
    taken: Collection[str] = (),
    track: int = 0,
) -> str:
    """Where an import put the song if that is not in the library, or nothing."""
    found = _rows_of(song, rows, before, taken, track, home=False)
    return found[0].path if found else ""


def _as_cut(song: Song, row: Row) -> bool:
    if row.length is None or song.cut is None:
        return False
    return abs(row.length - (song.cut.end - song.cut.start)) <= LENGTH_TOLERANCE


def recovered(
    song: Song,
    rows: Sequence[Row],
    *,
    taken: Collection[str] = (),
    track: int = 0,
) -> list[str]:
    """Where an earlier attempt's import may have put the song. Nothing says what was
    there before that attempt, so with no id the row's length must agree, with the
    list or with the cut handed over: a person may have heard a list be wrong."""
    return arrivals(
        song,
        [
            r
            for r in rows
            if recording_of(song) or same_length(song, r) or _as_cut(song, r)
        ],
        taken=taken,
        track=track,
    )


def second_row(run: Run, rows: Sequence[Row]) -> str:
    """A lookup-off run never joins an album, so anything already under this one means
    another album row. Not counted, because these rows are tracks and there may be many
    album rows behind them."""
    if any(fold(r.album) == fold(run.album) for r in rows):
        return (
            f"beets already held {run.album}, so this is one more album entry for it, "
            "and the tracks already there did not change"
        )
    return ""


def joining(run: Run, rows: Sequence[Row]) -> str:
    """How a pinned run meets the album rows already there, of any album artist. Beets
    asks about a duplicate by the artist's own name, where a release may credit another,
    so only the album's name can be compared here. Merging rematches every row of that
    album against the pinned release, so an owned track is renamed, renumbered and
    retagged unless all of them are already that release. Anything else goes beside
    them, and an empty or foreign album id is not that release. A release held among
    others cannot be merged for the same reason, and is added to its own row by hand."""
    held = {r.release for r in rows if fold(r.album) == fold(run.album)}
    if not held:
        return NEW_ALBUM
    if held == {run.release}:
        return JOIN
    return ADDING if run.release in held else BESIDE


def filed_copy(
    rows: Sequence[Row], artist: str, album: str, release: str = ""
) -> tuple[Row, ...]:
    """What the library holds of exactly this record: by the whole release id, or with
    none by the album artist and album, folded. A replace removes what this chooses, so
    a title inside a longer one, or the same title by someone else, is never it."""
    if release:
        return tuple(r for r in rows if r.release == release)
    if not album.strip():
        return ()
    who, what = fold(artist), fold(album)
    return tuple(
        r for r in rows if fold(r.album_artist) == who and fold(r.album) == what
    )


def replace_refusal(
    copy: Sequence[Row], artist: str, album: str, release: str = ""
) -> str:
    """Why these rows may not be removed as one record, or nothing. Rows of several
    releases under one name are editions, and only a release says which is meant."""
    if not copy:
        named = f"release {release}" if release else f"{artist} - {album}"
        return (
            f"the library holds nothing that is exactly {named}, so nothing is removed"
        )
    releases = {r.release for r in copy}
    if not release and len(releases) > 1:
        return (
            f"{artist} - {album} is {len(releases)} different releases in the library, "
            "so nothing is removed - pin the release to replace, or choose Keep both"
        )
    return ""


def beside_note(run: Run) -> str:
    return (
        f"beets already held an album called {run.album}, so this is a separate album "
        "entry, and the tracks already there did not change"
    )


def recorded(
    tape: Tape,
    number: int,
    state: str,
    path: str = "",
    note: str = "",
    *,
    keep_begun: bool = False,
    unconfirmed: str = "",
) -> Tape:
    """One song's outcome. Pending or failed may become anything; finished stays.

    `import_begun` lasts until the library has really answered, because it is what
    keeps a retry from importing over a row the first run may have left.
    """

    def told(song: Song) -> Song:
        if song.number != number:
            return song
        if song.filing in FINISHED:
            raise ValueError(f"{label(song)} is already {song.filing}")
        return replace(
            song,
            filing=state,
            path=path,
            filing_note=note,
            import_begun=song.import_begun and (keep_begun or state == UNKNOWN),
            unconfirmed=unconfirmed,
        )

    return replace(tape, songs=tuple(told(s) for s in tape.songs))


def with_cover_known(tape: Tape, missing: Collection[int]) -> Tape:
    """Which of its filed songs were looked at and found to have no cover."""
    return replace(
        tape,
        songs=tuple(
            replace(s, no_cover=s.number in missing) if s.filing == FILED else s
            for s in tape.songs
        ),
    )


def cover_given(tape: Tape, files: Collection[str]) -> Tape:
    """One cover answers for every song of this tape on that album."""
    return replace(
        tape,
        songs=tuple(
            replace(s, no_cover=False) if s.path in files else s for s in tape.songs
        ),
    )


def person_owns(tape: Tape, number: int) -> Tape:
    """A person heard the row the pre-check could not vouch for, and says it is this
    song. A finished song is never run again, so no later job can move this."""

    def owned(song: Song) -> Song:
        if song.number != number:
            return song
        return replace(
            song,
            filing=ALREADY,
            path=song.unconfirmed,
            filing_note="your choice: the copy already in your library",
            settled_by=PERSON,
            import_begun=False,
            unconfirmed="",
        )

    return replace(tape, songs=tuple(owned(s) for s in tape.songs))


def begun(tape: Tape, numbers: Collection[int]) -> Tape:
    """These songs are going to beets. One found in the library after a restart, with
    this still set, got there by this tape and is filed, unlike a song it merely already
    owned."""
    return replace(
        tape,
        songs=tuple(
            replace(s, import_begun=True) if s.number in numbers else s
            for s in tape.songs
        ),
    )


def file_refusal(tape: Tape, on_disk: Collection[str]) -> str:
    """Why this tape may not be filed yet, naming every song, or nothing. A cut needs no
    release, and filing needs both."""
    count = len(tape.songs)
    active = [s for s in tape.songs if not s.skip]
    groups = (
        (
            "no settled release - choose one, or file it from your list",
            [s for s in active if not s.settled_by],
        ),
        (
            "not cut - cut them first",
            [s for s in active if cut_status(s, count, on_disk) not in (CUT, NO_CUT)],
        ),
    )
    said = [
        f"{', '.join(label(s) for s in songs)}: {why}" for why, songs in groups if songs
    ]
    filing = f"{tape.name} cannot be filed yet. {'; '.join(said)}" if said else ""
    return " ".join(filter(None, (cut_refusal(tape), filing)))


def choice_refusal(song: Song) -> str:
    """Why this song's release may not change, or nothing. A finished song's choice
    is what the library's copy was filed as, and later jobs find that copy by it. One
    not confirmed, or sent to beets with no answer yet, may have such a copy, which
    another choice would not find."""
    if song.filing == UNKNOWN or song.import_begun:
        return (
            f"{label(song)} is not confirmed, and the library may already hold it, so "
            "its release stays as it is until a read-back answers - press File again"
        )
    if song.filing not in FINISHED:
        return ""
    return (
        f"{label(song)} is already in the library, so its release stays as it is - to "
        "file another edition, remove that copy with beets and load the list again"
    )


def no_match_at_all(tape: Tape) -> tuple[Song, ...]:
    """Songs the catalogue found nothing for and nobody has answered. A song with a
    choice to make between options is still somebody's to make, and one whose release
    may not change is left out as a row would refuse it."""
    return tuple(
        s
        for s in tape.songs
        if s.proposal
        and s.proposal.status == NONE
        and not s.settled_by
        and not s.skip
        and not choice_refusal(s)
    )


def from_list_view(tape: Tape) -> dict[str, Any]:
    """Which of those can go from the list, and how many cannot for having no album to
    file under, which is said at File and is better said here."""
    found = no_match_at_all(tape)
    return {
        "numbers": [s.number for s in found if s.album],
        "no_album": sum(not s.album for s in found),
    }


def all_from_list(tape: Tape, numbers: Collection[int]) -> Tape:
    """The ones a page asked about that are still as it saw them."""
    for number in from_list_view(tape)["numbers"]:
        if number in numbers:
            tape = settle(tape, number, None)
    return tape


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
            "unconfirmed": s.unconfirmed,
            "settled_by": s.settled_by,
            "no_cover": s.no_cover and s.filing == FILED,
        }
        for s in ordered
    ]
