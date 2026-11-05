"""Which release a song on a tape belongs to, and how sure that is. ADR-063.

Folding is for comparison only: what a list says is stored as the list said it.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Any

WITHIN = 3.0
CLOSE_RATIO = 0.8
OFFICIAL = "official"

MATCHED, REVIEW, NONE, FAILED = "matched", "review", "none", "failed"
CATALOGUE, PERSON = "catalogue", "person"

EXACT, CLOSE, NOTHING = 0, 1, 2

_VARIANTS = str.maketrans(
    {
        **dict.fromkeys("\u2019\u2018\u02bc`\u00b4", "'"),
        **dict.fromkeys("\u201c\u201d", '"'),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    }
)
_CREDIT = re.compile(
    r"\s*[(\[]\s*(?:feat|ft|featuring|with)\b[^)\]]*[)\]]"
    r"|\s+(?:feat|ft|featuring)\b\.?\s.*$",
    re.I,
)
_REMASTER = re.compile(
    r"\s*(?:-\s*|[(\[])[^()\[\]-]*\bremaster(?:ed|s)?\b[^()\[\]-]*[)\]]?\s*$", re.I
)
_LIVE = re.compile(r"\blive\b", re.I)
_LEAD = re.compile(r"\s*[,;&]\s*")
_NAMES = re.compile(r"\s*(?:[,;&/]|\band\b|\bx\b)\s*", re.I)
_RELEASE_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


class NoTrack(ValueError):
    """A person chose a release without saying which of its tracks is the song."""


@dataclass(frozen=True, slots=True)
class Wanted:
    title: str
    artist: str
    album: str
    length: float


@dataclass(frozen=True, slots=True)
class CatalogueTrack:
    title: str
    number: int
    disc: int
    length: float | None
    recording: str = ""


@dataclass(frozen=True, slots=True)
class Edition:
    release: str
    group: str
    title: str
    artist: str
    date: str
    status: str
    tracks: tuple[CatalogueTrack, ...]
    live: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class Choice:
    """An edition and its track for this song; `number` is 0 if it has no such track."""

    release: str
    group: str
    release_title: str
    album_artist: str
    date: str
    status: str
    note: str = ""
    track_title: str = ""
    number: int = 0
    disc: int = 0
    length: float | None = None
    recording: str = ""


@dataclass(frozen=True, slots=True)
class Proposal:
    """`choice` is None when the list's own album stands."""

    status: str
    reason: str = ""
    choice: Choice | None = None
    candidates: tuple[Choice, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Proposal:
        choice = data["choice"]
        return cls(
            status=data["status"],
            reason=data["reason"],
            choice=Choice(**choice) if choice else None,
            candidates=tuple(Choice(**c) for c in data["candidates"]),
        )


def fold(text: str) -> str:
    wide = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(unicodedata.normalize("NFKC", wide).translate(_VARIANTS).split())


def _bare(text: str) -> str:
    return _REMASTER.sub("", _CREDIT.sub("", text)).strip()


def _loose(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if c.isalnum())


def search_artists(artist: str) -> tuple[str, ...]:
    """Who to ask for: the credit without its guests, then its first name alone.

    The whole credit comes first because a comma can be part of one name.
    """
    credit = _CREDIT.sub("", artist.translate(_VARIANTS)).strip()
    lead = _LEAD.split(credit)[0]
    return (credit,) if lead == credit else (credit, lead)


def search_album(album: str) -> str:
    return _bare(album.translate(_VARIANTS))


def lookup_key(wanted: Wanted) -> tuple[str, str]:
    """Songs with the same key are answered by the same requests."""
    return (
        fold(search_artists(wanted.artist)[-1]),
        fold(search_album(wanted.album)),
    )


def release_id(text: str) -> str:
    """The id inside whatever was pasted, a catalogue link included."""
    found = _RELEASE_ID.search(text)
    if not found:
        raise ValueError(f"{text.strip()!r} holds no release id")
    return found.group(0).lower()


def _grade(wanted: str, found: str) -> int:
    ours, theirs = fold(wanted), fold(found)
    a, b = _bare(ours), _bare(theirs)
    if a == b and bool(_LIVE.search(ours)) == bool(_LIVE.search(theirs)):
        return EXACT
    p, q = _loose(a), _loose(b)
    if not p or not q:
        return NOTHING
    begins = min(len(p), len(q)) >= 3 and (p.startswith(q) or q.startswith(p))
    close = begins or SequenceMatcher(None, p, q).ratio() >= CLOSE_RATIO
    return CLOSE if close else NOTHING


def _off(track: CatalogueTrack, length: float) -> float:
    return math.inf if track.length is None else abs(track.length - length)


def _agrees(listed: str, credited: str) -> bool:
    ours, theirs = fold(_CREDIT.sub("", listed)), fold(credited)
    if theirs == "various artists" or ours == theirs:
        return True
    return bool(set(_NAMES.split(ours)) & set(_NAMES.split(theirs)))


def _choice(edition: Edition, track: CatalogueTrack | None) -> Choice:
    base = Choice(
        release=edition.release,
        group=edition.group,
        release_title=edition.title,
        album_artist=edition.artist,
        date=edition.date,
        status=edition.status,
        note=edition.note,
    )
    if track is None:
        return base
    return replace(
        base,
        track_title=track.title,
        number=track.number,
        disc=track.disc,
        length=track.length,
        recording=track.recording,
    )


def _track(wanted: Wanted, edition: Edition) -> tuple[int, CatalogueTrack | None]:
    graded = [(_grade(wanted.title, t.title), t) for t in edition.tracks]
    if not graded:
        return NOTHING, None
    grade, track = min(graded, key=lambda g: (g[0], _off(g[1], wanted.length)))
    return (grade, track) if grade != NOTHING else (NOTHING, None)


@dataclass(frozen=True, slots=True)
class _Fit:
    choice: Choice
    grade: int
    off: float
    official: bool
    live: bool
    credited: bool


def _fit(wanted: Wanted, edition: Edition) -> _Fit:
    grade, track = _track(wanted, edition)
    live = edition.live or bool(_LIVE.search(fold(edition.title)))
    return _Fit(
        choice=_choice(edition, track),
        grade=grade,
        off=math.inf if track is None else _off(track, wanted.length),
        official=fold(edition.status) == OFFICIAL,
        live=live and not _LIVE.search(fold(wanted.album)),
        credited=_agrees(wanted.artist, edition.artist),
    )


def _rank(fit: _Fit) -> tuple[Any, ...]:
    """A list's length is whole seconds, so a smaller difference is not nearer."""
    off = fit.off if math.isinf(fit.off) else round(fit.off)
    return (
        fit.grade,
        not fit.official,
        fit.live,
        not fit.credited,
        off,
        fit.choice.date or "9999",
        fit.choice.release,
    )


def _doubt(wanted: Wanted, fit: _Fit) -> str:
    """What stops this edition being accepted, in words a person can act on."""
    c = fit.choice
    if fit.grade == CLOSE:
        return f"only a close title: {c.track_title!r} on {c.release_title}"
    if fit.grade == NOTHING:
        return f"{c.release_title} has no track like this title"
    if not fit.official:
        return (
            f"no Official edition has this title: {c.release_title} "
            f"is {c.status or 'of no stated status'}"
        )
    if fit.live:
        return f"the title is only on a live album: {c.release_title}"
    if not fit.credited:
        return f"the catalogue credits {c.album_artist}, the list {wanted.artist}"
    if c.length is None:
        return f"{c.release_title} has this title with no length to check it against"
    if fit.off > WITHIN:
        return (
            f"the title matches, but the nearest length is {c.release_title}'s, "
            f"off by {fit.off:.0f} s"
        )
    return ""


def propose(wanted: Wanted, editions: Sequence[Edition]) -> Proposal:
    """Accept an Official edition with the title and a length within `WITHIN`:
    nearest length, then earliest date. Everything else states its doubt."""
    if not editions:
        named_album = wanted.album or "no album on the list"
        return Proposal(NONE, f"no release found for {wanted.artist}, {named_album}")

    fits = sorted((_fit(wanted, e) for e in editions), key=_rank)
    candidates = tuple(f.choice for f in fits)
    if fits[0].grade == NOTHING:
        return Proposal(
            REVIEW,
            f"{len(fits)} editions found, none with a track like this title - "
            "pick a release or file from your list",
            None,
            candidates,
        )
    clean = [f for f in fits if not _doubt(wanted, f)]
    if not clean:
        return Proposal(REVIEW, _doubt(wanted, fits[0]), fits[0].choice, candidates)

    album = _bare(fold(wanted.album))
    named = [f for f in clean if _bare(fold(f.choice.release_title)) == album] or clean
    groups = {f.choice.group or f.choice.release: f.choice for f in reversed(named)}
    best = named[0].choice
    if len(groups) > 1:
        seen = ", ".join(
            f"{c.release_title} ({c.date or 'no date'})" for c in groups.values()
        )
        return Proposal(
            REVIEW, f"fits {len(groups)} albums equally: {seen}", best, candidates
        )
    return Proposal(MATCHED, "", best, candidates)


def track_on(wanted: Wanted, edition: Edition, position: int | None = None) -> Choice:
    """A person's release: the track they named, or else the one like the title.

    `position` counts through the whole release, across its discs.
    """
    if position is not None:
        if not 1 <= position <= len(edition.tracks):
            raise NoTrack(
                f"{edition.title} has {len(edition.tracks)} tracks, "
                f"so there is no track {position}"
            )
        return _choice(edition, edition.tracks[position - 1])
    _, track = _track(wanted, edition)
    if track is None:
        listing = "; ".join(f"{i} {t.title}" for i, t in enumerate(edition.tracks, 1))
        raise NoTrack(
            f"{edition.title} has no track like {wanted.title!r} - "
            f"give its track number: {listing}"
        )
    return _choice(edition, track)
