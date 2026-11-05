"""One uncut recording and the ordered list of songs in it. ADR-063."""

from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import accumulate, pairwise
from typing import Any

from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import DB_MIN, Envelope
from ripdoctor.core.naming import MAX_COMPONENT, Unsafe, slug

LENGTH_TOLERANCE = 3.0
SILENCE_DBFS = -60.0

FOUND, EAR = "found", "ear"
AGREES, DOUBT = "agrees", "doubt"
PENDING = "pending"

COLUMNS = ("playlist", "owned", "position", "title", "artist", "album", "duration")
_DURATION = re.compile(r"(?:(\d+):)?(\d+):([0-5]\d)", re.ASCII)

Span = tuple[float, float]


class BadList(ValueError):
    """A song list that cannot be read, and what to change in it."""


class EarSet(ValueError):
    """The change would discard a boundary a person set."""


def _clean(text: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def _length(value: object, where: str) -> float:
    seconds = 0.0
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and (found := _DURATION.fullmatch(value.strip())):
        hours, minutes, secs = found.groups()
        seconds = int(hours or 0) * 3600.0 + int(minutes) * 60.0 + int(secs)
    if not math.isfinite(seconds) or seconds <= 0:
        raise BadList(f"{where}: length {value!r} is not m:ss - write it like 3:45")
    return seconds


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "length": self.length,
            "start": self.start,
            "end": self.end,
            "set_by": self.set_by,
            "delta": self.delta,
            "verdict": self.verdict,
            "filing": self.filing,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Song:
        return cls(**data)


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
class Music:
    """Where the sound starts and stops, and the silences between."""

    start: float
    end: float
    gaps: tuple[Span, ...]


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
    start, end = audible[0] * env.window, (audible[-1] + 1) * env.window
    return Music(
        round(start, 3),
        round(end, 3),
        tuple(
            (round(g.lo, 3), round(g.hi, 3))
            for g in found
            if start < g.lo and g.hi < end
        ),
    )


@dataclass(frozen=True, slots=True)
class Placed:
    start: float
    end: float
    delta: float
    verdict: str


@dataclass(frozen=True, slots=True)
class Alignment:
    """`fits` is false when there were fewer stretches of music than songs."""

    spans: tuple[Placed, ...]
    fits: bool


def _judged(start: float, end: float, delta: float, *, grounded: bool = True) -> Placed:
    agrees = grounded and abs(delta) <= LENGTH_TOLERANCE
    return Placed(start, end, delta, AGREES if agrees else DOUBT)


def _partition(pieces: Sequence[Span], lengths: Sequence[float]) -> tuple[Placed, ...]:
    """Group pieces into songs in list order: fewest doubts, then smallest error.

    A group's measure is its extent or its sum of music, whichever is nearer the
    list: whether a list counts a silent passage inside a song varies by source.
    """
    played = [0.0, *accumulate(hi - lo for lo, hi in pieces)]

    def off_by(first: int, last: int, length: float) -> float:
        extent = pieces[last][1] - pieces[first][0]
        music = played[last + 1] - played[first]
        return min(extent - length, music - length, key=abs)

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
                delta = abs(off_by(before, used - 1, length))
                cost = (doubts + (delta > LENGTH_TOLERANCE), total + delta)
                if used not in row or cost < row[used]:
                    row[used], back[used] = cost, before
        best.append(row)
        came_from.append(back)

    spans: list[Placed] = []
    used = count
    for done in range(songs, 0, -1):
        before = came_from[done][used]
        first, last = pieces[before], pieces[used - 1]
        delta = off_by(before, used - 1, lengths[done - 1])
        spans.append(_judged(first[0], last[1], delta))
        used = before
    return tuple(reversed(spans))


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
        return Alignment(_partition(pieces, lengths), True)
    return Alignment(_walk(ordered, start, end, lengths), False)


@dataclass(frozen=True, slots=True)
class Tape:
    id: str
    name: str
    created: float = 0.0
    source: str = ""
    duration: float = 0.0
    music_start: float | None = None
    music_end: float | None = None
    songs: tuple[Song, ...] = ()
    gaps: tuple[Span, ...] = ()
    inside: tuple[tuple[int, float, float], ...] = ()
    mismatch: str = ""

    @property
    def has_ear(self) -> bool:
        return any(s.set_by == EAR for s in self.songs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "source": self.source,
            "duration": self.duration,
            "music_start": self.music_start,
            "music_end": self.music_end,
            "songs": [s.to_dict() for s in self.songs],
            "gaps": [list(g) for g in self.gaps],
            "inside": [{"song": n, "lo": lo, "hi": hi} for n, lo, hi in self.inside],
            "mismatch": self.mismatch,
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
            songs=tuple(Song.from_dict(s) for s in data["songs"]),
            gaps=tuple((float(lo), float(hi)) for lo, hi in data["gaps"]),
            inside=tuple((i["song"], i["lo"], i["hi"]) for i in data["inside"]),
            mismatch=str(data["mismatch"]),
        )


def named(tape_key: str, name: str, now: float) -> Tape:
    cleaned = _clean(name)
    if not cleaned:
        raise BadList("a tape needs a name")
    return Tape(id=tape_key, name=cleaned, created=now)


def relist(tape: Tape, songs: tuple[Song, ...]) -> Tape:
    """Take a new list. What was found for the old one no longer means anything."""
    if tape.has_ear:
        raise EarSet(
            f"{tape.name} has boundaries set by ear, and a new list would lose them"
        )
    return replace(
        tape,
        songs=songs,
        gaps=(),
        inside=(),
        mismatch="",
        music_start=None,
        music_end=None,
    )


def _margin(start: float, stop: float, gaps: Iterable[Span]) -> Span:
    """Pull a stretch's edges out of any silence they were set inside."""
    for lo, hi in gaps:
        if lo <= start < hi:
            start = hi
        if lo < stop <= hi:
            stop = lo
    return start, stop


def _placed_song(song: Song, span: Placed) -> Song:
    return replace(
        song,
        start=round(span.start, 3),
        end=round(span.end, 3),
        delta=round(span.delta, 3),
        verdict=span.verdict,
    )


def _ear_song(song: Song) -> Song:
    measured = (song.end or 0.0) - (song.start or 0.0)
    return replace(song, delta=round(measured - song.length, 3), verdict=AGREES)


def _mismatch(songs: Sequence[Song], fits: bool, stretches: int) -> str:
    if not fits:
        return (
            f"{len(songs)} songs on the list and only {stretches} stretches of music "
            "- boundaries with no gap are placed from list lengths and need your ear"
        )
    doubts = sum(s.verdict == DOUBT for s in songs)
    if doubts * 2 > len(songs):
        return (
            f"{doubts} of {len(songs)} songs disagree with their list lengths - "
            "check the list and its order; nothing has been reordered"
        )
    return ""


def place(tape: Tape, music: Music) -> Tape:
    """Set every boundary not set by ear; those split the list and stay put."""
    out = list(tape.songs)
    fits = True
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
        limit = (out[stop].start or music.end) if stop < len(out) else music.end
        lo, hi = _margin(cursor, limit, music.gaps)
        within = [g for g in music.gaps if lo < g[0] and g[1] < hi]
        found = align(within, lo, hi, [s.length for s in out[index:stop]])
        fits = fits and found.fits
        for at, span in enumerate(found.spans, index):
            out[at] = _placed_song(out[at], span)
        index = stop

    inside = tuple(
        (s.number, lo, hi)
        for lo, hi in music.gaps
        for s in out
        if s.start is not None and s.end is not None and s.start < lo and hi < s.end
    )
    return replace(
        tape,
        songs=tuple(out),
        music_start=music.start,
        music_end=music.end,
        gaps=music.gaps,
        inside=inside,
        mismatch=_mismatch(out, fits, len(music.gaps) + 1),
    )
