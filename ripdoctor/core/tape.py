"""One uncut recording and the ordered list of songs in it. ADR-063."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, fields, replace
from itertools import accumulate, pairwise
from typing import Any

from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import DB_MIN, Envelope
from ripdoctor.core.match import (
    CATALOGUE,
    FAILED,
    MATCHED,
    NONE,
    OWNED,
    PERSON,
    Choice,
    Proposal,
    Wanted,
)
from ripdoctor.core.naming import MAX_COMPONENT, Unsafe, safe_filename, slug

LENGTH_TOLERANCE = 3.0
SILENCE_DBFS = -60.0
QUIET_WITHIN = 3.0

FOUND, EAR = "found", "ear"
AGREES, DOUBT = "agrees", "doubt"
PENDING = "pending"
START, END = "start", "end"
CUT, CUT_FAILED = "cut", "failed"
NOT_CUT, OUT_OF_DATE, SKIPPED = "not cut", "out of date", "skipped"
NO_CUT = "not needed"
ALREADY, FILED, UNKNOWN = "already", "filed", "unknown"
FINISHED = frozenset((ALREADY, FILED))

# In bytes, and short of a filesystem's 255: a superseded cut wears a timestamp.
CUT_TITLE_BYTES = 200

COLUMNS = ("playlist", "owned", "position", "title", "artist", "album", "duration")
_DURATION = re.compile(r"(?:(\d+):)?(\d+):([0-5]\d)", re.ASCII)

Span = tuple[float, float]


class BadList(ValueError):
    """A song list that cannot be read, and what to change in it."""


class EarSet(ValueError):
    """The change would discard a boundary a person set."""


class Locked(ValueError):
    """The change would alter a song the library holds or may hold."""


class ChoiceSet(EarSet):
    """The change would discard a release a person chose."""


class BadEdge(ValueError):
    """A boundary or a judgement that cannot be taken, and why."""


def _clean(text: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def _length(value: object, where: str) -> float:
    seconds = 0.0
    try:
        if isinstance(value, int | float) and not isinstance(value, bool):
            seconds = float(value)
        elif isinstance(value, str) and (found := _DURATION.fullmatch(value.strip())):
            hours, minutes, secs = found.groups()
            seconds = int(hours or 0) * 3600.0 + int(minutes) * 60.0 + int(secs)
    except (OverflowError, ValueError):
        # Digits past what a float or an int will take are no length either.
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        raise BadList(f"{where}: length {value!r} is not m:ss - write it like 3:45")
    return seconds


@dataclass(frozen=True, slots=True)
class CutRecord:
    """What a cut was made from, so a boundary that moves makes it out of date."""

    status: str
    file: str
    start: float
    end: float
    why: str = ""


@dataclass(frozen=True, slots=True)
class Song:
    number: int
    title: str
    artist: str
    album: str
    length: float
    start: float | None = None
    end: float | None = None
    set_by: str = FOUND
    delta: float | None = None
    verdict: str = ""
    filing: str = PENDING
    path: str = ""
    reason: str = ""
    proposal: Proposal | None = None
    settled_by: str = ""
    heard: bool = False
    skip: bool = False
    cut: CutRecord | None = None
    filing_note: str = ""
    import_begun: bool = False
    owes_ear: bool = False
    unconfirmed: str = ""
    no_cover: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Song:
        found, cut = data.get("proposal"), data.get("cut")
        return cls(
            **{
                **data,
                "proposal": Proposal.from_dict(found) if found else None,
                "cut": CutRecord(**cut) if cut else None,
            }
        )


def _song(where: str, number: int, fields: dict[str, Any], length: object) -> Song:
    title, artist = _clean(fields.get("title", "")), _clean(fields.get("artist", ""))
    if not title or not artist:
        raise BadList(f"{where}: a song needs a title and an artist - fill both in")
    return Song(
        number=number,
        title=title,
        artist=artist,
        album=_clean(fields.get("album", "")),
        length=_length(length, where),
    )


def songs_from(rows: object) -> tuple[Song, ...]:
    """A list built by hand, in the order given, numbered by that order."""
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise BadList("songs must be a list of title, artist, album and length")
    return tuple(
        _song(f"song {n}", n, row, row.get("length")) for n, row in enumerate(rows, 1)
    )


def _csv_rows(text: str) -> list[tuple[int, list[str]]]:
    reader = csv.reader(
        io.StringIO(text.removeprefix("﻿"), newline=""),
        strict=True,
        skipinitialspace=True,
    )
    try:
        return [(reader.line_num, r) for r in reader if any(c.strip() for c in r)]
    except csv.Error as e:
        raise BadList(
            f"row {reader.line_num}: {e} - a value holding a comma or a quote must "
            'be in double quotes, with its quotes doubled, like "a ""b"", c"'
        ) from e


def _absent(first: int, stop: int) -> str:
    if stop - first == 1:
        return f"position {first} is"
    return f"positions {first} to {stop - 1} are"


def parse_list(text: str) -> tuple[Song, ...]:
    """A CSV with the `COLUMNS`, one playlist per file. `owned` is not used."""
    rows = _csv_rows(text)
    if not rows:
        raise BadList("the list is empty - it needs a header and at least one song")
    header = [c.strip().lower() for c in rows[0][1]]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise BadList(
            f"row {rows[0][0]}: no {', '.join(missing)} column - "
            f"the first row must name {','.join(COLUMNS)}"
        )
    repeated = sorted({c for c in header if c and header.count(c) > 1})
    if repeated:
        raise BadList(
            f"row {rows[0][0]}: more than one {', '.join(repeated)} column - "
            "keep one of each"
        )

    placed: list[tuple[int, int, dict[str, str]]] = []
    for line, row in rows[1:]:
        if len(row) != len(header):
            raise BadList(
                f"row {line}: {len(row)} values where {len(header)} are expected - "
                "put double quotes around a value holding a comma"
            )
        fields = dict(zip(header, row, strict=True))
        try:
            position = int(fields["position"])
        except ValueError as e:
            raise BadList(
                f"row {line}: position {fields['position']!r} is not a whole number"
            ) from e
        placed.append((position, line, fields))
    if not placed:
        raise BadList("the list has a header and no songs - add at least one row")

    playlists = sorted({_clean(fields["playlist"]) for _, _, fields in placed})
    if len(playlists) > 1:
        raise BadList(
            f"this file holds {len(playlists)} playlists ({', '.join(playlists)}) - "
            "split it into one file per playlist"
        )

    placed.sort(key=lambda p: p[0])
    if placed[0][0] != 1:
        raise BadList(
            f"row {placed[0][1]}: positions begin at {placed[0][0]} - "
            f"{_absent(1, placed[0][0])} missing; add the songs or renumber from 1"
        )
    for (before, line_a, _), (after, line_b, _) in pairwise(placed):
        if after == before:
            raise BadList(
                f"row {line_b}: position {after} is also on row {line_a} - "
                "give each song its own position"
            )
        if after != before + 1:
            raise BadList(
                f"row {line_b}: positions jump from {before} to {after} - "
                f"{_absent(before + 1, after)} missing; add the songs or renumber"
            )
    return tuple(
        _song(f"row {line}", n, fields, fields["duration"])
        for n, (_, line, fields) in enumerate(placed, 1)
    )


def file_number(number: int, count: int) -> str:
    """Wide enough for the whole list, so text order is list order."""
    return f"{number:0{max(2, len(str(count)))}d}"


def tape_id(name: str) -> str:
    """A path component for a name that may hold anything, `:` `,` `.` included."""
    try:
        cut = slug("", name)[: MAX_COMPONENT // 2]
    except Unsafe:
        # A name with nothing a path can hold is still a good name: the document
        # keeps it, and only the directory falls back.
        return "tape"
    return re.sub(r"\.{2,}", ".", cut).strip("-._") or "tape"


@dataclass(frozen=True, slots=True)
class Gap:
    """A silence as found at `SILENCE_DBFS`, and the quiet interior within it.

    Finding at that level leaves a fade's tail outside the music, so song edges
    come from the interior. It covers whole windows, which takes `quiet_hi` one
    window past `hi`: `core.gaps` gives `hi` as the start of the last window.
    """

    lo: float
    hi: float
    quiet_lo: float
    quiet_hi: float

    @property
    def quiet(self) -> Span:
        return self.quiet_lo, self.quiet_hi


@dataclass(frozen=True, slots=True)
class Music:
    """Where the sound starts and stops, and the silences between.

    `start` and `end` are the edges of the tape's outer silences' quiet
    interiors; `found_start` and `found_end` are where `SILENCE_DBFS` put them.
    """

    start: float
    end: float
    gaps: tuple[Gap, ...]
    found_start: float | None = None
    found_end: float | None = None


def _quiet(levels: Sequence[float], first: int, last: int) -> tuple[int, int]:
    """Walk in from both ends to within `QUIET_WITHIN` of this stretch's minimum.

    The stretch's own floor, because some silences are low noise and not zero.
    """
    floor = min(levels[first : last + 1]) + QUIET_WITHIN
    while levels[first] > floor:
        first += 1
    while levels[last] > floor:
        last -= 1
    return first, last


def _gap(levels: Sequence[float], window: float, found: G.Gap) -> Gap:
    first, last = _quiet(levels, round(found.lo / window), round(found.hi / window))
    return Gap(
        round(found.lo, 3),
        round(found.hi, 3),
        round(first * window, 3),
        round((last + 1) * window, 3),
    )


def gaps_of(env: Envelope) -> Music:
    """Gaps at `SILENCE_DBFS` on the full lane, whatever the music's level.

    `below` is measured down from the music level, so asking for the distance
    from that level to the fixed threshold lands exactly on it.
    """
    levels = tuple(v if math.isfinite(v) else DB_MIN for v in env.levels)
    audible = [i for i, v in enumerate(levels) if v >= SILENCE_DBFS]
    if not audible:
        return Music(0.0, 0.0, ())
    clean = Envelope(levels, env.window)
    found = G.find(clean, below=clean.percentile(G.MUSIC_PCT) - SILENCE_DBFS)
    first, last = audible[0], audible[-1] + 1
    start, end = first * env.window, last * env.window
    after_lead = _quiet(levels, 0, first - 1)[1] + 1 if first else 0
    before_tail = (
        _quiet(levels, last, len(levels) - 1)[0] if last < len(levels) else last
    )
    return Music(
        round(after_lead * env.window, 3),
        round(before_tail * env.window, 3),
        tuple(
            _gap(levels, env.window, g) for g in found if start < g.lo and g.hi < end
        ),
        round(start, 3),
        round(end, 3),
    )


@dataclass(frozen=True, slots=True)
class Placed:
    start: float
    end: float
    delta: float
    verdict: str
    reason: str = ""
    also: str = ""


@dataclass(frozen=True, slots=True)
class Alignment:
    """`fits` is false when there were fewer stretches of music than songs.

    `groupings` counts the ways of laying the songs out with as few doubts as
    the one in `spans`.
    """

    spans: tuple[Placed, ...]
    fits: bool
    groupings: int = 1


def clock(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _judged(start: float, end: float, delta: float, *, grounded: bool = True) -> Placed:
    if not grounded:
        reason = "no silence where this boundary belongs - placed from the list length"
        return Placed(start, end, delta, DOUBT, reason)
    if abs(delta) <= LENGTH_TOLERANCE:
        return Placed(start, end, delta, AGREES)
    side = "longer" if delta > 0 else "shorter"
    reason = f"{abs(delta):.1f} s {side} than the list says - check the list or listen"
    return Placed(start, end, delta, DOUBT, reason)


def _also(starts: Sequence[float], ends: Sequence[float]) -> str:
    """Where the other groupings that fit as well would put this song's edges."""
    could = [
        *(f"start at {clock(t)}" for t in starts),
        *(f"end at {clock(t)}" for t in ends),
    ]
    if not could:
        return ""
    return f"Another grouping fits as well: it could {' or '.join(could)} - listen."


