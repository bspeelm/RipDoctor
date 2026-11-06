"""Which release a song on a tape belongs to, and how sure that is. ADR-063.

Folding is for comparison only: what a list says is stored as the list said it.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Any

WITHIN = 3.0
CLOSE_RATIO = 0.8
# Options a person is offered for one song: more than this is reading, not choosing.
OFFERED = 12
OFFICIAL = "official"
VARIOUS = "various artists"

MATCHED, REVIEW, NONE, FAILED, OWNED = "matched", "review", "none", "failed", "owned"
CATALOGUE, PERSON = "catalogue", "person"

EXACT, CLOSE, NOTHING = 0, 1, 2
NAMED, EDITION, ELSEWHERE = 0, 1, 2

_VARIANTS = str.maketrans(
    {
        **dict.fromkeys("\u2019\u2018\u02bc`\u00b4", "'"),
        **dict.fromkeys("\u201c\u201d", '"'),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    }
)
_GUEST = r"(?:feat\.|ft\.|featuring\b)"
_CREDIT = re.compile(
    rf"\s*[(\[]\s*{_GUEST}\s*((?:(?!\s-\s)[^)\]])*)[)\]]"
    rf"|\s+-\s*{_GUEST}\s*([^()\[\]]*)$",
    re.I,
)
_LISTED_GUESTS = re.compile(rf"\s+{_GUEST}\s.*$", re.I)
_REMASTER_WORDS = (
    r"(?=[^()\[\]]*remaster)(?:(?:remaster(?:ed|s)?|digital(?:ly)?|version|\d{4})\s*)+"
)
_REMASTER = re.compile(
    rf"(?:\s+-\s*{_REMASTER_WORDS}|\s*\(\s*{_REMASTER_WORDS}\)"
    rf"|\s*\[\s*{_REMASTER_WORDS}\])\s*$",
    re.I,
)
_EDITION_WORDS = (
    r"(?=[^()\[\]]*(?:deluxe|expanded|remaster|anniversary|bonus|special|limited"
    r"|collector))"
    r"(?:(?:the|deluxe|expanded|remaster(?:ed|s)?|anniversary|bonus|tracks?|special"
    r"|limited|collector'?s|edition|version|digital|\d{4}|\d+(?:st|nd|rd|th))\s*)+"
)
_EDITION = re.compile(
    rf"(?:(?:\s+-|:)\s*{_EDITION_WORDS}|\s*\(\s*{_EDITION_WORDS}\)"
    rf"|\s*\[\s*{_EDITION_WORDS}\])\s*$",
    re.I,
)
_LIVE = re.compile(r"\blive\b", re.I)
_LEAD = re.compile(r"\s*[,;&]\s*")
_NAMES = re.compile(rf"\s*(?:[,;&]|\band\b|\b{_GUEST})\s*", re.I)
_ANY_NAMES = re.compile(r"\s*(?:[,;&/()]|\band\b|\bfeat\b\.?|\bft\b\.?)\s*")
_PROSE_LIST = re.compile(r"&|\band\b", re.I)
# A net over free text, so it can miss a version and say nothing of one it catches
# beyond the word: "unless the list says so" is the whole of its judgement.
_VERSION = re.compile(
    r"\b(live|acoustic|instrumental|demo|remix|edit|karaoke|session|alternate|clean"
    r"|atmos|spatial|a cappella|acapella|sped|slowed|rehearsal|unplugged|stripped"
    r"|extended|dub|mix|video|tribute)(?:ed|e?s)?\b",
    re.I,
)
_THE = re.compile(r"^the\s+")
_SUFFIX = re.compile(r"\s*(?:\(.*\)|\[.*\]|\s-\s.*)\s*$")
# Catalogued as one of these, a release is still that primary kind of record.
_ONLY_A_FLAVOUR = frozenset(("Remix", "Mixtape/Street"))
_PREFERRED_KINDS = ("album", "ep", "single")
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
    recording_title: str = ""
    disambiguation: str = ""
    credit: tuple[str, ...] = ()  # each name, as credited and as the artist is known
    tracks: int = 0  # on its disc


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
    primary: str = ""
    secondary: tuple[str, ...] = ()
    country: str = ""
    format: str = ""
    disambiguation: str = ""


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
    country: str = ""
    format: str = ""
    kind: str = ""
    primary: str = ""  # the kind of record it is, where `kind` says what it is sold as
    tracks: int = 0


@dataclass(frozen=True, slots=True)
class Held:
    """An album row the library has, as many rows as `tracks`, numbered as `numbers`."""

    release: str
    album: str
    album_artist: str
    tracks: int
    numbers: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Copy:
    """The library's copy of a song."""

    album_artist: str
    album: str
    path: str
    release: str


