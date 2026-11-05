"""One uncut recording and the ordered list of songs in it. ADR-063."""

from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from itertools import accumulate, pairwise
from typing import Any

from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import DB_MIN, Envelope
from ripdoctor.core.match import (
    CATALOGUE,
    FAILED,
    MATCHED,
    NONE,
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

# In bytes, and short of a filesystem's 255: a superseded cut wears a timestamp.
CUT_TITLE_BYTES = 200

COLUMNS = ("playlist", "owned", "position", "title", "artist", "album", "duration")
_DURATION = re.compile(r"(?:(\d+):)?(\d+):([0-5]\d)", re.ASCII)

Span = tuple[float, float]


class BadList(ValueError):
    """A song list that cannot be read, and what to change in it."""


class EarSet(ValueError):
    """The change would discard a boundary a person set."""


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
            "reason": self.reason,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "settled_by": self.settled_by,
            "heard": self.heard,
            "skip": self.skip,
            "cut": asdict(self.cut) if self.cut else None,
        }

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


def _clock(seconds: float) -> str:
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
        *(f"start at {_clock(t)}" for t in starts),
        *(f"end at {_clock(t)}" for t in ends),
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
            "found_start": self.found_start,
            "found_end": self.found_end,
            "songs": [s.to_dict() for s in self.songs],
            "gaps": [[g.lo, g.hi] for g in self.gaps],
            "quiet": [list(g.quiet) for g in self.gaps],
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
            found_start=data.get("found_start"),
            found_end=data.get("found_end"),
            songs=tuple(Song.from_dict(s) for s in data["songs"]),
            gaps=tuple(
                Gap(float(lo), float(hi), float(quiet_lo), float(quiet_hi))
                for (lo, hi), (quiet_lo, quiet_hi) in zip(
                    data["gaps"], data.get("quiet", data["gaps"]), strict=True
                )
            ),
            inside=tuple((i["song"], i["lo"], i["hi"]) for i in data["inside"]),
            mismatch=str(data["mismatch"]),
        )


def named(tape_key: str, name: str, now: float) -> Tape:
    cleaned = _clean(name)
    if not cleaned:
        raise BadList("a tape needs a name")
    return Tape(id=tape_key, name=cleaned, created=now)


def _same_song(song: Song) -> tuple[str, str, str, int]:
    return song.title, song.artist, song.album, round(song.length)


def _keeping_matches(tape: Tape, songs: tuple[Song, ...]) -> tuple[Song, ...]:
    """A song the new list leaves as it was keeps its match, wherever it moved."""
    new = {_same_song(s) for s in songs}
    lost = [
        s.title
        for s in tape.songs
        if s.settled_by == PERSON and _same_song(s) not in new
    ]
    if lost:
        raise ChoiceSet(
            f"you chose the release for {', '.join(lost)}, and this list changes or "
            "drops them - keep those songs as they were"
        )
    old = {_same_song(s): s for s in tape.songs}
    return tuple(
        replace(s, proposal=was.proposal, settled_by=was.settled_by, skip=was.skip)
        if (was := old.get(_same_song(s)))
        else s
        for s in songs
    )