def _partition(
    pieces: Sequence[Span], lengths: Sequence[float]
) -> tuple[tuple[Placed, ...], int]:
    """Group pieces into songs in list order: fewest doubts, then smallest error.

    A group is measured by its extent or its sum of music, whichever is nearer
    the list: whether a list counts a silent passage inside a song varies by
    source. That only chooses a grouping. No verdict rests on it, and the delta
    reported is against the extent.
    """
    played = [0.0, *accumulate(hi - lo for lo, hi in pieces)]

    def off_by(before: int, used: int, length: float) -> float:
        extent = pieces[used - 1][1] - pieces[before][0]
        music = played[used] - played[before]
        return abs(min(extent - length, music - length, key=abs))

    count, songs = len(pieces), len(lengths)
    best: list[dict[int, tuple[int, float]]] = [{0: (0, 0.0)}]
    came_from: list[dict[int, int]] = [{}]
    for done, length in enumerate(lengths, 1):
        row: dict[int, tuple[int, float]] = {}
        back: dict[int, int] = {}
        for used in range(done, count - (songs - done) + 1):
            for before, (doubts, total) in best[done - 1].items():
                if before >= used:
                    continue
                delta = off_by(before, used, length)
                cost = (doubts + (delta > LENGTH_TOLERANCE), total + delta)
                if used not in row or cost < row[used]:
                    row[used], back[used] = cost, before
        best.append(row)
        came_from.append(back)

    # Back from the end through every step that keeps the fewest doubts: what is
    # left after each song is everywhere an equally good grouping could cut.
    tied: list[dict[int, int]] = [{} for _ in range(songs + 1)]
    tied[songs][count] = 1
    for done in range(songs, 0, -1):
        for used, ways in tied[done].items():
            for before, (doubts, _) in best[done - 1].items():
                missed = off_by(before, used, lengths[done - 1]) > LENGTH_TOLERANCE
                if before < used and doubts + missed == best[done][used][0]:
                    tied[done - 1][before] = tied[done - 1].get(before, 0) + ways

    spans: list[Placed] = []
    used = count
    for done in range(songs, 0, -1):
        before = came_from[done][used]
        first, last = pieces[before], pieces[used - 1]
        span = _judged(first[0], last[1], last[1] - first[0] - lengths[done - 1])
        also = _also(
            [pieces[b][0] for b in sorted(tied[done - 1]) if b != before],
            [pieces[u - 1][1] for u in sorted(tied[done]) if u != used],
        )
        spans.append(replace(span, verdict=DOUBT, also=also) if also else span)
        used = before
    return tuple(reversed(spans)), tied[0][0]