@dataclass(frozen=True, slots=True)
class Proposal:
    """`choice` is None when the list's own album stands. `held` is what the library
    has of the albums in question and `copy` its copy of this song, as of the lookup."""

    status: str
    reason: str = ""
    choice: Choice | None = None
    candidates: tuple[Choice, ...] = ()
    note: str = ""
    held: tuple[Held, ...] = ()
    copy: Copy | None = None
    library_note: str = ""

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
            note=data.get("note", ""),
            held=tuple(
                Held(**{**h, "numbers": tuple(h.get("numbers", ()))})
                for h in data.get("held", ())
            ),
            copy=Copy(**data["copy"]) if data.get("copy") else None,
            library_note=data.get("library_note", ""),
        )


def fold(text: str) -> str:
    wide = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(unicodedata.normalize("NFKC", wide).translate(_VARIANTS).split())


def _exact(text: str) -> str:
    """What acceptance compares: canonical form, case and typographic variants only.

    Compatibility folding would make a superscript or a sharp s the same letter as
    something else, and a title that only agrees that way is a person's to judge.
    """
    plain = unicodedata.normalize("NFC", text.translate(_VARIANTS)).lower()
    return " ".join(plain.split())


def _bare(text: str) -> str:
    """Without a guest clause or a remaster suffix; a title that is only one stays."""
    return _REMASTER.sub("", _CREDIT.sub("", text)).strip() or text


def _guests(text: str) -> set[str]:
    return {(a or b).strip() for a, b in _CREDIT.findall(text)}


def _loose(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if c.isalnum())


def search_artists(artist: str) -> tuple[str, ...]:
    """Who to ask for: the credit without its guests, then its first name alone.

    The whole credit comes first because a comma can be part of one name.
    """
    credit = _LISTED_GUESTS.sub("", _CREDIT.sub("", artist.translate(_VARIANTS)))
    credit = credit.strip()
    lead = _LEAD.split(credit)[0]
    return (credit,) if lead == credit else (credit, lead)


def search_album(album: str) -> str:
    return _bare(album.translate(_VARIANTS))


def search_title(title: str) -> str:
    return _bare(title.translate(_VARIANTS))


def base_title(title: str) -> str:
    """Without what a list may add and a catalogue not have, like (Single Edit). Empty
    for a title that is only that."""
    return _SUFFIX.sub("", title).strip()


def _lead(listed: str) -> str:
    """The credit before any guests and, in a comma list, its first name.

    An ampersand makes the commas part of one name, as in a band called
    "Sun, Moon & Stars", whose first word is not who is credited.
    """
    credit = _LISTED_GUESTS.sub("", _CREDIT.sub("", listed)).strip()
    return credit if _PROSE_LIST.search(credit) else credit.split(",")[0].strip()


def release_id(text: str) -> str:
    """The id inside whatever was pasted, a catalogue link included."""
    found = _RELEASE_ID.search(text)
    if not found:
        raise ValueError(f"{text.strip()!r} holds no release id")
    return found.group(0).lower()


def _album_base(title: str) -> str:
    """Without its edition suffixes, however many it carries."""
    while (shorter := _EDITION.sub("", title)) != title:
        title = shorter
    return title


def album_agreement(listed: str, title: str) -> int:
    """NAMED, EDITION when only an edition suffix differs, or ELSEWHERE."""
    ours, theirs = _exact(listed), _exact(title)
    if not ours or not theirs:
        return ELSEWHERE
    if ours == theirs:
        return NAMED
    base = _album_base(ours)
    return EDITION if base and base == _album_base(theirs) else ELSEWHERE


