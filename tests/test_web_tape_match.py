"""Matching a tape over the wire, with no network. ADR-063."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ripdoctor.core import match as M
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Jobs
from tests.test_web_routes import a_service, get, post

PASTED = "12345678-90ab-cdef-1234-567890abcdef"


@dataclass
class Clock:
    at: float = 100.0

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


@dataclass
class Catalogue:
    """Albums by title, each a list of editions, answering what a Fetcher is asked."""

    albums: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    down: set[str] = field(default_factory=set)
    broken: set[str] = field(default_factory=set)
    malformed: dict[str, bytes] = field(default_factory=dict)
    times: list[float] = field(default_factory=list)
    clock: Clock | None = None

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append(url)
        if self.clock:
            self.times.append(self.clock.now())
        asked = urllib.parse.unquote(url)
        if any(name in asked for name in self.down):
            raise urllib.error.URLError("no route")
        if any(name in asked for name in self.broken):
            raise RuntimeError("the worker died")
        for name, body in self.malformed.items():
            if name in asked:
                return body
        if "query=" in url:
            hits = [
                e
                for title, found in self.albums.items()
                if f'release:"{title}"' in asked
                for e in found
            ]
            return json.dumps({"releases": hits}).encode()
        wanted = url.split("/release/")[1].split("?")[0]
        for found in self.albums.values():
            for e in found:
                if e["id"] == wanted:
                    return json.dumps(e).encode()
        return json.dumps(
            an_edition(wanted, tracks=[("A Song", 200), ("Another", 150)])
        ).encode()

    def searches(self) -> int:
        return sum("query=" in c for c in self.calls)


def an_edition(
    mbid: str,
    *,
    tracks: list[tuple[str, int]],
    title: str = "A Record",
    group: str = "g1",
    date: str = "1990",
) -> dict[str, object]:
    return {
        "id": mbid,
        "title": title,
        "date": date,
        "status": "Official",
        "artist-credit": [{"name": "A Band"}],
        "release-group": {"id": group},
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "title": name,
                        "length": seconds * 1000,
                        "position": at,
                        "recording": {"id": f"rec-{mbid}-{at}"},
                    }
                    for at, (name, seconds) in enumerate(tracks, 1)
                ],
            }
        ],
    }


A_RECORD = an_edition("rel-a", tracks=[("A Song", 200), ("B Song", 180)])
B_RECORD = an_edition("rel-b", title="B Record", tracks=[("C Song", 240)], group="g2")


def row(title: str, album: str = "A Record", length: str = "3:20") -> dict[str, str]:
    return {"title": title, "artist": "A Band", "album": album, "length": length}


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue({"A Record": [A_RECORD], "B Record": [B_RECORD]})


@pytest.fixture
def service(tmp_path: Path, catalogue: Catalogue) -> Service:
    made = a_service(tmp_path)
    clock = Clock()
    catalogue.clock = clock
    made.fetcher = catalogue
    made.pace = MB.Pace(now=clock.now, sleep=clock.sleep)
    return made


TWO_ALBUMS = [
    row("A Song"),
    row("B Song", length="3:00"),
    row("C Song", "B Record", "4:00"),
]


def a_tape(app, service: Service, rows: list[dict[str, str]] | None = None) -> str:  # type: ignore[no-untyped-def]
    tape = post(app, "/api/tapes", service, {"name": "Mix"}).json()["id"]
    sent = post(app, f"/api/tapes/{tape}/songs", service, {"songs": rows or TWO_ALBUMS})
    assert sent.status == 200
    return tape


def run(app, service: Service, tape: str, **body: object) -> dict:  # type: ignore[no-untyped-def,type-arg]
    assert post(app, f"/api/tapes/{tape}/match", service, body).status == 202
    return get(app, f"/api/tapes/{tape}", service).json()


def statuses(view: dict) -> list[str]:  # type: ignore[type-arg]
    return [s["proposal"]["status"] for s in view["songs"]]


def test_every_song_gets_a_proposal_and_the_tape_says_it_is_settled(service) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    view = run(app, service, a_tape(app, service))
    assert statuses(view) == [M.MATCHED] * 3
    assert view["all_settled"] is True
    assert {s["settled_by"] for s in view["songs"]} == {M.CATALOGUE}
    choice = view["songs"][1]["proposal"]["choice"]
    assert (choice["release"], choice["group"], choice["number"]) == ("rel-a", "g1", 2)
    assert choice["recording"] == "rec-rel-a-2"


def test_what_the_list_says_is_never_altered_by_a_match(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    catalogue.albums["A Record"] = [an_edition("rel-a", tracks=[("a song", 200)])]
    app = build(service)
    listed = "A Song - Remastered 2011"
    view = run(app, service, a_tape(app, service, [row(listed)]))
    assert view["songs"][0]["title"] == listed
    assert view["songs"][0]["proposal"]["choice"]["track_title"] == "a song"


def test_songs_from_one_album_share_one_search_and_one_fetch(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    run(
        app,
        service,
        a_tape(app, service, [row("A Song"), row("B Song", length="3:00")]),
    )
    assert catalogue.searches() == 1
    assert len(catalogue.calls) == 2


def test_two_albums_ask_twice_and_no_more(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    run(app, service, a_tape(app, service))
    assert catalogue.searches() == 2
    assert len(catalogue.calls) == 4


def test_a_whole_run_keeps_to_the_pace(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    run(app, service, a_tape(app, service))
    gaps = [b - a for a, b in zip(catalogue.times, catalogue.times[1:], strict=False)]
    assert len(gaps) == 3
    assert all(gap >= 1.0 for gap in gaps)


def test_a_failed_lookup_is_retryable_and_never_reads_as_no_match(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.down = {'release:"B Record"'}
    app = build(service)
    tape = a_tape(app, service)
    view = run(app, service, tape)
    assert statuses(view) == [M.MATCHED, M.MATCHED, M.FAILED]
    assert "look again" in view["songs"][2]["proposal"]["reason"]
    assert view["all_settled"] is False

    catalogue.down = set()
    catalogue.calls.clear()
    view = run(app, service, tape)
    assert statuses(view) == [M.MATCHED] * 3
    assert all('"A Record"' not in urllib.parse.unquote(c) for c in catalogue.calls)


@pytest.mark.parametrize(
    "body",
    [b"{}", b"[]", b"null", b'{"releases": 7}', b'{"releases": [1]}'],
)
def test_a_malformed_answer_fails_one_album_and_the_rest_carry_on(
    service, catalogue, body: bytes
) -> None:  # type: ignore[no-untyped-def]
    catalogue.malformed = {'release:"B Record"': body}
    app = build(service)
    tape = a_tape(app, service)
    view = run(app, service, tape)
    assert statuses(view) == [M.MATCHED, M.MATCHED, M.FAILED]
    assert view["job"]["error"] in ("", None)

    catalogue.malformed = {}
    assert statuses(run(app, service, tape)) == [M.MATCHED] * 3


def test_an_empty_list_of_releases_is_still_none(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    catalogue.malformed = {'release:"B Record"': b'{"releases": []}'}
    app = build(service)
    view = run(app, service, a_tape(app, service))
    assert statuses(view) == [M.MATCHED, M.MATCHED, M.NONE]


def test_the_album_asked_for_is_fetched_past_a_series(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    catalogue.albums["A Record"] = [
        an_edition(
            f"vol-{n}",
            title=f"A Record Vol. {n}",
            group=f"v{n}",
            date=f"199{n}",
            tracks=[("A Song", 200)],
        )
        for n in range(1, 5)
    ] + [an_edition("the-one", group="one", date="2005", tracks=[("A Song", 200)])]
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("A Song")]))
    assert statuses(view) == [M.MATCHED]
    assert view["songs"][0]["proposal"]["choice"]["release"] == "the-one"


def test_a_second_album_of_the_name_past_the_probe_is_seen_and_forces_review(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    without = [("Other", 100)]
    catalogue.albums["A Record"] = [
        an_edition("first", group="a", date="1991", tracks=[("A Song", 200)]),
        an_edition("b", group="b", date="1992", tracks=without),
        an_edition("c", group="c", date="1993", tracks=without),
        an_edition("d", group="d", date="1994", tracks=without),
        an_edition("late", group="e", date="2005", tracks=[("A Song", 200)]),
    ]
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("A Song")]))
    assert statuses(view) == [M.REVIEW]
    assert "fits 2 albums" in view["songs"][0]["proposal"]["reason"]


def test_a_name_holding_a_comma_is_not_answered_by_its_first_word(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    rows = [row("A Song"), {**row("B Song", length="3:00"), "artist": "A Band, B & C"}]
    view = run(app, service, a_tape(app, service, rows))
    assert catalogue.searches() == 2
    assert statuses(view) == [M.MATCHED, M.REVIEW]
    assert "credits" in view["songs"][1]["proposal"]["reason"]


def test_a_retry_waits_with_the_paces_sleep_and_not_the_real_one(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.down = {"query="}
    app = build(service)
    before = service.pace.now()
    run(app, service, a_tape(app, service))
    assert service.pace.now() - before >= MB.BACKOFF


def test_no_release_stands_on_the_lists_album_and_needs_a_person(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.albums = {}
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("A Song")]))
    assert statuses(view) == [M.NONE]
    assert view["songs"][0]["proposal"]["choice"] is None
    assert view["songs"][0]["album"] == "A Record"
    assert view["all_settled"] is False


def test_a_blank_album_is_answered_without_asking(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("A Song", album="")]))
    assert statuses(view) == [M.NONE]
    assert catalogue.calls == []


def test_a_guest_in_the_artist_is_not_searched_for(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    song = {**row("A Song (feat. A Guest)"), "artist": "A Band feat. A Guest"}
    view = run(app, service, a_tape(app, service, [song]))
    assert statuses(view) == [M.MATCHED]
    assert "Guest" not in urllib.parse.unquote(catalogue.calls[0])


def test_a_non_ascii_title_matches_through_the_whole_path(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    catalogue.albums["A Record"] = [an_edition("rel-a", tracks=[("Cafe\u0301", 200)])]
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("Caf\u00e9")]))
    assert statuses(view) == [M.MATCHED]
    assert view["songs"][0]["title"] == "Caf\u00e9"


def test_a_live_album_named_by_its_group_is_not_accepted(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    live = an_edition("rel-l", tracks=[("A Song", 200)])
    live["release-group"] = {"id": "g1", "secondary-types": ["Live"]}
    catalogue.albums["A Record"] = [live]
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("A Song - Remastered")]))
    assert statuses(view) == [M.REVIEW]
    assert "live album" in view["songs"][0]["proposal"]["reason"]


def test_two_release_groups_arrive_in_review_with_both_to_pick_from(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.albums["A Record"] = [
        an_edition("rel-a", tracks=[("A Song", 200)]),
        an_edition("rel-z", tracks=[("A Song", 200)], group="g9", date="2001"),
    ]
    app = build(service)
    view = run(app, service, a_tape(app, service, [row("A Song")]))
    assert statuses(view) == [M.REVIEW]
    offered = view["songs"][0]["proposal"]["candidates"]
    assert {c["release"] for c in offered} == {"rel-a", "rel-z"}


def doubtful(service: Service, catalogue: Catalogue) -> tuple:  # type: ignore[type-arg]
    catalogue.albums["A Record"] = [
        an_edition("rel-a", tracks=[("A Song", 200)]),
        an_edition("rel-z", tracks=[("A Song", 200)], group="g9", date="2001"),
    ]
    app = build(service)
    tape = a_tape(app, service, [row("A Song")])
    run(app, service, tape)
    catalogue.calls.clear()
    return app, tape


def test_choosing_a_candidate_settles_it_with_no_request(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    got = post(
        app, f"/api/tapes/{tape}/songs/1/release", service, {"release": "rel-z"}
    ).json()
    song = got["songs"][0]
    assert song["proposal"]["choice"]["release"] == "rel-z"
    assert (song["settled_by"], song["proposal"]["status"]) == (M.PERSON, M.MATCHED)
    assert got["all_settled"] is True
    assert catalogue.calls == []


def test_an_accepted_match_can_be_overridden(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    tape = a_tape(app, service, [row("A Song")])
    run(app, service, tape)
    got = post(app, f"/api/tapes/{tape}/songs/1/release", service, {"release": PASTED})
    assert got.status == 200
    song = got.json()["songs"][0]
    assert (song["settled_by"], song["proposal"]["choice"]["release"]) == (
        M.PERSON,
        PASTED,
    )


def test_a_pasted_link_is_read_for_its_id(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    link = f"https://catalogue.example/release/{PASTED}"
    got = post(app, f"/api/tapes/{tape}/songs/1/release", service, {"release": link})
    assert got.json()["songs"][0]["proposal"]["choice"]["release"] == PASTED
    assert f"/release/{PASTED}" in catalogue.calls[0]


def test_something_that_is_no_release_id_asks_nothing(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    got = post(app, f"/api/tapes/{tape}/songs/1/release", service, {"release": "../x"})
    assert got.status == 400
    assert catalogue.calls == []


def test_a_release_without_the_song_asks_for_the_track_and_changes_nothing(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    catalogue.albums["Elsewhere"] = [
        an_edition(PASTED, title="Elsewhere", tracks=[("Unrelated", 90)])
    ]
    path = f"/api/tapes/{tape}/songs/1/release"
    refused = post(app, path, service, {"release": PASTED})
    assert refused.status == 409
    assert "1 Unrelated" in refused.json()["error"]
    assert T.read(service.layout, tape).songs[0].settled_by == ""

    named = post(app, path, service, {"release": PASTED, "track": "1"})
    assert named.json()["songs"][0]["proposal"]["choice"]["track_title"] == "Unrelated"


def test_a_catalogue_that_is_down_is_reported_and_changes_nothing(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    catalogue.down = {PASTED}
    got = post(app, f"/api/tapes/{tape}/songs/1/release", service, {"release": PASTED})
    assert got.status == 503
    assert T.read(service.layout, tape).songs[0].settled_by == ""


def test_a_claim_on_the_tape_refuses_settling_a_song(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    with service.jobs.claim(T.key_of(tape), "a change"):
        chosen = post(
            app, f"/api/tapes/{tape}/songs/1/release", service, {"release": "rel-z"}
        )
        listed = post(app, f"/api/tapes/{tape}/songs/1/from-list", service)
        assert (chosen.status, listed.status) == (409, 409)
        assert post(app, f"/api/tapes/{tape}/match", service).status == 409
    assert T.read(service.layout, tape).songs[0].settled_by == ""
    assert catalogue.calls == []


def test_no_run_can_start_while_a_pasted_release_is_being_looked_up(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    """The lookup is the slow part of settling, so it is where a job would slip in
    and later overwrite the document with what it read beforehand."""
    app, tape = doubtful(service, catalogue)
    during: list[int] = []
    asked = catalogue.get

    def asking(url: str, headers: dict[str, str]) -> bytes:
        during.append(post(app, f"/api/tapes/{tape}/match", service).status)
        return asked(url, headers)

    service.fetcher = type("Probing", (), {"get": staticmethod(asking)})()
    got = post(app, f"/api/tapes/{tape}/songs/1/release", service, {"release": PASTED})
    assert got.status == 200
    assert during and set(during) == {409}
    assert T.read(service.layout, tape).songs[0].settled_by == M.PERSON


def test_filing_from_the_list_settles_a_song_with_no_release(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.albums = {}
    app = build(service)
    tape = a_tape(app, service, [row("A Song")])
    run(app, service, tape)
    got = post(app, f"/api/tapes/{tape}/songs/1/from-list", service).json()
    song = got["songs"][0]
    assert song["settled_by"] == M.PERSON
    assert song["proposal"]["choice"] is None
    assert got["all_settled"] is True


def test_a_song_that_is_not_on_the_tape_is_not_found(service) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    tape = a_tape(app, service)
    assert post(app, f"/api/tapes/{tape}/songs/9/from-list", service).status == 404
    huge = "9" * 5000
    assert post(app, f"/api/tapes/{tape}/songs/{huge}/from-list", service).status == 404


def test_a_persons_choice_survives_a_run_for_everything_unsettled(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    post(app, f"/api/tapes/{tape}/songs/1/release", service, {"release": "rel-z"})
    view = run(app, service, tape, again=True)
    assert view["songs"][0]["proposal"]["choice"]["release"] == "rel-z"
    assert view["songs"][0]["settled_by"] == M.PERSON
    assert catalogue.calls == []


def test_a_person_may_not_settle_a_song_while_a_run_holds_the_tape(service) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    tape = a_tape(app, service)
    service.jobs = Jobs(spawn=lambda _work: None, now=service.now)
    assert post(app, f"/api/tapes/{tape}/match", service).status == 202
    assert post(app, f"/api/tapes/{tape}/songs/1/from-list", service).status == 409
    assert post(app, f"/api/tapes/{tape}/match", service).status == 409


def test_an_interrupted_run_keeps_what_it_finished_and_resumes_after_it(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.broken = {'release:"B Record"'}
    app = build(service)
    tape = a_tape(app, service)
    assert post(app, f"/api/tapes/{tape}/match", service).status == 202
    job = get(app, f"/api/tapes/{tape}", service).json()["job"]
    assert "the worker died" in job["error"]
    saved = T.read(service.layout, tape)
    assert [s.proposal is not None for s in saved.songs] == [True, True, False]

    catalogue.broken = set()
    catalogue.calls.clear()
    view = run(app, service, tape)
    assert statuses(view) == [M.MATCHED] * 3
    assert all('"A Record"' not in urllib.parse.unquote(c) for c in catalogue.calls)


def test_a_first_run_after_a_stated_doubt_does_not_ask_again(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    run(app, service, tape)
    assert catalogue.calls == []


def test_looking_again_asks_about_a_stated_doubt(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    catalogue.albums["A Record"].pop()
    view = run(app, service, tape, again=True)
    assert catalogue.calls
    assert statuses(view) == [M.MATCHED]


def test_a_tape_with_no_songs_cannot_be_matched(service) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    tape = post(app, "/api/tapes", service, {"name": "Mix"}).json()["id"]
    assert post(app, f"/api/tapes/{tape}/match", service).status == 409


def test_a_new_list_may_not_lose_a_choice_over_the_wire(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    app, tape = doubtful(service, catalogue)
    post(app, f"/api/tapes/{tape}/songs/1/from-list", service)
    got = post(app, f"/api/tapes/{tape}/songs", service, {"songs": [row("Other")]})
    assert got.status == 409
    assert "A Song" in got.json()["error"]


def test_the_same_list_sent_again_keeps_the_choice_on_its_own_row(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    rows = [row("A Song"), row("A Song"), row("B Song", length="3:00")]
    tape = a_tape(app, service, rows)
    post(app, f"/api/tapes/{tape}/songs/2/from-list", service)
    sent = post(app, f"/api/tapes/{tape}/songs", service, {"songs": rows})
    assert sent.status == 200
    assert [s["settled_by"] for s in sent.json()["songs"]] == ["", M.PERSON, ""]


def test_a_proposal_is_on_disk_and_reads_back(service) -> None:  # type: ignore[no-untyped-def]
    app = build(service)
    tape = a_tape(app, service)
    run(app, service, tape)
    saved = T.read(service.layout, tape)
    assert saved.songs[0].proposal is not None
    assert saved.songs[0].proposal.choice is not None
    assert saved.songs[0].proposal.choice.release == "rel-a"


def test_the_album_flows_route_still_reads_an_empty_object_as_no_releases(
    service, catalogue
) -> None:  # type: ignore[no-untyped-def]
    catalogue.malformed = {'release:"A Record"': b"{}"}
    got = get(build(service), "/api/mb/search?artist=A+Band&album=A+Record", service)
    assert (got.status, got.json()["releases"]) == (200, [])


def test_the_album_flows_search_is_unchanged_and_unpaced(service, catalogue) -> None:  # type: ignore[no-untyped-def]
    def refuse(_seconds: float) -> None:
        raise AssertionError("the album flow waited on the tape's pace")

    service.pace = MB.Pace(now=lambda: 0.0, sleep=refuse)
    app = build(service)
    got = get(app, "/api/mb/search?artist=A+Band&album=A+Record", service)
    assert got.status == 200
    assert set(got.json()["releases"][0]) == {
        "id",
        "title",
        "artist",
        "date",
        "country",
        "disambiguation",
        "formats",
        "tracks",
        "durations",
        "total",
        "vinyl",
        "has_durations",
        "describe",
    }
    query = urllib.parse.quote('artist:"A Band" AND release:"A Record"', safe="")
    assert catalogue.calls[0] == f"{MB.BASE}/release?query={query}&limit=10&fmt=json"
    assert catalogue.calls[1] == (
        f"{MB.BASE}/release/rel-a?inc=recordings+artist-credits&fmt=json"
    )