def relist(tape: Tape, songs: tuple[Song, ...]) -> Tape:
    """Take a new list. What was found for the old one no longer means anything."""
    if tape.has_ear:
        raise EarSet(
            f"{tape.name} has boundaries set by ear, and a new list would lose them"
        )
    return replace(
        tape,
        songs=_keeping_matches(tape, songs),
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
    where = ", ".join(f"{_clock(lo)} to {_clock(hi)}" for lo, hi in silences)
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


def _mismatch(
    songs: Sequence[Song],
    fits: bool,
    stretches: int,
    inside: Sequence[tuple[int, float, float]],
    groupings: int,
) -> str:
    said: list[str] = []
    silent = {s.number: 0.0 for s in songs}
    for number, lo, hi in inside:
        silent[number] += hi - lo
    misfits = sum(
        s.verdict == DOUBT
        and min(abs(off := s.delta or 0.0), abs(off - silent[s.number]))
        > LENGTH_TOLERANCE
        for s in songs
    )
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


def place(tape: Tape, music: Music) -> Tape:
    """Set every boundary not set by ear; those split the list and stay put."""
    out = list(tape.songs)
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
        for at, span in enumerate(found.spans, index):
            out[at] = _placed_song(out[at], span, quiet)
        index = stop

    inside = tuple(
        (s.number, lo, hi)
        for lo, hi in quiet
        for s in out
        if s.start is not None and s.end is not None and s.start < lo and hi < s.end
    )
    return replace(
        tape,
        songs=tuple(out),
        music_start=music.start,
        music_end=music.end,
        found_start=music.found_start,
        found_end=music.found_end,
        gaps=music.gaps,
        inside=inside,
        mismatch=_mismatch(out, fits, len(music.gaps) + 1, inside, groupings),
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


def with_proposals(tape: Tape, found: dict[int, Proposal]) -> Tape:
    """The catalogue's answers. A person's choice is not the catalogue's to change."""

    def answered(song: Song) -> Song:
        made = found.get(song.number)
        if made is None or song.settled_by == PERSON:
            return song
        by = CATALOGUE if made.status == MATCHED else ""
        return replace(song, proposal=made, settled_by=by)

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
            proposal=replace(was, status=status, reason="", choice=choice),
            settled_by=PERSON,
        )

    return replace(tape, songs=tuple(chosen(s) for s in tape.songs))


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


def set_edges(tape: Tape, number: int, start: float, end: float) -> Tape:
    """A person's boundary, which every other song not set by ear is laid around.

    Ear-set songs keep list order against each other, near or far, because `place`
    relies on them to split the list.
    """
    music = music_of(tape)
    _song_at(tape, number)
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
    placed = _changed(
        tape,
        number,
        start=round(start, 3),
        end=round(end, 3),
        set_by=EAR,
        heard=False,
    )
    return place(placed, music)


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


def doubtful(tape: Tape, song: Song) -> bool:
    """Waiting for an ear: its own doubt, or any song on a tape whose list and audio
    disagree, where a length that happens to fit proves nothing."""
    if song.skip or song.heard or song.set_by == EAR:
        return False
    return song.verdict == DOUBT or bool(tape.mismatch)


def ear_order(tape: Tape) -> tuple[Song, ...]:
    """Doubts first, then the rest of what a mismatch flags, the skipped last."""

    def rank(song: Song) -> tuple[int, int]:
        if song.skip:
            return 3, song.number
        if doubtful(tape, song):
            return (0 if song.verdict == DOUBT else 1), song.number
        return 2, song.number

    return tuple(sorted(tape.songs, key=rank))


def edge_at(song: Song, edge: str) -> float | None:
    return song.start if edge == START else song.end


def flagged(tape: Tape) -> tuple[tuple[Song, str], ...]:
    """The boundaries worth a clip without being asked for."""
    return tuple(
        (s, edge)
        for s in ear_order(tape)
        if doubtful(tape, s) and s.start is not None
        for edge in (START, END)
    )


def clip_name(at: float) -> str:
    """By the instant alone: the audio under a tape never changes, so a clip for a
    boundary that moved is another file and never a stale one."""
    return f"{round(at * 1000):010d}.flac"


def cut_name(song: Song, count: int) -> str:
    title = safe_filename(song.title).encode()[:CUT_TITLE_BYTES]
    return f"{file_number(song.number, count)} {title.decode(errors='ignore')}.flac"


def label(song: Song) -> str:
    return f"song {song.number} ({song.title})"


def cut_refusal(tape: Tape) -> str:
    """Why this tape may not be cut yet, naming every song, or nothing."""
    active = [s for s in tape.songs if not s.skip]
    if not active:
        return "every song is marked skip, so there is nothing to cut"
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
            "still in doubt - listen, then set it, mark it heard or skip it",
            [s for s in active if doubtful(tape, s)],
        ),
        (
            "no settled release - match the songs and settle each one",
            [s for s in active if not s.settled_by],
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
    return bool(statuses) and all(status == CUT for status in statuses)


def with_cut(tape: Tape, number: int, record: CutRecord) -> Tape:
    return _changed(tape, number, cut=record)