def _grade(wanted: str, found: str) -> int:
    ours, theirs = _exact(wanted), _exact(found)
    a, b = _bare(ours), _bare(theirs)
    guests = _guests(ours), _guests(theirs)
    same_guests = guests[0] == guests[1] or not all(guests)
    same_live = bool(_LIVE.search(ours)) == bool(_LIVE.search(theirs))
    if a == b and same_guests and same_live:
        return EXACT
    p, q = _loose(fold(a)), _loose(fold(b))
    if not p or not q:
        return NOTHING
    begins = min(len(p), len(q)) >= 3 and (p.startswith(q) or q.startswith(p))
    close = begins or SequenceMatcher(None, p, q).ratio() >= CLOSE_RATIO
    return CLOSE if close else NOTHING


def _off(track: CatalogueTrack, length: float) -> float:
    return math.inf if track.length is None else abs(track.length - length)


def names(credit: str, *, loosely: bool = False) -> set[str]:
    """The names in a credit. `loosely`, for folded text, also splits on what a
    library's credit may use where a list's does not."""
    return {name for name in (_ANY_NAMES if loosely else _NAMES).split(credit) if name}


def _name(name: str) -> frozenset[str]:
    return frozenset(_THE.sub("", n) for n in names(name))


def lead_credited(listed: str, credit: Iterable[str]) -> bool:
    """The list's lead is on the credit, whoever else is, a leading The aside.

    A name is compared whole: a shared word is no agreement, and Various Artists names
    nobody. A list crediting several names outright needs all of them.
    """
    said = {_name(_exact(c)) for c in credit if _exact(c) != VARIOUS}
    ours = _name(_lead(_exact(listed)))
    return bool(ours) and (ours in said or all(frozenset((n,)) in said for n in ours))


def kind_of(primary: str, secondary: Sequence[str]) -> str:
    return "compilation" if "Compilation" in secondary else primary.lower()


def _choice(edition: Edition, track: CatalogueTrack | None) -> Choice:
    base = Choice(
        release=edition.release,
        group=edition.group,
        release_title=edition.title,
        album_artist=edition.artist,
        date=edition.date,
        status=edition.status,
        note=edition.note,
        country=edition.country,
        format=edition.format,
        kind=kind_of(edition.primary, edition.secondary),
        primary=edition.primary.lower(),
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
        tracks=track.tracks,
    )


def _title_grade(wanted: str, track: CatalogueTrack) -> int:
    """Both names of the track must say it: a recording called one thing can sit on a
    track called another, and the difference is often the version."""
    grade = _grade(wanted, track.title)
    return max(grade, _grade(wanted, track.recording_title or track.title))


def _track(wanted: Wanted, edition: Edition) -> tuple[int, CatalogueTrack | None]:
    graded = [(_title_grade(wanted.title, t), t) for t in edition.tracks]
    if not graded:
        return NOTHING, None
    grade, track = min(graded, key=lambda g: (g[0], _off(g[1], wanted.length)))
    return (grade, track) if grade != NOTHING else (NOTHING, None)


def _version(wanted: Wanted, *texts: str) -> str:
    """A word in these that says another version, and the list's title does not."""
    title = fold(wanted.title)
    for text in texts:
        for word in _VERSION.finditer(text):
            if not re.search(rf"\b{re.escape(word.group(1).lower())}", title):
                return text
    return ""


@dataclass(frozen=True, slots=True)
class _Fit:
    choice: Choice
    grade: int
    off: float
    official: bool
    live: bool
    credited: bool
    credit: str
    album: int
    kinds: tuple[str, ...]
    repeats: int
    version: str
    other_release: str
    in_library: bool


