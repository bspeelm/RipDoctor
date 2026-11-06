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


def a_recording(
    title: str = "A Song",
    *,
    recording: str = "rec-1",
    length: int | None = 200000,
    track_title: str = "",
    track_length: int | None = 200000,
    offset: int = 3,
    number: str = "4",
    disc: int = 2,
    release: dict[str, object] | None = None,
    **kw: object,
) -> dict[str, object]:
    """A recording search's answer for one recording on one release, in its shape."""
    track = {"id": "t", "number": number, "title": track_title or title}
    if track_length:
        track["length"] = track_length  # type: ignore[assignment]
    media = {
        "position": disc,
        "format": "CD",
        "track": [track],
        "track-count": 13,
        "track-offset": offset,
    }
    body: dict[str, object] = {
        "id": recording,
        "title": title,
        "artist-credit": [{"name": "A Band", "artist": {"name": "A Band"}}],
        "releases": [{**(release or entry("a")), "media": [media]}],
        **kw,
    }
    if length:
        body["length"] = length
    return body


@dataclass
class Searched:
    """Answers each search with the next reply, and counts them."""

    replies: list[object] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append(urllib.parse.unquote(url))
        reply = self.replies.pop(0) if self.replies else []
        if isinstance(reply, bytes):
            return reply
        assert isinstance(reply, list)
        count = len(reply)
        return json.dumps({"count": count, "recordings": reply}).encode()


A_SONG = Wanted("A Song", "A Band", "A Record", 200.0)


def test_a_search_is_for_the_song_and_the_artist_as_quoted_phrases() -> None:
    fetcher = Searched([[a_recording()]])
    MB.find_song(fetcher, Wanted('A "Song"', "A Band", "", 200.0))
    assert 'recording:"A \\"Song\\"" AND artist:"A Band"' in fetcher.calls[0]
    assert "limit=100" in fetcher.calls[0]
    assert len(fetcher.calls) == 1


def test_a_found_track_is_placed_by_its_offset_since_its_number_can_be_a_side() -> None:
    found = MB.find_song(Searched([[a_recording(number="A6")]]), A_SONG)
    track = found.editions[0].tracks[0]
    assert (track.number, track.disc, track.tracks) == (4, 2, 13)


def test_a_track_with_no_length_falls_back_on_its_recording() -> None:
    found = MB.find_song(Searched([[a_recording(track_length=None)]]), A_SONG)
    assert found.editions[0].tracks[0].length == 200.0


def test_a_recording_with_no_length_anywhere_has_none() -> None:
    reply = [a_recording(track_length=None, length=None)]
    assert MB.find_song(Searched([reply]), A_SONG).editions[0].tracks[0].length is None


def test_a_found_edition_says_what_the_rule_and_the_page_need() -> None:
    group = {"id": "g9", "primary-type": "Album", "secondary-types": ["Compilation"]}
    release = entry(
        "a",
        country="GB",
        disambiguation="tour edition",
        **{
            "artist-credit": [{"name": "A Band", "joinphrase": " & "}, {"name": "B"}],
            "release-group": group,
        },
    )
    reply = [a_recording(release=release, disambiguation="demo")]
    got = MB.find_song(Searched([reply]), A_SONG).editions[0]
    assert (got.release, got.group, got.status) == ("a", "g9", "Official")
    assert (got.primary, got.secondary) == ("Album", ("Compilation",))
    assert (got.country, got.format) == ("GB", "CD")
    assert got.artist == "A Band & B"
    assert got.note == "CD GB tour edition"
    assert got.tracks[0].disambiguation == "demo"
    assert got.tracks[0].recording_title == "A Song"


def test_a_credit_gives_each_name_as_credited_and_as_the_artist_is_known() -> None:
    credit = [
        {"name": "A. Band", "joinphrase": " & ", "artist": {"name": "A Band"}},
        {"name": "A Guest", "artist": {"name": "A Guest"}},
    ]
    reply = [a_recording(**{"artist-credit": credit})]
    track = MB.find_song(Searched([reply]), A_SONG).editions[0].tracks[0]
    assert track.credit == ("A. Band", "A Band", "A Guest")