def _walk(
    gaps: Sequence[Span], start: float, end: float, lengths: Sequence[float]
) -> tuple[Placed, ...]:
    """Step through the list when gaps cannot place it: a boundary takes a nearby
    gap if there is one, or else falls where the length says and stays in doubt."""
    spans: list[Placed] = []
    cursor, grounded = start, True
    for index, length in enumerate(lengths):
        wanted = cursor + length
        near = [g for g in gaps if cursor < g[0] and g[1] < end]
        gap = min(near, key=lambda g: abs(g[0] - wanted), default=None)
        if index == len(lengths) - 1:
            stop, after, landed = end, end, True
        elif gap is not None and abs(gap[0] - wanted) <= LENGTH_TOLERANCE:
            stop, after, landed = gap[0], gap[1], True
        else:
            stop = min(max(wanted, cursor), end)
            after, landed = stop, False
        spans.append(
            _judged(cursor, stop, stop - cursor - length, grounded=grounded and landed)
        )
        cursor, grounded = after, landed
    return tuple(spans)


def align(
    gaps: Iterable[Span], start: float, end: float, lengths: Sequence[float]
) -> Alignment:
    """Lay the songs over the music in list order, never scaling a length."""
    ordered = sorted(gaps)
    edges = [start, *(g[1] for g in ordered)]
    stops = [*(g[0] for g in ordered), end]
    pieces = list(zip(edges, stops, strict=True))
    if not lengths:
        return Alignment((), True)
    if len(pieces) >= len(lengths):
        spans, groupings = _partition(pieces, lengths)
        return Alignment(spans, True, groupings)
    return Alignment(_walk(ordered, start, end, lengths), False)