def _fit(wanted: Wanted, edition: Edition, library: Iterable[str]) -> _Fit:
    grade, track = _track(wanted, edition)
    live = edition.live or bool(_LIVE.search(fold(edition.title)))
    album = album_agreement(wanted.album, edition.title)
    named = album == NAMED
    asked_live = named and _LIVE.search(fold(wanted.album))
    credit = (track.credit if track else ()) or (edition.artist,)
    # A person can still choose a track whose name did not agree, so it is offered.
    shown = track or min(
        edition.tracks, key=lambda t: _off(t, wanted.length), default=None
    )
    return _Fit(
        choice=_choice(edition, shown),
        grade=grade,
        off=math.inf if track is None else _off(track, wanted.length),
        official=fold(edition.status) == OFFICIAL,
        live=live and not asked_live,
        credited=lead_credited(wanted.artist, credit),
        credit=" & ".join(credit),
        album=album,
        kinds=()
        if named
        else tuple(k for k in edition.secondary if k not in _ONLY_A_FLAVOUR),
        repeats=sum(
            _grade(wanted.title, t.title) == EXACT and _off(t, wanted.length) <= WITHIN
            for t in edition.tracks
        ),
        version=_version(wanted, track.disambiguation) if track else "",
        other_release=""
        if named
        else _version(wanted, edition.title, edition.disambiguation),
        in_library=edition.release in library,
    )


def _rank(fit: _Fit) -> tuple[Any, ...]:
    """Among those fit to accept. Whatever is equal falls to the date and then the id,
    so a tie never stops for a person and always comes out the same. A list's length is
    whole seconds, so a smaller difference is not nearer."""
    kind = fit.choice.kind
    return (
        not fit.in_library,
        fit.album,
        _PREFERRED_KINDS.index(kind) if kind in _PREFERRED_KINDS else 9,
        fit.choice.date or "9999",
        fit.off if math.isinf(fit.off) else round(fit.off),
        fit.choice.release,
    )


def _nearness(fit: _Fit) -> tuple[Any, ...]:
    """Among those not fit to accept, the nearest first."""
    return (
        fit.grade,
        not fit.credited,
        not fit.official,
        fit.album,
        fit.off > WITHIN,
        fit.live or bool(fit.kinds) or bool(fit.version or fit.other_release),
        *_rank(fit)[2:],
    )


def _doubt(wanted: Wanted, fit: _Fit) -> str:
    """What stops this edition being accepted, in words a person can act on."""
    c = fit.choice
    if fit.grade == CLOSE:
        return (
            f"only a close title: the list has {wanted.title!r}, "
            f"{c.release_title} has {c.track_title!r}"
        )
    if fit.grade == NOTHING:
        return f"{c.release_title} has no track like this title"
    if not fit.official:
        return (
            f"no Official edition has this title: {c.release_title} "
            f"is {c.status or 'of no stated status'}"
        )
    if fit.live:
        return f"the title is only on a live album: {c.release_title}"
    if fit.kinds:
        kinds = " + ".join(fit.kinds)
        return (
            f"{c.release_title} is catalogued as {kinds}, which the list did not name"
        )
    if fit.version:
        return (
            f"the catalogue calls this recording {fit.version!r}, "
            "which the list's title does not say"
        )
    if fit.other_release:
        return (
            f"{c.release_title} reads as another version: {fit.other_release!r}, "
            "which the list does not say"
        )
    if not fit.credited:
        return f"the catalogue credits {fit.credit}, the list {wanted.artist}"
    if c.length is None:
        return f"{c.release_title} has this title with no length to check it against"
    if fit.off > WITHIN:
        return (
            f"the title matches, but the nearest length is {c.release_title}'s, "
            f"off by {fit.off:.0f} s"
        )
    if fit.repeats > 1:
        return (
            f"{c.release_title} has {fit.repeats} tracks called {c.track_title!r} "
            "of about this length - name the track"
        )
    return ""


