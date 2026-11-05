"""Catalogue lookup, so a spec can be built rather than typed.

Network access goes through a Fetcher for the same reason external programs go
through a Runner: everything above it is then testable with no network at all.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ripdoctor import __version__
from ripdoctor.core.match import OFFICIAL, CatalogueTrack, Edition

BASE = "https://musicbrainz.org/ws/2"

# A real, identifying User-Agent is required rather than polite. Requests
# without one are throttled hard, and the throttling looks like the service
# being down.
USER_AGENT = f"ripdoctor/{__version__} (https://github.com/bspeelm/RipDoctor)"

VINYL = ("vinyl", '12" vinyl', '10" vinyl', '7" vinyl')

# How many candidates are asked for a tracklist. Each is its own request to a
# service that allows one a second.
PROBE = 4
# A search costs one request however many it answers, and the earliest edition
# of a record is often not among the first few.
WIDE = 25

RETRY_STATUS = (429, 500, 502, 503, 504)
RETRIES = 4
BACKOFF = 1.5


class LookupFailed(Exception):
    """The catalogue could not be reached, or said nothing useful."""


class Fetcher(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> bytes: ...


@dataclass
class HttpFetcher:
    timeout: float = 15.0

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https
        with urllib.request.urlopen(request, timeout=self.timeout) as r:  # noqa: S310
            data: bytes = r.read()
            return data


@dataclass
class Pace:
    """One request a second, shared by everything holding this pace."""

    gap: float = 1.0
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    last: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self) -> None:
        with self.lock:
            if self.last is not None:
                left = self.last + self.gap - self.now()
                if left > 0:
                    self.sleep(left)
            self.last = self.now()


@dataclass
class Paced:
    """A Fetcher that keeps a pace, for runs of many requests."""

    inner: Fetcher
    pace: Pace

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.pace.wait()
        return self.inner.get(url, headers)


@dataclass
class Track:
    number: int
    title: str
    length: float | None  # seconds, or nothing at all
    disc: int = 1
    position: int = 0  # on its disc, where `number` runs through the release
    recording: str = ""


@dataclass
class Release:
    """One candidate pressing."""

    mbid: str
    title: str
    artist: str
    date: str
    format: str
    tracks: list[Track]
    # MusicBrainz keeps pressing qualifiers here rather than in the title.
    # Without it a search for one well-known record returns fifty rows that all
    # read the same.
    disambiguation: str = ""
    country: str = ""
    status: str = ""
    group: str = ""
    live: bool = False
    credit: str = ""  # everyone on the credit, where `artist` is the first
    primary: str = ""
    secondary: tuple[str, ...] = ()

    @property
    def has_durations(self) -> bool:
        """Whether this entry is usable for fitting.

        Vinyl entries are frequently bulk-created clones of a digital tracklist
        with no lengths at all. Fitting against zeros produces garbage, so an
        entry without durations is worse than a CD entry that has them.
        """
        return bool(self.tracks) and all(t.length for t in self.tracks)

    @property
    def total_seconds(self) -> float:
        return sum(t.length or 0.0 for t in self.tracks)

    def describe(self) -> str:
        length = (
            f"{self.total_seconds / 60:.0f} min"
            if self.has_durations
            else "NO DURATIONS"
        )
        return (
            f"{self.artist} - {self.title} ({self.date or 'no date'}) "
            f"[{self.format or 'unknown'}] {length}"
        )


def _request(
    fetcher: Fetcher,
    url: str,
    *,
    retries: int = RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch and decode, retrying what is worth retrying.

    The service answers 503 under load, and without a retry a first pass simply
    dies on a busy minute. A 404 is not retried - it will not become a 200.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last = ""
    for attempt in range(retries):
        try:
            raw = fetcher.get(url, headers)
        except urllib.error.HTTPError as e:
            # An HTTPError holds the response body open; closing it releases
            # the connection rather than leaving it to the collector.
            code = e.code
            e.close()
            if code not in RETRY_STATUS:
                raise LookupFailed(f"catalogue returned {code}") from e
            last = f"HTTP {code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
        else:
            try:
                parsed: dict[str, Any] = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as e:
                raise LookupFailed(
                    "catalogue returned something that is not JSON"
                ) from e
            return parsed
        if attempt < retries - 1:
            sleep(BACKOFF * (2**attempt))
    raise LookupFailed(f"catalogue unreachable after {retries} attempts: {last}")


def _tracks(media: list[dict[str, Any]]) -> list[Track]:
    out: list[Track] = []
    for medium in media:
        for t in medium.get("tracks", []):
            ms = t.get("length")
            out.append(
                Track(
                    number=len(out) + 1,
                    title=str(t.get("title", "")),
                    length=(ms / 1000.0)
                    if isinstance(ms, int | float) and ms
                    else None,
                    disc=int(medium.get("position") or 1),
                    position=int(t.get("position") or 0),
                    recording=str((t.get("recording") or {}).get("id", "")),
                )
            )
    return out


def _credit(data: dict[str, Any]) -> str:
    return "".join(
        str(c.get("name", "")) + str(c.get("joinphrase", ""))
        for c in data.get("artist-credit") or []
    )


def _phrase(value: str) -> str:
    """One quoted term, with the query language's own syntax escaped.

    A record called Back in "Black" otherwise closes the phrase early and the
    rest of the title becomes syntax. The search then returns something, which
    is worse than returning nothing.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def release_group(fetcher: Fetcher, mbid: str, **kw: Any) -> str:
    """The group a pressing belongs to, which is where its cover usually is.

    Derived rather than stored: a kept copy is a second thing to hold in step.
    """
    quoted = urllib.parse.quote(mbid, safe="")
    data = _request(
        fetcher, f"{BASE}/release/{quoted}?inc=release-groups&fmt=json", **kw
    )
    group = data.get("release-group") or {}
    return str(group.get("id", ""))


