"""Catalogue lookup, with no network involved.

Two of these correspond to things that went wrong against the real service and
cost a first pass: an entry with no durations at all, and a 503 on a busy
minute.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field

import pytest

from ripdoctor.integrations import musicbrainz as MB


@dataclass
class FakeFetcher:
    """Answers from a script. Records what was asked and with what headers."""

    replies: list[bytes | Exception] = field(default_factory=list)
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append((url, headers))
        reply = self.replies.pop(0) if self.replies else b"{}"
        if isinstance(reply, Exception):
            raise reply
        return reply


def http(code: int) -> urllib.error.HTTPError:
    # A real file object: HTTPError wraps None in a temporary file that
    # complains when it is collected, which surfaces as an unrelated error.
    return urllib.error.HTTPError("u", code, "why", {}, io.BytesIO(b""))  # type: ignore[arg-type]


SEARCH = json.dumps(
    {
        "releases": [
            {
                "id": "aaa",
                "title": "Hell on Church Street",
                "date": "2022",
                "artist-credit": [{"name": "Punch Brothers"}],
                "media": [{"format": "Vinyl"}],
            },
            {
                "id": "bbb",
                "title": "Hell on Church Street",
                "date": "2022",
                "artist-credit": [{"name": "Punch Brothers"}],
                "media": [{"format": "CD"}],
            },
        ]
    }
).encode()


def release_json(lengths: list[int | None]) -> bytes:
    return json.dumps(
        {
            "media": [
                {
                    "format": "Vinyl",
                    "tracks": [
                        {"title": f"Track {i + 1}", "length": ms}
                        for i, ms in enumerate(lengths)
                    ],
                }
            ]
        }
    ).encode()


def no_sleep(_seconds: float) -> None:
    return None


# --------------------------------------------------------------- requests


def test_an_identifying_user_agent_is_sent() -> None:
    """Requests without one are throttled hard, and throttling looks like the
    service being down."""
    fetcher = FakeFetcher([SEARCH])
    MB.search(fetcher, "Punch Brothers", "Hell", sleep=no_sleep)
    _url, headers = fetcher.calls[0]
    assert "ripdoctor" in headers["User-Agent"]
    assert "http" in headers["User-Agent"], "no way to be contacted about abuse"


def test_the_query_is_escaped() -> None:
    fetcher = FakeFetcher([SEARCH])
    MB.search(fetcher, "AC/DC", "Back in Black", sleep=no_sleep)
    url = fetcher.calls[0][0]
    assert " " not in url and '"' not in url
    assert "AC%2FDC" in url, "a slash was left to mean a path separator"


def test_a_title_containing_a_quote_does_not_break_the_query() -> None:
    """Otherwise the phrase closes early and the rest becomes syntax.

    The search then returns something, which is worse than returning nothing.
    """
    fetcher = FakeFetcher([SEARCH])
    MB.search(fetcher, "Artist", 'Back in "Black"', sleep=no_sleep)
    decoded = urllib.parse.unquote(fetcher.calls[0][0])
    assert 'release:"Back in \\"Black\\""' in decoded


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_a_busy_service_is_retried(code: int) -> None:
    """It answers 503 under load. Without a retry a first pass dies on a busy
    minute."""
    fetcher = FakeFetcher([http(code), http(code), SEARCH])
    found = MB.search(fetcher, "a", "b", sleep=no_sleep)
    assert len(found) == 2
    assert len(fetcher.calls) == 3


def test_backoff_grows_between_attempts() -> None:
    waits: list[float] = []
    fetcher = FakeFetcher([http(503), http(503), SEARCH])
    MB.search(fetcher, "a", "b", sleep=waits.append)
    assert waits == sorted(waits) and waits[1] > waits[0], "it retried at a fixed rate"


def test_a_not_found_is_not_retried() -> None:
    """It will not become a 200, and retrying wastes the service's patience."""
    fetcher = FakeFetcher([http(404)])
    with pytest.raises(MB.LookupFailed, match="404"):
        MB.search(fetcher, "a", "b", sleep=no_sleep)
    assert len(fetcher.calls) == 1


def test_a_service_that_never_answers_gives_up_and_says_so() -> None:
    fetcher = FakeFetcher([http(503)] * 10)
    with pytest.raises(MB.LookupFailed, match="unreachable after"):
        MB.search(fetcher, "a", "b", sleep=no_sleep)