def _owing(data: dict[str, Any]) -> tuple[Song, ...]:
    """A document from before the flag was kept meant every song, under a mismatch."""
    songs = tuple(Song.from_dict(s) for s in data["songs"])
    if data["mismatch"] and not any("owes_ear" in s for s in data["songs"]):
        return tuple(replace(s, owes_ear=True) for s in songs)
    return songs


@dataclass(frozen=True, slots=True)
class Tape:
    id: str
    name: str
    created: float = 0.0
    source: str = ""
    duration: float = 0.0
    music_start: float | None = None
    music_end: float | None = None
    found_start: float | None = None
    found_end: float | None = None
    songs: tuple[Song, ...] = ()
    gaps: tuple[Gap, ...] = ()
    inside: tuple[tuple[int, float, float], ...] = ()
    mismatch: str = ""
    prepare_failed: str = ""

    @property
    def has_ear(self) -> bool:
        return any(s.set_by == EAR for s in self.songs)

    def to_dict(self) -> dict[str, Any]:
        """Every field by name, and in plain values those that are not plain."""
        return {
            **{f.name: getattr(self, f.name) for f in fields(self)},
            "songs": [s.to_dict() for s in self.songs],
            "gaps": [[g.lo, g.hi] for g in self.gaps],
            "quiet": [list(g.quiet) for g in self.gaps],
            "inside": [{"song": n, "lo": lo, "hi": hi} for n, lo, hi in self.inside],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tape:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            created=float(data["created"]),
            source=str(data["source"]),
            duration=float(data["duration"]),
            music_start=data["music_start"],
            music_end=data["music_end"],
            found_start=data.get("found_start"),
            found_end=data.get("found_end"),
            songs=_owing(data),
            gaps=tuple(
                Gap(float(lo), float(hi), float(quiet_lo), float(quiet_hi))
                for (lo, hi), (quiet_lo, quiet_hi) in zip(
                    data["gaps"], data.get("quiet", data["gaps"]), strict=True
                )
            ),
            inside=tuple((i["song"], i["lo"], i["hi"]) for i in data["inside"]),
            mismatch=str(data["mismatch"]),
            prepare_failed=str(data.get("prepare_failed", "")),
        )


def named(tape_key: str, name: str, now: float) -> Tape:
    cleaned = _clean(name)
    if not cleaned:
        raise BadList("a tape needs a name")
    return Tape(id=tape_key, name=cleaned, created=now)


def _same_song(song: Song) -> tuple[str, str, str, int]:
    return song.title, song.artist, song.album, round(song.length)


def _rows_left(rows: list[Song], places: Collection[int]) -> list[Song]:
    """Which of several identical rows stay when a list has these places for them: a
    person's choices first, then a row whose place is still its own."""
    chosen = sorted(
        rows, key=lambda s: (s.settled_by != PERSON, s.number not in places)
    )
    return chosen[: len(places)]


def _seated(rows: list[Song], places: list[int]) -> dict[int, Song]:
    """A row keeps a place that is still its own and the rest pair off in order, unless
    that would swap identical rows, as a song put before them would have it."""
    left = sorted(_rows_left(rows, places), key=lambda s: s.number)
    own = {s.number: s for s in left if s.number in places}
    free = [p for p in places if p not in own]
    others = [s for s in left if s.number not in own]
    seated = {**own, **dict(zip(free, others, strict=False))}
    numbers = [seated[p].number for p in sorted(seated)]
    if numbers == sorted(numbers):
        return seated
    return dict(zip(places, left, strict=False))


def _keeping_matches(tape: Tape, songs: tuple[Song, ...]) -> tuple[Song, ...]:
    """A song the new list leaves as it was keeps its match, wherever it moved, and
    among identical rows each keeps its own."""
    old: dict[tuple[str, str, str, int], list[Song]] = {}
    for song in tape.songs:
        old.setdefault(_same_song(song), []).append(song)
    places: dict[tuple[str, str, str, int], list[int]] = {}
    for song in songs:
        places.setdefault(_same_song(song), []).append(song.number)
    kept: dict[int, Song] = {}
    for key, rows in old.items():
        kept.update(_seated(rows, places.get(key, [])))
    still = {s.number for s in kept.values()}
    lost = [
        s.title for s in tape.songs if s.settled_by == PERSON and s.number not in still
    ]
    if lost:
        raise ChoiceSet(
            f"you chose the release for {', '.join(lost)}, and this list changes or "
            "drops them - keep those songs as they were"
        )

    def kept_as(song: Song) -> Song:
        was = kept.get(song.number)
        if was is None:
            return song
        return replace(
            song, proposal=was.proposal, settled_by=was.settled_by, skip=was.skip
        )

    return tuple(kept_as(s) for s in songs)