def search(
    fetcher: Fetcher, artist: str, album: str, limit: int = 10, **kw: Any
) -> list[Release]:
    """Candidate releases, most useful first."""
    terms = f"artist:{_phrase(artist)} AND release:{_phrase(album)}"
    query = urllib.parse.quote(terms, safe="")
    data = _request(
        fetcher, f"{BASE}/release?query={query}&limit={limit}&fmt=json", **kw
    )
    out = []
    for r in data.get("releases", []):
        credit = r.get("artist-credit") or [{}]
        group = r.get("release-group") or {}
        out.append(
            Release(
                mbid=str(r.get("id", "")),
                title=str(r.get("title", "")),
                artist=str(credit[0].get("name", artist)),
                date=str(r.get("date", "")),
                format=_format_of(r),
                tracks=[],
                disambiguation=str(r.get("disambiguation", "")),
                country=str(r.get("country", "")),
                status=str(r.get("status", "")),
                group=str(group.get("id", "")),
                live="Live" in (group.get("secondary-types") or []),
                credit=_credit(r),
                primary=str(group.get("primary-type") or ""),
                secondary=tuple(str(t) for t in group.get("secondary-types") or ()),
            )
        )
    return out


def _format_of(release: dict[str, Any]) -> str:
    media = release.get("media") or []
    return str(media[0].get("format", "")) if media else ""


def likely(releases: list[Release], keep: int = PROBE) -> list[Release]:
    """The few worth asking about, from what a search already answered.

    A search answers with the format and the date. Only a second request per
    release says whether it has durations, and that is what the ranking needs,
    so asking about all ten took eleven requests against a service that allows
    one a second - and the page said `searching...` for the whole of it.

    The cheap facts choose who gets asked. A dated vinyl pressing of a record
    somebody is ripping from vinyl is the likely answer; the rest are not worth
    a second each to find out.
    """
    ordered = sorted(
        releases,
        key=lambda r: (r.format.lower() not in VINYL, not r.date),
    )
    return ordered[:keep]