def test_a_reply_that_is_not_json_is_reported_clearly() -> None:
    fetcher = FakeFetcher([b"<html>rate limited</html>"])
    with pytest.raises(MB.LookupFailed, match="not JSON"):
        MB.search(fetcher, "a", "b", sleep=no_sleep)


# ---------------------------------------------------------- the results


def test_releases_come_back_with_what_is_needed_to_choose() -> None:
    found = MB.search(FakeFetcher([SEARCH]), "Punch Brothers", "Hell", sleep=no_sleep)
    assert [r.format for r in found] == ["Vinyl", "CD"]
    assert found[0].mbid == "aaa" and found[0].artist == "Punch Brothers"


def test_track_lengths_are_read_in_seconds_across_every_medium() -> None:
    r = MB.Release("aaa", "t", "a", "2022", "Vinyl", [])
    MB.fetch_tracks(FakeFetcher([release_json([153320, 212160])]), r, sleep=no_sleep)
    assert [t.length for t in r.tracks] == [153.32, 212.16]
    assert [t.number for t in r.tracks] == [1, 2]


def test_an_entry_with_no_durations_is_recognised() -> None:
    """Vinyl entries are frequently bulk clones of a digital tracklist.

    Fitting against zeros produces garbage, so this has to be visible before a
    release is chosen rather than after.
    """
    r = MB.Release("aaa", "t", "a", "2022", "Vinyl", [])
    MB.fetch_tracks(FakeFetcher([release_json([None, None])]), r, sleep=no_sleep)
    assert not r.has_durations
    assert "NO DURATIONS" in r.describe()


def test_a_partly_timed_entry_is_still_unusable() -> None:
    r = MB.Release("aaa", "t", "a", "2022", "Vinyl", [])
    MB.fetch_tracks(FakeFetcher([release_json([153320, None])]), r, sleep=no_sleep)
    assert not r.has_durations, "one missing length is enough to ruin the arithmetic"


# ------------------------------------------------------------ the ranking


def timed(fmt: str, n: int = 6) -> MB.Release:
    return MB.Release(
        "x", "t", "a", "2022", fmt, [MB.Track(i + 1, "t", 200.0) for i in range(n)]
    )


def untimed(fmt: str, n: int = 6) -> MB.Release:
    return MB.Release(
        "y", "t", "a", "2022", fmt, [MB.Track(i + 1, "t", None) for i in range(n)]
    )


def test_a_usable_cd_outranks_an_unusable_vinyl() -> None:
    """The mistake that steered one first pass into a dead end.

    Ranking vinyl first without checking put an entry with no durations at the
    top, above a CD entry that had them.
    """
    ranked = MB.rank([untimed("Vinyl"), timed("CD")])
    assert ranked[0].format == "CD"


def test_vinyl_wins_when_both_are_usable() -> None:
    ranked = MB.rank([timed("CD"), timed("Vinyl")])
    assert ranked[0].format == "Vinyl", "the right pressing, when it is usable"


def test_the_fuller_tracklist_breaks_a_tie() -> None:
    ranked = MB.rank([timed("Vinyl", n=4), timed("Vinyl", n=11)])
    assert len(ranked[0].tracks) == 11


def test_the_report_marks_what_cannot_be_used_and_says_why() -> None:
    text = MB.report(MB.rank([timed("CD"), untimed("Vinyl")]))
    assert "NO DURATIONS" in text
    assert "produces nonsense" in text
    assert text.index("CD") < text.index("NO DURATIONS"), "usable entries first"


def test_a_report_of_only_usable_entries_carries_no_warning() -> None:
    assert "produces nonsense" not in MB.report([timed("Vinyl")])


def test_the_mbid_is_shown_so_a_choice_can_be_recorded() -> None:
    assert "x" in MB.report([timed("Vinyl")])


def test_the_group_a_pressing_belongs_to_is_asked_for() -> None:
    """Where a vinyl cover usually is. The archive holds a scan for the album
    far more often than for one twelve-inch edition of it."""
    fetcher = FakeFetcher(
        replies=[b'{"release-group": {"id": "rg-1", "title": "Album"}}']
    )
    assert MB.release_group(fetcher, "rel-1") == "rg-1"
    url = fetcher.calls[0][0]
    assert "release/rel-1" in url and "inc=release-groups" in url


def test_a_release_with_no_group_is_an_empty_answer() -> None:
    fetcher = FakeFetcher(replies=[b"{}"])
    assert MB.release_group(fetcher, "rel-1") == ""