def _said(song: Song) -> tuple[str, str, str, float]:
    return song.title, song.artist, song.album, song.length


def list_digest(tape: Tape) -> str:
    """The list as it stands, which a save names as what it was made from: a whole list
    sent from a page left open would otherwise put back whatever has changed since."""
    said = json.dumps([_said(s) for s in tape.songs], ensure_ascii=False)
    return hashlib.sha256(said.encode()).hexdigest()[:16]


def only_edits(tape: Tape, songs: tuple[Song, ...]) -> bool:
    """Whether this list is the old one with at most one row changed where it stands. A
    page saves a field at a time, so two rows changing at once is a new list, however it
    reads, and so is a row taking on what another row said: which audio belongs to which
    song would be different, and no boundary can stay for that."""
    if len(songs) != len(tape.songs):
        return False
    was = [_said(s) for s in tape.songs]
    changed = [
        new
        for old, new in zip(tape.songs, songs, strict=True)
        if _said(new) != _said(old)
    ]
    return len(changed) <= 1 and not any(_said(new) in was for new in changed)


def _redoubted(tape: Tape, song: Song) -> Song:
    """Edges that stay, measured against a length that changed."""
    if song.start is None or song.end is None:
        return song
    if song.set_by == EAR:
        return _ear_song(song)
    span = _judged(song.start, song.end, song.end - song.start - song.length)
    return _placed_song(song, span, [g.quiet for g in tape.gaps])


def _edited(tape: Tape, old: Song, new: Song) -> Song:
    """What a song's own edit changes: never its boundaries. Other words are another
    question to the catalogue, and another length unsettles only what it answered, since
    a person's choice was theirs whatever the list says."""
    if _said(new) == _said(old):
        return old
    held = edges_held(old)
    if held:
        raise Locked(f"its title, artist, album and length stay as they are: {held}")
    reworded = (new.title, new.artist, new.album) != (old.title, old.artist, old.album)
    song = replace(
        old, title=new.title, artist=new.artist, album=new.album, length=new.length
    )
    if reworded or old.settled_by != PERSON:
        song = replace(
            song,
            proposal=None,
            settled_by="",
            unconfirmed="",
            filing=PENDING,
            filing_note="",
        )
    return song if new.length == old.length else _redoubted(tape, song)


def edited(tape: Tape, songs: tuple[Song, ...]) -> Tape:
    """Rows changed where they stand. Every boundary stays, whoever set it."""
    return replace(
        tape,
        prepare_failed="",
        songs=tuple(
            _edited(tape, old, new) for old, new in zip(tape.songs, songs, strict=True)
        ),
    )


def relist(tape: Tape, songs: tuple[Song, ...], gone: Collection[str] = ()) -> Tape:
    """Take a new list. What was found for the old one no longer means anything, but a
    song the library holds or may hold keeps this tape from forgetting it. `gone` is the
    paths the library was asked about and no longer has."""
    if tape.has_ear:
        raise EarSet(
            f"{tape.name} has boundaries set by ear, and moving, adding or removing a "
            "song would change which audio belongs to which song - to start again, "
            "remove the tape"
        )
    held = [s for s in tape.songs if edges_held(s) and s.path not in gone]
    if held:
        raise Locked(
            f"{', '.join(label(s) for s in held)}: in the library, or may be, and a "
            "new list would forget that - remove that copy with beets, then load the "
            "list again; for a song not confirmed, press File again first"
        )
    return replace(
        tape,
        songs=_keeping_matches(tape, songs),
        prepare_failed="",
        gaps=(),
        inside=(),
        mismatch="",
        music_start=None,
        music_end=None,
        found_start=None,
        found_end=None,
    )


def _margin(start: float, stop: float, gaps: Iterable[Span]) -> Span:
    """Pull a stretch's edges out of any silence they were set inside."""
    for lo, hi in gaps:
        if lo <= start < hi:
            start = hi
        if lo < stop <= hi:
            stop = lo
    return start, stop


def _spanning(length: float, span: Placed, silences: Sequence[Span]) -> str:
    extent = span.end - span.start
    music = extent - sum(hi - lo for lo, hi in silences)
    where = ", ".join(f"{clock(lo)} to {clock(hi)}" for lo, hi in silences)
    return (
        f"Silence inside this song at {where}: a passage in it, damage, or a song "
        f"missing from the list - listen there. It measures {extent:.1f} s with the "
        f"silence and {music:.1f} s without; the list says {length:.1f} s."
    )


def _placed_song(song: Song, span: Placed, quiet: Sequence[Span]) -> Song:
    """A song that spans a silence is doubt whatever its length: only an ear can
    tell a passage from damage or from a song the list left out. What a person
    heard was these edges, so moving them takes the hearing away."""
    silences = [g for g in quiet if span.start < g[0] and g[1] < span.end]
    said = _spanning(song.length, span, silences) if silences else span.reason
    start, end = round(span.start, 3), round(span.end, 3)
    return replace(
        song,
        start=start,
        end=end,
        heard=song.heard and (song.start, song.end) == (start, end),
        delta=round(span.delta, 3),
        verdict=DOUBT if silences else span.verdict,
        reason=" ".join(part for part in (said, span.also) if part),
    )


