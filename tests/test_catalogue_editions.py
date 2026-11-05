"""The catalogue's side of matching a tape, through the Fetcher seam. ADR-063."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field

import pytest

from ripdoctor.core.match import Wanted, propose
from ripdoctor.integrations import musicbrainz as MB


@dataclass
class Clock:
    at: float = 100.0
    slept: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += seconds


def entry(mbid: str, **kw: object) -> dict[str, object]:
    return {
        "id": mbid,
        "title": "A Record",
        "date": "1990",
        "status": "Official",
        "artist-credit": [{"name": "A Band"}],
        "release-group": {"id": "g1"},
        **kw,
    }


@dataclass
class Scripted:
    """Answers a search with `found` and any lookup with one timed track."""

    found: list[dict[str, object]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    lookup: bytes | None = None

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append(url)
        if "query=" in url:
            return json.dumps({"releases": self.found}).encode()
        if self.lookup is not None:
            return self.lookup
        track = {
            "title": "A Song",
            "length": 200000,
            "position": 4,
            "recording": {"id": "rec-1"},
        }
        media = [{"position": 2, "format": "CD", "tracks": [track]}]
        return json.dumps({"status": "Official", "media": media}).encode()


def fetched(calls: list[str]) -> list[str]:
    return [c.split("/release/")[1].split("?")[0] for c in calls if "/release/" in c]


def test_every_request_on_a_pace_waits_out_the_one_before() -> None:
    clock = Clock()
    paced = MB.Paced(Scripted(), MB.Pace(now=clock.now, sleep=clock.sleep))
    paced.get("u", {})
    assert clock.slept == []
    clock.at += 0.25
    paced.get("u", {})
    assert clock.slept == [0.75]


def test_time_already_passed_is_not_slept_again() -> None:
    clock = Clock()
    paced = MB.Paced(Scripted(), MB.Pace(now=clock.now, sleep=clock.sleep))
    paced.get("u", {})
    clock.at += 5.0
    paced.get("u", {})
    assert clock.slept == []


def test_one_pace_holds_across_two_fetchers() -> None:
    clock = Clock()
    pace = MB.Pace(now=clock.now, sleep=clock.sleep)
    MB.Paced(Scripted(), pace).get("u", {})
    MB.Paced(Scripted(), pace).get("u", {})
    assert clock.slept == [1.0]


def test_a_search_says_what_the_rule_needs_to_know_about_an_edition() -> None:
    found = entry(
        "a",
        **{
            "artist-credit": [
                {"name": "A Band", "joinphrase": " & "},
                {"name": "Another Band"},
            ],
            "release-group": {"id": "g9", "secondary-types": ["Live"]},
        },
    )
    got = MB.search(Scripted([found]), "A Band", "A Record")[0]
    assert (got.status, got.group, got.live) == ("Official", "g9", True)
    assert got.credit == "A Band & Another Band"
    assert got.artist == "A Band"


def test_a_search_keeps_the_groups_primary_and_secondary_types() -> None:
    group = {
        "id": "g9",
        "primary-type": "Album",
        "secondary-types": ["Compilation", "Remix"],
    }
    got = MB.search(Scripted([entry("a", **{"release-group": group})]), "A", "B")[0]
    assert (got.primary, got.secondary) == ("Album", ("Compilation", "Remix"))
    made = MB.edition_of(got)
    assert (made.primary, made.secondary) == ("Album", ("Compilation", "Remix"))
    assert not got.live


def test_a_compilation_from_the_catalogue_is_not_accepted_on_a_like_title() -> None:
    group = {"id": "g9", "primary-type": "Album", "secondary-types": ["Compilation"]}
    found = entry("a", title="A Record (Deluxe)", **{"release-group": group})
    got = MB.editions(Scripted([found]), ["A Band"], "A Record")
    made = propose(
        Wanted("A Song", "A Band", "A Record", 200.0), [MB.edition_of(r) for r in got]
    )
    assert made.status == "review"
    assert "Compilation" in made.reason


def test_a_track_carries_its_disc_position_and_recording() -> None:
    release = MB.fetch_tracks(Scripted(), MB.Release("a", "", "", "", "", []))
    track = release.tracks[0]
    assert (track.number, track.disc, track.position) == (1, 2, 4)
    assert track.recording == "rec-1"


def test_an_edition_numbers_a_track_by_its_place_on_the_disc() -> None:
    release = MB.fetch_tracks(Scripted(), MB.Release("a", "", "", "", "", []))
    track = MB.edition_of(release).tracks[0]
    assert (track.number, track.disc, track.recording) == (4, 2, "rec-1")


def test_a_fully_read_edition_meets_the_rule() -> None:
    fetcher = Scripted([entry("a")])
    got = MB.editions(fetcher, ["A Band"], "A Record")
    made = propose(
        Wanted("A Song", "A Band", "A Record", 200.0), [MB.edition_of(r) for r in got]
    )
    assert made.status == "matched"
    assert made.choice is not None
    assert (made.choice.release, made.choice.group) == ("a", "g1")
    assert made.choice.recording == "rec-1"


def test_a_blank_album_asks_nothing() -> None:
    fetcher = Scripted()
    assert MB.editions(fetcher, ["A Band"], "") == []
    assert fetcher.calls == []


def test_the_lead_artist_is_asked_only_when_the_whole_credit_finds_nothing() -> None:
    @dataclass
    class OnlyTheLead(Scripted):
        def get(self, url: str, headers: dict[str, str]) -> bytes:
            asked = urllib.parse.unquote(url)
            self.found = [] if "Another" in asked else [entry("a")]
            return super().get(url, headers)

    fetcher = OnlyTheLead()
    got = MB.editions(fetcher, ["A Band, Another Band", "A Band"], "A Record")
    assert [r.mbid for r in got] == ["a"]
    assert len([c for c in fetcher.calls if "query=" in c]) == 2


def test_the_whole_credit_is_enough_when_the_catalogue_knows_it() -> None:
    fetcher = Scripted([entry("a")])
    MB.editions(fetcher, ["A Band, Another Band", "A Band"], "A Record")
    assert len([c for c in fetcher.calls if "query=" in c]) == 1


def test_only_a_few_editions_are_fetched_however_many_a_search_returns() -> None:
    fetcher = Scripted([entry(f"e{i}", date=f"19{i + 10}") for i in range(9)])
    got = MB.editions(fetcher, ["A Band"], "A Record")
    assert fetched(fetcher.calls) == ["e0", "e1", "e2", "e3"]
    assert len(got) == MB.PROBE


def test_the_earliest_of_every_group_is_fetched_before_any_second_pressing() -> None:
    found = [
        entry("a1", date="1980"),
        entry("a2", date="1981"),
        entry("a3", date="1982"),
        entry("a4", date="1983"),
        entry("b1", date="2005", **{"release-group": {"id": "g2"}}),
    ]
    fetcher = Scripted(found)
    MB.editions(fetcher, ["A Band"], "A Record")
    assert fetched(fetcher.calls)[:2] == ["a1", "b1"]


def test_unofficial_editions_cost_nothing_when_an_official_one_exists() -> None:
    fetcher = Scripted([entry("boot", status="Bootleg", date="1970"), entry("real")])
    MB.editions(fetcher, ["A Band"], "A Record")
    assert fetched(fetcher.calls) == ["real"]


def test_unofficial_editions_are_still_looked_at_when_nothing_else_exists() -> None:
    fetcher = Scripted([entry("boot", status="Bootleg")])
    assert [r.mbid for r in MB.editions(fetcher, ["A Band"], "A Record")] == ["boot"]


def in_series(*names: str) -> list[dict[str, object]]:
    return [
        entry(
            f"e{at}", title=name, date=f"19{at + 10}", **{"release-group": {"id": name}}
        )
        for at, name in enumerate(names)
    ]


def test_a_series_does_not_crowd_out_the_album_asked_for() -> None:
    found = in_series(
        "Hits Vol. 1", "Hits Vol. 2", "Hits Vol. 3", "Hits Vol. 4", "Hits"
    )
    got = MB.editions(Scripted(found), ["A Band"], "Hits")
    assert got[0].title == "Hits"
    assert len(got) == MB.PROBE


def test_an_edition_of_the_album_is_read_before_an_earlier_unrelated_title() -> None:
    found = in_series("Hits Live", "Hits Vol. 2", "Hits Vol. 3", "Hits Vol. 4")
    found += in_series("Hits (Deluxe Edition)")
    found[-1]["id"] = "deluxe"
    fetcher = Scripted(found)
    MB.editions(fetcher, ["A Band"], "Hits")
    assert fetched(fetcher.calls)[0] == "deluxe"


def test_the_nearest_title_is_read_first_when_nothing_agrees() -> None:
    found = in_series("Unrelated", "Hits Vol. 2")
    fetcher = Scripted(found)
    MB.editions(fetcher, ["A Band"], "Hits")
    assert fetched(fetcher.calls) == ["e1", "e0"]


def same_named(count: int, artist: str = "A Band") -> list[dict[str, object]]:
    return [
        entry(
            f"e{at}",
            date=f"19{at + 10}",
            **{"release-group": {"id": f"g{at}"}, "artist-credit": [{"name": artist}]},
        )
        for at in range(count)
    ]


def test_every_album_of_the_exact_name_is_read_and_forces_review() -> None:
    fetcher = Scripted(same_named(6))
    got = MB.editions(fetcher, ["A Band"], "A Record")
    assert len(got) == 6
    assert len(fetcher.calls) == 7
    made = propose(
        Wanted("A Song", "A Band", "A Record", 200.0), [MB.edition_of(r) for r in got]
    )
    assert made.status == "review"
    assert "fits 6 albums" in made.reason


def test_albums_of_that_name_by_others_cost_no_extra_requests() -> None:
    fetcher = Scripted(same_named(30, artist="A Band of Others"))
    MB.editions(fetcher, ["A Band"], "A Record")
    assert len(fetcher.calls) == 1 + MB.PROBE


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"error": "busy"}',
        b"[]",
        b"null",
        b'{"releases": 7}',
        b'{"releases": [1]}',
    ],
)
def test_a_search_answer_of_the_wrong_shape_is_a_failure_and_not_no_release(
    body: bytes,
) -> None:
    class Answers(Scripted):
        def get(self, url: str, headers: dict[str, str]) -> bytes:
            return body

    with pytest.raises(MB.LookupFailed):
        MB.editions(Answers(), ["A Band"], "A Record")


def test_a_search_that_really_finds_nothing_is_no_release() -> None:
    assert MB.editions(Scripted([]), ["A Band"], "A Record") == []


@pytest.mark.parametrize(
    "body",
    [b"[]", b"null", b'{"media": 7}', b'{"media": [{"tracks": [1]}]}'],
)
def test_a_tracklist_of_the_wrong_shape_is_a_failure(body: bytes) -> None:
    with pytest.raises(MB.LookupFailed):
        MB.editions(Scripted([entry("a")], lookup=body), ["A Band"], "A Record")


def test_a_release_named_by_id_comes_back_with_its_group() -> None:
    @dataclass
    class WithGroup(Scripted):
        def get(self, url: str, headers: dict[str, str]) -> bytes:
            self.calls.append(url)
            return json.dumps(
                {
                    "title": "A Record",
                    "status": "Official",
                    "artist-credit": [{"name": "A Band"}],
                    "release-group": {"id": "g7"},
                    "media": [],
                }
            ).encode()

    got = MB.release_by_id(WithGroup(), "a")
    assert (got.title, got.group, got.status) == ("A Record", "g7", "Official")


def test_a_lookup_that_fails_is_raised_and_not_an_empty_answer() -> None:
    @dataclass
    class Down(Scripted):
        def get(self, url: str, headers: dict[str, str]) -> bytes:
            raise TimeoutError("no answer")

    with pytest.raises(MB.LookupFailed):
        MB.editions(Down(), ["A Band"], "A Record", sleep=lambda _s: None)