def _offered(fits: Sequence[_Fit]) -> tuple[Choice, ...]:
    """One of each album and recording, best first, and a copy the library has however
    far down it comes. The best's own single stays offered past the cap: that is the
    way out of adding an album entry beside an edition already filed."""
    seen: set[tuple[str, str]] = set()
    kept: list[_Fit] = []
    for fit in fits:
        c = fit.choice
        key = (c.group or c.release, c.recording)
        if fit.in_library or key not in seen:
            kept.append(fit)
        seen.add(key)
    first = kept[0].choice
    single = next(
        (
            f.choice
            for f in kept[OFFERED:]
            if f.choice.kind == "single" and f.choice.recording == first.recording
        ),
        None,
    )
    offered = tuple(f.choice for f in kept[:OFFERED])
    return (*offered, single) if single else offered


def _relevant(held: Sequence[Held], offered: Sequence[Choice]) -> tuple[Held, ...]:
    titles = [c.release_title for c in offered]
    return tuple(
        h
        for h in held
        if any(album_agreement(h.album, title) != ELSEWHERE for title in titles)
    )


def propose(
    wanted: Wanted,
    editions: Sequence[Edition],
    *,
    held: Sequence[Held] = (),
    loose: bool = False,
) -> Proposal:
    """Accept what fits with nobody looking, or state the doubt and offer what there is.

    Accepted means, all of it: the title on both the recording and the track, nothing
    saying it is another version, a release of no kind the list did not ask for, the
    list's lead on the credit, a length within `WITHIN`, and Official. The album only
    chooses among those: an edition the library has, then the one the list names, then
    the earliest album, EP and single. `loose` is a search on a title the list did not
    say, whose results are never accepted.
    """
    if not editions:
        named_album = wanted.album or "no album on the list"
        return Proposal(NONE, f"no release found for {wanted.artist}, {named_album}")

    library = {h.release for h in held}
    fits = [_fit(wanted, e, library) for e in editions]
    clean = sorted((f for f in fits if not _doubt(wanted, f)), key=_rank)
    doubted = sorted((f for f in fits if _doubt(wanted, f)), key=_nearness)
    ordered = [*clean, *doubted]
    offered = _offered(ordered)
    albums = _relevant(held, offered)
    if ordered[0].grade == NOTHING:
        return Proposal(
            REVIEW,
            f"{len(fits)} editions found, none with a track like this title - "
            "pick a release or file from your list",
            None,
            offered,
            held=albums,
        )
    if loose:
        reason = (
            f"nothing in the catalogue is called {wanted.title!r} by this artist - "
            "these have the title without what follows it, so pick one or file from "
            "your list"
        )
        return Proposal(REVIEW, reason, ordered[0].choice, offered, held=albums)
    if not clean:
        reason = _doubt(wanted, ordered[0])
        return Proposal(REVIEW, reason, ordered[0].choice, offered, held=albums)
    best = clean[0]
    note = ""
    if wanted.album and best.album == ELSEWHERE:
        note = f"the list has {wanted.album}; this is on {best.choice.release_title}"
    return Proposal(MATCHED, "", best.choice, offered, note, albums)


def _shown_kind(wanted: Wanted, c: Choice) -> str:
    """An album the catalogue files as a compilation too is the album a list named."""
    if c.kind != "compilation" or c.primary != "album":
        return c.kind
    return (
        "compilation"
        if album_agreement(wanted.album, c.release_title) == ELSEWHERE
        else "album"
    )


def options(wanted: Wanted, proposal: Proposal | None) -> list[dict[str, Any]]:
    """What a person can choose from, as the page shows it."""
    if proposal is None:
        return []
    library = {h.release for h in proposal.held}
    return [
        {
            **asdict(c),
            "kind": _shown_kind(wanted, c),
            "year": c.date[:4],
            "delta": None if c.length is None else round(c.length - wanted.length, 1),
            "in_library": c.release in library,
        }
        for c in proposal.candidates
    ]


def off_the_list(wanted: Wanted, choice: Choice) -> str:
    """Why a person's choice needs saying twice, or nothing."""
    if choice.length is None or abs(choice.length - wanted.length) <= WITHIN:
        return ""
    gap = choice.length - wanted.length
    return (
        f"{choice.track_title or wanted.title} on {choice.release_title} is "
        f"{abs(gap):.0f} s {'longer' if gap > 0 else 'shorter'} than the list says, so "
        "it may be another version - say so to choose it anyway"
    )


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