def _ear_song(song: Song) -> Song:
    measured = (song.end or 0.0) - (song.start or 0.0)
    return replace(
        song, delta=round(measured - song.length, 3), verdict=AGREES, reason=""
    )


def _misfits(songs: Sequence[Song], inside: Sequence[tuple[int, float, float]]) -> int:
    silent = {s.number: 0.0 for s in songs}
    for number, lo, hi in inside:
        silent[number] += hi - lo
    return sum(
        s.verdict == DOUBT
        and min(abs(off := s.delta or 0.0), abs(off - silent[s.number]))
        > LENGTH_TOLERANCE
        for s in songs
    )


def _mismatch(
    songs: Sequence[Song],
    fits: bool,
    stretches: int,
    misfits: int,
    groupings: int,
) -> str:
    said: list[str] = []
    if not fits:
        said.append(
            f"{len(songs)} songs on the list and only {stretches} stretches of music "
            "- boundaries with no gap are placed from list lengths and need your ear"
        )
    elif misfits * 2 > len(songs):
        said.append(
            f"{misfits} of {len(songs)} songs disagree with their list lengths - "
            "check the list and its order; nothing has been reordered"
        )
    if groupings > 1:
        said.append(
            f"{groupings} groupings of the silences fit the list equally well - the "
            "nearest is shown, and every song they disagree over is marked doubt"
        )
    return "; ".join(said)


def _silences_in(
    songs: Sequence[Song], music: Music
) -> tuple[tuple[int, float, float], ...]:
    return tuple(
        (s.number, lo, hi)
        for lo, hi in (g.quiet for g in music.gaps)
        for s in songs
        if s.start is not None and s.end is not None and s.start < lo and hi < s.end
    )


def place(tape: Tape, music: Music) -> Tape:
    """Set every boundary not set by ear; those split the list and stay put.

    What the audio and the list dispute falls on the stretch that disputes it, or on
    the whole list when most of it disagrees, since then no length that happens to fit
    proves anything. A song the library holds or may hold was judged before it got
    there, and is not asked again.
    """
    out = list(tape.songs)
    disputed: set[int] = set()
    quiet = [g.quiet for g in music.gaps]
    fits = True
    groupings = 1
    cursor = music.start
    index = 0
    while index < len(out):
        if out[index].set_by == EAR:
            out[index] = _ear_song(out[index])
            cursor = out[index].end or cursor
            index += 1
            continue
        stop = index
        while stop < len(out) and out[stop].set_by != EAR:
            stop += 1
        after = out[stop].start if stop < len(out) else None
        limit = music.end if after is None else after
        lo, hi = _margin(cursor, limit, quiet)
        within = [g for g in quiet if lo < g[0] and g[1] < hi]
        found = align(within, lo, hi, [s.length for s in out[index:stop]])
        fits = fits and found.fits
        groupings *= found.groupings
        if not found.fits or found.groupings > 1:
            disputed.update(s.number for s in out[index:stop])
        for at, span in enumerate(found.spans, index):
            out[at] = _placed_song(out[at], span, quiet)
        index = stop

    inside = _silences_in(out, music)
    misfits = _misfits(out, inside)
    if misfits * 2 > len(out):
        disputed.update(s.number for s in out)
    mismatch = _mismatch(out, fits, len(music.gaps) + 1, misfits, groupings)
    return replace(
        tape,
        songs=tuple(
            replace(s, owes_ear=True)
            if s.number in disputed and not edges_held(s)
            else s
            for s in out
        ),
        music_start=music.start,
        music_end=music.end,
        found_start=music.found_start,
        found_end=music.found_end,
        gaps=music.gaps,
        inside=inside,
        mismatch=mismatch,
    )


def place_refusal(tape: Tape) -> str:
    """Why boundaries may not be found again, naming every song, or nothing. Finding
    them lays every song not set by ear, a song in the library among them."""
    held = [s for s in tape.songs if edges_held(s)]
    if not held:
        return ""
    return (
        f"{', '.join(label(s) for s in held)}: in the library, or may be, and finding "
        "boundaries again would move them - nothing was changed"
    )


def wanted(song: Song) -> Wanted:
    return Wanted(song.title, song.artist, song.album, song.length)


def all_settled(tape: Tape) -> bool:
    return bool(tape.songs) and all(s.settled_by for s in tape.songs)


def to_match(tape: Tape, *, again: bool = False) -> tuple[Song, ...]:
    """What a run has to ask about. A first run or a resume leaves a doubt already
    stated alone; `again` asks afresh about everything not settled."""
    return tuple(
        s
        for s in tape.songs
        if not s.settled_by
        and (again or s.proposal is None or s.proposal.status == FAILED)
    )


def is_placed(tape: Tape) -> bool:
    return (
        bool(tape.songs)
        and tape.music_start is not None
        and all(s.start is not None and s.end is not None for s in tape.songs)
    )


def wants_preparing(tape: Tape, audio: bool) -> bool:
    """With a list and audio both in, whether boundaries or a first lookup are owed. A
    run that failed has said why, and waits for a person, or every look at the page and
    every restart would start it again."""
    if not tape.songs or not audio or tape.prepare_failed:
        return False
    unplaced = not is_placed(tape) and not place_refusal(tape)
    return unplaced or any(s.proposal is None and not s.settled_by for s in tape.songs)