def fetch_tracks(fetcher: Fetcher, release: Release, **kw: Any) -> Release:
    """Fill in a release from the catalogue, keeping anything already known.

    Everything, not only the tracklist: a first pass has nothing but an id, and
    a release that came back unnamed named the record it was fitting. ADR-042.
    """
    # artist-credits too: without it the answer carries no artist at all.
    data = _request(
        fetcher,
        f"{BASE}/release/{release.mbid}?inc=recordings+artist-credits&fmt=json",
        **kw,
    )
    release.tracks = _tracks(data.get("media") or [])
    if not release.format:
        release.format = _format_of(data)
    if not release.title:
        release.title = str(data.get("title", ""))
    if not release.date:
        release.date = str(data.get("date", ""))
    if not release.artist:
        credit = data.get("artist-credit") or [{}]
        release.artist = str(credit[0].get("name", ""))
    release.status = release.status or str(data.get("status", ""))
    release.credit = release.credit or _credit(data)
    return release


def release_by_id(fetcher: Fetcher, mbid: str, **kw: Any) -> Release:
    """A release somebody named. A search would have answered with its group."""
    release = fetch_tracks(fetcher, Release(mbid, "", "", "", "", []), **kw)
    release.group = release_group(fetcher, mbid, **kw)
    return release


def _worth_probing(found: list[Release]) -> list[Release]:
    """Official editions if there are any, the earliest of each group first, so
    that two records with one name are both seen before any second pressing."""
    official = [r for r in found if r.status.lower() == OFFICIAL] or found
    firsts: list[Release] = []
    rest: list[Release] = []
    groups: set[str] = set()
    for release in sorted(official, key=lambda r: (r.date or "9999", r.mbid)):
        group = release.group or release.mbid
        (rest if group in groups else firsts).append(release)
        groups.add(group)
    return (firsts + rest)[:PROBE]


def editions(
    fetcher: Fetcher, artists: Sequence[str], album: str, **kw: Any
) -> list[Release]:
    """The editions of an album with their tracks, from the first of `artists`
    the catalogue knows it under. Raises LookupFailed, which is not "none"."""
    if not album:
        return []
    for artist in artists:
        found = search(fetcher, artist, album, limit=WIDE, **kw)
        if found:
            return [fetch_tracks(fetcher, r, **kw) for r in _worth_probing(found)]
    return []


def edition_of(release: Release) -> Edition:
    return Edition(
        release=release.mbid,
        group=release.group,
        title=release.title,
        artist=release.credit or release.artist,
        date=release.date,
        status=release.status,
        tracks=tuple(
            CatalogueTrack(
                t.title, t.position or t.number, t.disc, t.length, t.recording
            )
            for t in release.tracks
        ),
        live=release.live,
        note=" ".join(
            filter(None, (release.format, release.country, release.disambiguation))
        ),
        primary=release.primary,
        secondary=release.secondary,
    )


def rank(releases: list[Release]) -> list[Release]:
    """Usable entries first, whatever their format.

    Ranking vinyl first without checking is what steered one first pass into a
    dead end: the vinyl entry for that record had no durations at all, and the
    CD entry that did was pushed below it.
    """
    return sorted(
        releases,
        key=lambda r: (
            not r.has_durations,
            r.format.lower() not in VINYL,
            -len(r.tracks),
        ),
    )


def report(releases: list[Release]) -> str:
    lines = []
    for i, r in enumerate(releases, 1):
        mark = " " if r.has_durations else "!"
        lines.append(f"  {mark}{i:>2}. {r.describe()}")
        lines.append(f"       {r.mbid}")
    if any(not r.has_durations for r in releases):
        lines.append("")
        lines.append(
            "  ! no track durations - fitting against those produces nonsense, "
            "so pick one that has them even if its format is wrong"
        )
    return "\n".join(lines)
