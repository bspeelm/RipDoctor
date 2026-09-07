"""Catalogue lookup, so a spec can be built rather than typed.

Network access goes through a Fetcher for the same reason external programs go
through a Runner: everything above it is then testable with no network at all.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ripdoctor import __version__

BASE = "https://musicbrainz.org/ws/2"

# A real, identifying User-Agent is required rather than polite. Requests
# without one are throttled hard, and the throttling looks like the service
# being down.
USER_AGENT = f"ripdoctor/{__version__} (https://github.com/bspeelm/RipDoctor)"

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
class Track:
    number: int
    title: str
    length: float | None  # seconds, or nothing at all


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
                )
            )
    return out


def _phrase(value: str) -> str:
    """One quoted term, with the query language's own syntax escaped.

    A record called Back in "Black" otherwise closes the phrase early and the
    rest of the title becomes syntax. The search then returns something, which
    is worse than returning nothing.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
            )
        )
    return out


def _format_of(release: dict[str, Any]) -> str:
    media = release.get("media") or []
    return str(media[0].get("format", "")) if media else ""


def fetch_tracks(fetcher: Fetcher, release: Release, **kw: Any) -> Release:
    data = _request(
        fetcher, f"{BASE}/release/{release.mbid}?inc=recordings&fmt=json", **kw
    )
    release.tracks = _tracks(data.get("media") or [])
    if not release.format:
        release.format = _format_of(data)
    return release


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
            r.format.lower() not in ("vinyl", '12" vinyl'),
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