def test_two_recordings_on_one_release_are_one_edition_holding_both() -> None:
    reply = [
        a_recording(recording="one", offset=0),
        a_recording(recording="two", offset=9),
    ]
    editions = MB.find_song(Searched([reply]), A_SONG).editions
    assert len(editions) == 1
    assert [t.recording for t in editions[0].tracks] == ["one", "two"]


def test_a_comma_credit_with_nothing_found_is_retried_by_its_lead() -> None:
    fetcher = Searched([[], [a_recording()]])
    wanted = Wanted("A Song", "A Band, A Guest", "A Record", 200.0)
    assert MB.find_song(fetcher, wanted).editions
    assert 'artist:"A Band, A Guest"' in fetcher.calls[0]
    assert 'artist:"A Band"' in fetcher.calls[1]
    assert 'A Guest"' not in fetcher.calls[1]


def test_more_than_a_page_without_the_lists_album_asks_for_that_album() -> None:
    other = a_recording(release=entry("o", title="Hits"))
    named = a_recording(recording="rec-2", release=entry("n"))
    page = json.dumps({"count": 101, "recordings": [other]}).encode()
    fetcher = Searched([page, [named]])
    found = MB.find_song(fetcher, A_SONG)
    assert 'release:"A Record"' in fetcher.calls[1]
    assert [e.release for e in found.editions] == ["o", "n"]


def test_more_than_a_page_with_the_lists_album_asks_no_more() -> None:
    page = json.dumps({"count": 101, "recordings": [a_recording()]}).encode()
    fetcher = Searched([page])
    MB.find_song(fetcher, A_SONG)
    assert len(fetcher.calls) == 1


def test_a_title_with_a_list_only_suffix_is_searched_short_and_marked_loose() -> None:
    fetcher = Searched([[], [a_recording()]])
    found = MB.find_song(fetcher, Wanted("A Song (Single Edit)", "A Band", "", 200.0))
    assert found.loose
    assert 'recording:"A Song" AND' in fetcher.calls[1]


def test_nothing_found_for_a_title_with_no_suffix_asks_no_more() -> None:
    fetcher = Searched([[]])
    assert MB.find_song(fetcher, A_SONG) == MB.Found([])
    assert len(fetcher.calls) == 1


def test_a_song_never_costs_more_than_three_requests() -> None:
    fetcher = Searched([[], [], []])
    MB.find_song(fetcher, Wanted("A Song (Edit)", "A Band, A Guest", "An LP", 200.0))
    assert len(fetcher.calls) == 3


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"error": "busy"}',
        b"[]",
        b"null",
        b'{"recordings": 7}',
        b'{"recordings": [1]}',
        b'{"recordings": [{"releases": [1]}]}',
    ],
)
def test_a_search_answer_of_the_wrong_shape_is_a_failure_and_not_no_release(
    body: bytes,
) -> None:
    with pytest.raises(MB.LookupFailed):
        MB.find_song(Searched([body]), A_SONG)


def test_a_found_edition_meets_the_rule() -> None:
    found = MB.find_song(Searched([[a_recording()]]), A_SONG)
    made = propose(A_SONG, found.editions)
    assert made.status == "matched"
    assert made.choice is not None
    assert (made.choice.release, made.choice.group) == ("a", "g1")
    assert made.choice.recording == "rec-1"


def test_a_track_carries_its_disc_position_and_recording() -> None:
    release = MB.fetch_tracks(Scripted(), MB.Release("a", "", "", "", "", []))
    track = release.tracks[0]
    assert (track.number, track.disc, track.position) == (1, 2, 4)
    assert track.recording == "rec-1"


def test_an_edition_numbers_a_track_by_its_place_on_the_disc() -> None:
    release = MB.fetch_tracks(Scripted(), MB.Release("a", "", "", "", "", []))
    track = MB.edition_of(release).tracks[0]
    assert (track.number, track.disc, track.recording) == (4, 2, "rec-1")


@pytest.mark.parametrize(
    "body",
    [b"[]", b"null", b'{"media": 7}', b'{"media": [{"tracks": [1]}]}'],
)
def test_a_tracklist_of_the_wrong_shape_is_a_failure(body: bytes) -> None:
    with pytest.raises(MB.LookupFailed):
        MB.release_by_id(Scripted(lookup=body), "a")


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
        MB.find_song(Down(), A_SONG, sleep=lambda _s: None)