def with_proposals(
    tape: Tape, found: dict[int, Proposal], maybe: dict[int, str] | None = None
) -> Tape:
    """The catalogue's answers. A person's choice is not the catalogue's to change.
    `maybe` is a song's library copy that only a person can say is this song."""

    def answered(song: Song) -> Song:
        made = found.get(song.number)
        if made is None or song.settled_by == PERSON:
            return song
        by = CATALOGUE if made.status in (MATCHED, OWNED) else ""
        copy = (maybe or {}).get(song.number, "")
        return replace(song, proposal=made, settled_by=by, unconfirmed=copy)

    return replace(tape, songs=tuple(answered(s) for s in tape.songs))


def settle(tape: Tape, number: int, choice: Choice | None) -> Tape:
    """A person's answer for one song. No choice means file it from the list."""

    def chosen(song: Song) -> Song:
        if song.number != number:
            return song
        was = song.proposal or Proposal(NONE)
        status = MATCHED if choice else NONE
        return replace(
            song,
            proposal=replace(
                was, status=status, reason="", choice=choice, note="", copy=None
            ),
            settled_by=PERSON,
        )

    return replace(tape, songs=tuple(chosen(s) for s in tape.songs))


def offered(tape: Tape, number: int, found: tuple[Choice, ...]) -> Tape:
    """What a search found goes first among a song's options, changing nothing else."""

    def added(song: Song) -> Song:
        if song.number != number:
            return song
        was = song.proposal or Proposal(NONE)
        fresh = {(c.release, c.recording) for c in found}
        others = tuple(
            c for c in was.candidates if (c.release, c.recording) not in fresh
        )
        return replace(song, proposal=replace(was, candidates=(*found, *others)))

    return replace(tape, songs=tuple(added(s) for s in tape.songs))


def music_of(tape: Tape) -> Music:
    """What `place` was given, as the document keeps it."""
    if tape.music_start is None or tape.music_end is None:
        raise BadEdge(f"{tape.name} has no boundaries yet - find them first")
    return Music(
        tape.music_start,
        tape.music_end,
        tape.gaps,
        tape.found_start,
        tape.found_end,
    )


def _changed(tape: Tape, number: int, **changes: Any) -> Tape:
    songs = (replace(s, **changes) if s.number == number else s for s in tape.songs)
    return replace(tape, songs=tuple(songs))


def _song_at(tape: Tape, number: int) -> Song:
    for song in tape.songs:
        if song.number == number:
            return song
    raise BadEdge(f"{tape.name} has no song {number}")


def label(song: Song) -> str:
    return f"song {song.number} ({song.title})"


def edges_held(song: Song) -> str:
    """Why these edges stay as they are, or nothing. The library's copy was cut from
    them, and one not confirmed, or sent to beets with no answer yet, may be there."""
    if song.filing == UNKNOWN or song.import_begun:
        return (
            f"{label(song)} is not confirmed, and the library may already hold it, so "
            "its boundaries stay as they are until a read-back answers - press File "
            "again"
        )
    if song.filing not in FINISHED:
        return ""
    return (
        f"{label(song)} is already in the library, so its boundaries stay as they are "
        "- to cut it differently, remove that copy with beets, then load the list "
        "again, or if any boundary on this tape is yours, remove the tape and start it "
        "over"
    )


def _overlaps(other: Song, which: str, at: float) -> str:
    then = "which stays where it is, since the library holds it, or may"
    if not edges_held(other):
        then = "move that one first"
    return f"that overlaps song {other.number}, which {which} at {clock(at)} - {then}"


def _crossing(tape: Tape, song: Song, start: float, end: float) -> str:
    """Why a moved edge passes the song beside it, or nothing. A move needs the
    neighbour's edge out of its way first, which is never stuck: an edge moved away from
    its partner is not passing anything."""
    before = [s for s in tape.songs if s.number < song.number]
    after = [s for s in tape.songs if s.number > song.number]
    if before and start != song.start and start < (at := before[-1].end or 0.0):
        return _overlaps(before[-1], "ends", at)
    if after and end != song.end and end > (at := after[0].start or end):
        return _overlaps(after[0], "starts", at)
    return ""


def still_as_seen(tape: Tape, song: Song, seen: Span) -> None:
    """Refuse a move made from edges that are no longer the song's."""
    music_of(tape)
    if seen != (song.start, song.end):
        raise BadEdge(
            f"{label(song)} has changed since this page loaded it: it is now "
            f"{clock(song.start or 0.0)} to {clock(song.end or 0.0)}, and nothing was "
            "moved - look again"
        )


def set_edges(tape: Tape, number: int, start: float, end: float) -> Tape:
    """A person's boundary. It moves this song and no other: what a move leaves between
    two songs belongs to neither, which is right for a gap, and whose audio that is only
    a person can say.

    No edge passes the song beside it, whoever set that one, and ear-set songs keep list
    order against each other, near or far, because `place` relies on them to split the
    list. Nothing the library holds or may hold moves.
    """
    music = music_of(tape)
    song = _song_at(tape, number)
    held = edges_held(song)
    if held:
        raise BadEdge(held)
    if not (math.isfinite(start) and math.isfinite(end)):
        raise BadEdge("a boundary has to be a number of seconds")
    if not 0.0 <= start < end <= tape.duration:
        raise BadEdge(
            f"{start:.3f} to {end:.3f} does not fit: a song starts before it ends, "
            f"inside the tape's {tape.duration:.3f} s"
        )
    for other in tape.songs:
        if other.set_by != EAR or other.number == number:
            continue
        before = other.number < number and start < (other.end or 0.0)
        after = other.number > number and end > (other.start or 0.0)
        if before or after:
            raise BadEdge(
                f"that overlaps song {other.number}, which you set by ear to "
                f"{other.start:.3f} to {other.end:.3f} - change that one first"
            )
    crossing = _crossing(tape, song, start, end)
    if crossing:
        raise BadEdge(crossing)
    moved = _ear_song(
        replace(song, start=round(start, 3), end=round(end, 3), set_by=EAR, heard=False)
    )
    songs = tuple(moved if s.number == number else s for s in tape.songs)
    changed = replace(tape, songs=songs, inside=_silences_in(songs, music))
    if tape.mismatch and not place(changed, music).mismatch:
        return replace(changed, mismatch="")
    return changed


def hear(tape: Tape, number: int, heard: bool = True) -> Tape:
    """A person accepts the found edges. They stay found, and the verdict stays
    what was measured: `doubtful` is what the hearing answers."""
    song = _song_at(tape, number)
    if song.start is None:
        raise BadEdge(f"song {number} has no boundaries yet - find them first")
    if song.set_by == EAR:
        raise BadEdge(f"song {number} is already set by ear")
    return _changed(tape, number, heard=heard)


def skip(tape: Tape, number: int, skipped: bool = True) -> Tape:
    _song_at(tape, number)
    return _changed(tape, number, skip=skipped)


def needs_no_cut(song: Song) -> bool:
    """A lookup found this in the library, and File finds it there again and imports
    nothing. If it has gone by then, the song goes back to choosing an album, which
    makes it a song to cut like any other."""
    return bool(song.proposal and song.proposal.status == OWNED)


def doubtful(song: Song) -> bool:
    """Waiting for an ear: its own doubt, or a stretch of the list its audio disagrees
    with, where a length that happens to fit proves nothing. Setting its neighbours can
    end the disagreement and proves nothing about this song, so one flagged that way
    stays flagged until it is judged itself. What reached the library was judged
    before it got there."""
    if song.skip or song.heard or song.set_by == EAR or edges_held(song):
        return False
    if needs_no_cut(song):
        return False
    return song.verdict == DOUBT or song.owes_ear


def ear_order(tape: Tape) -> tuple[Song, ...]:
    """Doubts first, then the rest of what a mismatch flags, the skipped last."""

    def rank(song: Song) -> tuple[int, int]:
        if song.skip:
            return 3, song.number
        if doubtful(song):
            return (0 if song.verdict == DOUBT else 1), song.number
        return 2, song.number

    return tuple(sorted(tape.songs, key=rank))


def cut_name(song: Song, count: int) -> str:
    title = safe_filename(song.title).encode()[:CUT_TITLE_BYTES]
    return f"{file_number(song.number, count)} {title.decode(errors='ignore')}.flac"


def cut_refusal(tape: Tape) -> str:
    """Why this tape may not be cut yet, naming every song, or nothing."""
    active = [s for s in tape.songs if not s.skip]
    if not active:
        return "every song is marked skip, so there is nothing to cut"
    active = [s for s in active if not needs_no_cut(s)]
    groups = (
        (
            "no boundaries yet - find them first",
            [s for s in active if s.start is None or s.end is None],
        ),
        (
            "no room between its boundaries - set them by ear",
            [
                s
                for s in active
                if s.start is not None and s.end is not None and s.end <= s.start
            ],
        ),
        (
            "still in doubt - listen, then move it, accept it or skip it",
            [s for s in active if doubtful(s)],
        ),
    )
    said = [
        f"{', '.join(label(s) for s in songs)}: {why}" for why, songs in groups if songs
    ]
    return f"{tape.name} cannot be cut yet. {'; '.join(said)}" if said else ""


def is_current(song: Song, count: int) -> bool:
    """A verified cut of exactly these edges, under the name it should have."""
    made = song.cut
    return (
        made is not None
        and made.status == CUT
        and made.file == cut_name(song, count)
        and (made.start, made.end) == (song.start, song.end)
    )


def cut_status(song: Song, count: int, on_disk: Collection[str]) -> str:
    if song.skip:
        return SKIPPED
    if needs_no_cut(song):
        return NO_CUT
    if song.cut is None:
        return NOT_CUT
    if song.cut.status == CUT_FAILED:
        return CUT_FAILED
    if not is_current(song, count):
        return OUT_OF_DATE
    return CUT if song.cut.file in on_disk else NOT_CUT


def all_cut(tape: Tape, on_disk: Collection[str]) -> bool:
    count = len(tape.songs)
    statuses = [cut_status(s, count, on_disk) for s in tape.songs if not s.skip]
    return bool(statuses) and all(status in (CUT, NO_CUT) for status in statuses)


def with_cut(tape: Tape, number: int, record: CutRecord | None) -> Tape:
    return _changed(tape, number, cut=record)
