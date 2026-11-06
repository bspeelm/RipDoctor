"""Looking a tape's songs up by song and artist, through a recording search's replies
and a library's rows. ADR-063."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.config.settings import Settings
from ripdoctor.core import filing as F
from ripdoctor.core import match as M
from ripdoctor.core import tape as TP
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from tests.test_web_routes import a_service, get, post
from tests.test_web_tape_cut import Cutting, cut, job_of, placed
from tests.test_web_tape_file import Beet
from tests.test_web_tape_match import Catalogue, Clock, an_edition, row, statuses
from tests.test_web_tapes import a_tape_runner, save_list


def a_release(
    mbid: str,
    title: str,
    tracks: list[tuple[str, int]],
    *,
    kind: str = "Album",
    extras: tuple[str, ...] = (),
    group: str = "",
    date: str = "1990",
    status: str = "Official",
) -> dict[str, object]:
    made = an_edition(
        mbid,
        title=title,
        tracks=tracks,
        group=group or f"g-{mbid}",
        date=date,
        status=status,
    )
    made["release-group"] = {
        "id": group or f"g-{mbid}",
        "primary-type": kind,
        "secondary-types": list(extras),
    }
    return made


def one_recording(made: dict[str, object], recording: str) -> dict[str, object]:
    """Every copy of a song is the one recording, as an album and its single are."""
    for medium in made["media"]:  # type: ignore[attr-defined]
        for track in medium["tracks"]:
            track["recording"]["id"] = recording
    return made


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue({})


@pytest.fixture
def service(tmp_path: Path, catalogue: Catalogue) -> Service:
    beet = Beet(Cutting(a_tape_runner()), tmp_path / "lib")
    made = a_service(tmp_path, runner=beet)  # type: ignore[arg-type]
    made.settings = Settings(library=str(tmp_path / "lib"), importer="beets")
    made.state_dir = str(tmp_path / "state")
    clock = Clock()
    catalogue.clock = clock
    made.fetcher = catalogue
    made.pace = MB.Pace(now=clock.now, sleep=clock.sleep)
    return made


def beet_of(service: Service) -> Beet:
    return service.runner  # type: ignore[return-value]


def a_tape(app, service: Service, *rows: dict[str, str]) -> str:  # type: ignore[no-untyped-def]
    tape = post(app, "/api/tapes", service, {"name": "Mix"}).json()["id"]
    assert save_list(app, tape, service, list(rows)).status == 200
    return tape


def looked_up(app, service: Service, tape: str, **body: object) -> dict:  # type: ignore[no-untyped-def,type-arg]
    assert post(app, f"/api/tapes/{tape}/match", service, body).status == 202
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert not view["job"]["error"]
    return view


def proposal(view: dict, number: int = 1) -> dict:  # type: ignore[type-arg]
    return view["songs"][number - 1]["proposal"]


def releases(view: dict, number: int = 1) -> dict:  # type: ignore[type-arg]
    return view["releases"][number - 1]


# ----------------------------------------------------------- what the search finds


def test_a_song_only_on_editions_an_album_search_would_have_passed_over_is_found(
    service: Service, catalogue: Catalogue
) -> None:
    """Four editions of the album, all without the song, once crowded out the fifth."""
    catalogue.albums["A Record"] = [
        a_release(f"without-{n}", "A Record", [("Other", 100)], date=f"199{n}")
        for n in range(4)
    ] + [a_release("with", "A Record", [("Other", 100), ("A Song", 226)], date="2026")]
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song", length="3:46")))
    assert statuses(view) == [M.MATCHED]
    choice = proposal(view)["choice"]
    assert (choice["release"], choice["number"]) == ("with", 2)


@pytest.mark.parametrize("listed", ["", "Not An Album Of Theirs"])
def test_a_wrong_or_empty_album_on_the_list_still_finds_the_song(
    service: Service, catalogue: Catalogue, listed: str
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song", album=listed)))
    assert statuses(view) == [M.MATCHED]
    assert proposal(view)["choice"]["release"] == "r"
    assert bool(proposal(view)["note"]) == bool(listed)


def test_a_compilation_is_the_only_home_of_a_blank_album_song_goes_to_review(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["Best Of"] = [
        a_release("c", "Best Of", [("A Song", 200)], extras=("Compilation",))
    ]
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song", album="")))
    assert statuses(view) == [M.REVIEW]
    assert view["all_settled"] is False


def test_the_remix_on_the_list_does_not_take_the_originals_place(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("lp", "A Record", [("A Song", 233)])]
    catalogue.albums["S"] = [
        a_release(
            "s", "A Song (Some Remix)", [("A Song (Some Remix)", 202)], kind="Single"
        )
    ]
    app = build(service)
    listed = row("A Song (Some Remix)", length="3:22")
    view = looked_up(app, service, a_tape(app, service, listed))
    assert statuses(view) == [M.MATCHED]
    assert proposal(view)["choice"]["release"] == "s"


def test_the_original_on_the_list_does_not_take_the_remixs_place(
    service: Service, catalogue: Catalogue
) -> None:
    """Asked for by a title both hold, and the remix's length is the list's."""
    catalogue.albums["A Record"] = [a_release("lp", "A Record", [("A Song", 233)])]
    catalogue.albums["S"] = [
        a_release(
            "s", "A Song (Some Remix)", [("A Song (Some Remix)", 202)], kind="Single"
        )
    ]
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song", length="3:22")))
    assert statuses(view) == [M.REVIEW]
    assert view["all_settled"] is False


def test_a_title_the_catalogue_only_has_shorter_is_offered_and_never_accepted(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("lp", "A Record", [("A Song", 200)])]
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song (Single Edit)")))
    assert statuses(view) == [M.REVIEW]
    assert [o["release"] for o in releases(view)["options"]] == ["lp"]


def test_a_lookup_that_fails_leaves_the_song_to_retry_and_stops_no_other(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("A Song", 200), ("B Song", 180)])
    ]
    catalogue.down = {'recording:"B Song"'}
    app = build(service)
    tape = a_tape(app, service, row("A Song"), row("B Song", length="3:00"))
    view = looked_up(app, service, tape)
    assert statuses(view) == [M.MATCHED, M.FAILED]
    catalogue.down = set()
    assert statuses(looked_up(app, service, tape)) == [M.MATCHED, M.MATCHED]


# ----------------------------------------------------------------- choosing by hand


def picked(app, service: Service, tape: str, **body: object):  # type: ignore[no-untyped-def]
    return post(app, f"/api/tapes/{tape}/songs/1/release", service, body)


def two_versions(catalogue: Catalogue) -> None:
    catalogue.albums["A Record"] = [
        a_release("near", "A Record", [("A Song", 202)], kind="Single"),
        a_release("far", "Long Version", [("A Song", 233)]),
    ]


def test_a_choice_more_than_three_seconds_off_needs_saying_twice(
    service: Service, catalogue: Catalogue
) -> None:
    """A click once settled a song on the 233 s original when the list said 202 s."""
    two_versions(catalogue)
    app = build(service)
    tape = a_tape(app, service, row("A Song", length="3:22"))
    looked_up(app, service, tape)
    refused = picked(app, service, tape, release="far")
    assert refused.status == 409
    answer = refused.json()
    assert answer["needs_confirm"] is True
    assert "31 s longer" in answer["error"]
    assert T.read(service.layout, tape).songs[0].proposal.choice.release == "near"  # type: ignore[union-attr]

    said = picked(app, service, tape, release="far", confirm=True)
    assert said.status == 200
    assert proposal(said.json())["choice"]["release"] == "far"
    assert said.json()["songs"][0]["settled_by"] == M.PERSON


def test_a_choice_within_three_seconds_needs_saying_once(
    service: Service, catalogue: Catalogue
) -> None:
    two_versions(catalogue)
    app = build(service)
    tape = a_tape(app, service, row("A Song", length="3:22"))
    looked_up(app, service, tape)
    assert picked(app, service, tape, release="near").status == 200


def test_a_choice_carries_its_track_so_no_id_or_number_is_typed(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("Other", 100), ("A Song", 240)])
    ]
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    looked_up(app, service, tape)
    catalogue.calls.clear()
    said = picked(app, service, tape, release="r", confirm=True)
    assert proposal(said.json())["choice"]["number"] == 2
    assert catalogue.calls == []


def two_recordings_on_one_release(service: Service, tape: str) -> None:
    choice = M.Choice(
        "r",
        "g",
        "A Record",
        "A Band",
        "1990",
        "Official",
        track_title="A Song",
        number=1,
        disc=1,
        length=200.0,
        recording="the-one",
    )
    other = replace(
        choice, track_title="A Song (Remix)", number=2, recording="the-other"
    )
    current = T.read(service.layout, tape)
    made = M.Proposal(M.REVIEW, "a doubt", None, (choice, other))
    T.save(service.layout, TP.with_proposals(current, {1: made}))


def test_an_option_chooses_its_own_recording_and_not_the_first_on_its_release(
    service: Service,
) -> None:
    """The page sent the release alone, so pressing one line took another recording."""
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    two_recordings_on_one_release(service, tape)
    said = picked(app, service, tape, release="r", recording="the-other")
    assert said.status == 200
    assert proposal(said.json())["choice"]["recording"] == "the-other"
    assert proposal(said.json())["choice"]["number"] == 2


def test_an_option_no_longer_offered_is_refused_and_settles_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    two_recordings_on_one_release(service, tape)
    refused = picked(app, service, tape, release="r", recording="gone")
    assert refused.status == 409
    assert "no longer offers" in refused.json()["error"]
    assert T.read(service.layout, tape).songs[0].settled_by == ""


def test_an_id_given_with_no_recording_keeps_its_path(service: Service) -> None:
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    two_recordings_on_one_release(service, tape)
    said = picked(app, service, tape, release="r")
    assert proposal(said.json())["choice"]["recording"] == "the-one"


def searched(app, service: Service, tape: str, **body: object):  # type: ignore[no-untyped-def]
    return post(app, f"/api/tapes/{tape}/songs/1/search", service, body)


def test_a_search_inside_a_row_offers_what_it_finds_and_settles_nothing(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["Their Record"] = [
        a_release("r", "Their Record", [("Other Name", 200)], date="1999")
    ]
    catalogue.albums["Their Record"][0]["artist-credit"] = [{"name": "Their Band"}]
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    assert statuses(looked_up(app, service, tape)) == [M.NONE]

    said = searched(app, service, tape, title="Other Name", artist="Their Band")
    assert said.status == 200
    found = said.json()["options"]
    assert [o["release"] for o in found] == ["r"]
    assert found[0]["track_title"] == "Other Name"
    assert (found[0]["delta"], found[0]["year"]) == (0.0, "1999")
    saved = T.read(service.layout, tape).songs[0]
    assert saved.settled_by == ""
    assert saved.proposal is not None and saved.proposal.status == M.NONE

    catalogue.calls.clear()
    chosen = picked(app, service, tape, release="r")
    assert chosen.status == 200
    assert proposal(chosen.json())["choice"]["number"] == 1
    assert catalogue.calls == []


def test_a_search_with_nothing_found_says_so(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    said = searched(app, service, tape, title="Nothing", artist="Nobody").json()
    assert said["options"] == []
    assert "Nobody" in said["reason"]


def test_a_search_is_refused_on_a_filed_song_and_while_the_catalogue_is_down(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    catalogue.down = {"recording"}
    assert searched(app, service, tape, title="A", artist="B").status == 503
    catalogue.down = set()
    current = T.read(service.layout, tape)
    T.save(service.layout, F.recorded(current, 1, F.FILED, "/lib/a.flac"))
    assert searched(app, service, tape, title="A", artist="B").status == 409


# ---------------------------------------------------------------- the library first


def owned_row(service: Service, title: str = "A Song", **fields: str) -> Path:
    return beet_of(service).owns(
        title,
        length=fields.pop("length", "3:20"),
        albumartist="A Band",
        album=fields.pop("album", "A Record"),
        **fields,
    )


def test_a_copy_with_this_recording_is_shown_at_lookup_and_counts_as_settled(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    where = owned_row(service, mb_trackid="rec-r-1", mb_albumid="r")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.OWNED]
    assert view["all_settled"] is True
    assert view["songs"][0]["settled_by"] == M.CATALOGUE
    assert releases(view)["copy"] == {
        "album_artist": "A Band",
        "album": "A Record",
        "path": str(where),
        "release": "r",
    }


def test_a_row_agreeing_by_name_settles_nothing_when_nothing_was_matched(
    service: Service, catalogue: Catalogue
) -> None:
    """It settled itself with nobody looking, so a live take of the same length answered
    for the song."""
    where = owned_row(service, album="Live Somewhere", mb_trackid="their-own-id")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.REVIEW]
    assert view["songs"][0]["settled_by"] == ""
    assert releases(view)["copy"] is None
    assert releases(view)["maybe_copy"] == str(where)


def test_a_review_with_a_row_of_another_recording_is_asked_about_and_not_owned(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("A Song", 200)], status="Bootleg")
    ]
    where = owned_row(service, album="Live Somewhere", mb_trackid="their-own-id")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.REVIEW]
    assert view["songs"][0]["settled_by"] == ""
    assert releases(view)["maybe_copy"] == str(where)


def test_a_review_with_a_row_of_no_recording_id_is_asked_about_and_not_owned(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("A Song", 200)], status="Bootleg")
    ]
    where = owned_row(service, album="Live Somewhere")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.REVIEW]
    assert releases(view)["maybe_copy"] == str(where)


def test_a_review_whose_candidate_is_the_rows_recording_is_asked_about_and_not_owned(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("A Song", 200)], status="Bootleg")
    ]
    where = owned_row(service, mb_trackid="rec-r-1", mb_albumid="r")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.REVIEW]
    assert view["songs"][0]["settled_by"] == ""
    assert releases(view)["copy"] is None
    assert releases(view)["maybe_copy"] == str(where)


def test_a_live_album_of_the_list_s_length_was_owned_with_nobody_asked(
    service: Service, catalogue: Catalogue
) -> None:
    """The studio take is 230 s, the list says 200 s, and the library's live album runs
    201 s. Its id was on offer, so title, artist and length were taken as proof."""
    catalogue.albums["A Record"] = [
        a_release("studio", "A Record", [("A Song", 230)]),
        one_recording(
            a_release("live", "Live at Somewhere", [("A Song", 201)]), "rec-live"
        ),
    ]
    where = owned_row(
        service,
        album="Live at Somewhere",
        length="3:21",
        mb_trackid="rec-live",
        mb_albumid="live",
    )
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.REVIEW]
    assert view["songs"][0]["settled_by"] == ""
    assert releases(view)["copy"] is None
    assert releases(view)["maybe_copy"] == str(where)


def test_a_remix_album_s_copy_was_owned_with_nobody_asked(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        one_recording(
            a_release("remixes", "The Remixes", [("A Song", 200)], extras=("Remix",)),
            "rec-remix",
        ),
    ]
    where = owned_row(
        service, album="The Remixes", mb_trackid="rec-remix", mb_albumid="remixes"
    )
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.REVIEW]
    assert releases(view)["copy"] is None
    assert releases(view)["maybe_copy"] == str(where)


def test_a_failed_lookup_never_settles_on_a_row_and_stays_to_be_retried(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.down = {"recording"}
    where = owned_row(service, mb_trackid="their-own-id")
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    view = looked_up(app, service, tape)
    assert statuses(view) == [M.FAILED]
    assert view["songs"][0]["settled_by"] == ""
    assert view["all_settled"] is False
    assert releases(view)["maybe_copy"] == str(where)
    assert str(where) in releases(view)["library_note"]
    assert [s.number for s in TP.to_match(T.read(service.layout, tape))] == [1]


def test_a_person_can_use_a_copy_a_lookup_only_asked_about(
    service: Service, catalogue: Catalogue
) -> None:
    where = owned_row(service, album="Elsewhere", mb_trackid="their-own-id")
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    looked_up(app, service, tape)
    used = post(
        app, f"/api/tapes/{tape}/songs/1/use-copy", service, {"path": str(where)}
    )
    assert used.status == 200
    assert used.json()["songs"][0]["filing"] == F.ALREADY


def test_a_row_of_another_length_is_not_a_copy(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    owned_row(service, length="3:53", album="Elsewhere")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.MATCHED]
    assert releases(view)["copy"] is None


def test_a_doubts_best_guess_proves_nothing_by_its_recording(
    service: Service, catalogue: Catalogue
) -> None:
    """The original is owned and the list says the shorter remix of the same title."""
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 233)])]
    owned_row(service, length="3:53", mb_trackid="rec-r-1", mb_albumid="r")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song", length="3:22")))
    assert statuses(view) == [M.REVIEW]
    assert releases(view)["copy"] is None


def test_a_copy_nothing_proves_is_asked_about_at_review_with_use_copy_to_hand(
    service: Service, catalogue: Catalogue
) -> None:
    """It used to wait for File, and then fail the song as not confirmed."""
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    where = owned_row(service, album="Elsewhere")
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    view = looked_up(app, service, tape)
    assert statuses(view) == [M.REVIEW]
    assert view["songs"][0]["unconfirmed"] == str(where)
    assert releases(view)["maybe_copy"] == str(where)
    assert str(where) in proposal(view)["reason"]
    assert view["all_settled"] is False

    used = post(
        app, f"/api/tapes/{tape}/songs/1/use-copy", service, {"path": str(where)}
    )
    assert used.status == 200
    after = used.json()["songs"][0]
    assert (after["filing"], after["path"]) == (F.ALREADY, str(where))
    assert after["settled_by"] == M.PERSON


def test_a_library_that_cannot_be_read_says_so_and_the_lookup_goes_on(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    beet_of(service).ls_fails = True
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.MATCHED]
    assert "could not be read" in releases(view)["library_note"]
    assert "database is locked" in releases(view)["library_note"]


def test_a_stale_row_is_said_at_lookup_and_changes_nothing_about_the_answer(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    owned_row(service, mb_trackid="rec-r-1").unlink()
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.MATCHED]
    assert "the file is not there" in releases(view)["library_note"]


def test_no_library_configured_is_no_note_and_asks_beets_nothing(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    service.settings = Settings()
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert statuses(view) == [M.MATCHED]
    assert releases(view)["library_note"] == ""
    assert not [c for c in beet_of(service).calls if c[0] == "beet"]


def test_an_edition_the_library_has_comes_first_and_is_marked(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("mine", "My Edition", [("A Song", 200)], date="2010"),
        a_release("named", "A Record", [("A Song", 200)], date="1990"),
    ]
    owned_row(service, title="Another Song", album="My Edition", mb_albumid="mine")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert proposal(view)["choice"]["release"] == "mine"
    options = {o["release"]: o["in_library"] for o in releases(view)["options"]}
    assert options == {"mine": True, "named": False}


def test_one_artists_rows_are_read_once_in_a_job(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("A Song", 200), ("B Song", 180)])
    ]
    app = build(service)
    looked_up(
        app,
        service,
        a_tape(app, service, row("A Song"), row("B Song", length="3:00")),
    )
    asked = [
        c
        for c in beet_of(service).calls
        if c[0] == "beet" and F.term("artist", "A Band") in c
    ]
    assert len(asked) == 1


def test_the_same_copy_does_not_answer_for_two_rows_of_the_list(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    owned_row(service, mb_trackid="rec-r-1", mb_albumid="r")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song"), row("A Song")))
    assert statuses(view)[0] == M.OWNED
    assert statuses(view)[1] != M.OWNED


# ---------------------------------------------------- beside an edition already held


def an_owned_edition(service: Service, catalogue: Catalogue) -> None:
    """The album is owned as an edition without the song. Another has it, as does a
    single."""
    catalogue.albums["A Record"] = [
        one_recording(
            a_release("theirs", "A Record", [("A Song", 200)], date="2020"), "rec"
        ),
        one_recording(
            a_release(
                "single", "A Song", [("A Song", 200)], kind="Single", date="2021"
            ),
            "rec",
        ),
    ]
    owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="rec-mine")
    owned_row(service, title="Mine Too", mb_albumid="mine", mb_trackid="rec-mine-2")


def test_a_release_beside_an_owned_edition_says_what_will_happen_and_offers_its_single(
    service: Service, catalogue: Catalogue
) -> None:
    an_owned_edition(service, catalogue)
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert proposal(view)["choice"]["release"] == "theirs"
    note = releases(view)["edition_note"]
    assert note["album"] == "A Record"
    assert (note["track"], note["tracks"]) == (1, 1)
    assert note["single"] == "single"
    assert note["text"] == (
        "You have the 2-track A Record. This song is only on the 1-track version."
    )
    add, alone = note["choices"]
    assert add["label"] == "Add to A Record" and add["current"]
    assert "your A Record folder as track 1" in add["said"]
    assert "Your 2 songs don't change" in add["said"]
    assert alone["label"] == "File as a single" and not alone["current"]
    assert alone["release"] == "single"
    assert "its own folder, A Band / A Song" in alone["said"]
    for jargon in ("edition", "second album entry", "another entry"):
        assert jargon not in note["text"] + add["said"] + alone["said"]


def test_a_note_says_track_clash_only_when_the_library_has_that_number(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        one_recording(a_release("theirs", "A Record", [("A Song", 200)]), "rec")
    ]
    owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="m1", track="1")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    add = releases(view)["edition_note"]["choices"][0]
    assert "next to your other track 1" in add["said"]


def test_a_note_promised_the_album_s_folder_where_paths_give_it_a_folder_of_its_own(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        one_recording(a_release("theirs", "A Record", [("A Song", 200)]), "rec")
    ]
    owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="m1", track="1")
    beet_of(service).album_path = "$albumartist/$album%aunique{}/$track $title"
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    add = releases(view)["edition_note"]["choices"][0]
    assert "a folder of its own, apart from your A Record" in add["said"]
    assert "next to" not in add["said"] and "your A Record folder" not in add["said"]


def test_a_note_says_only_what_is_true_either_way_when_paths_cannot_be_read(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        one_recording(a_release("theirs", "A Record", [("A Song", 200)]), "rec")
    ]
    owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="m1", track="1")
    beet_of(service).album_path = ""
    original = beet_of(service).run

    def no_config(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "config" in argv:
            raise OSError("no such thing")
        return original(argv, stdin=stdin, timeout=timeout)

    beet_of(service).run = no_config  # type: ignore[method-assign]
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    add = releases(view)["edition_note"]["choices"][0]
    assert "or in a folder beside it" in add["said"]
    assert "next to" not in add["said"]


def test_a_note_about_another_album_artist_does_not_promise_the_same_folder(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        one_recording(a_release("theirs", "A Record", [("A Song", 200)]), "rec")
    ]
    owned = owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="m1")
    next(r for r in beet_of(service).rows if r["path"] == str(owned)).update(
        albumartist="Other People"
    )
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    add = releases(view)["edition_note"]["choices"][0]
    assert "a folder of its own" in add["said"]


def test_a_note_promised_the_folder_of_an_album_with_another_title(
    service: Service, catalogue: Catalogue
) -> None:
    """Beets names the folder from the release, so the deluxe one is another folder."""
    catalogue.albums["A Record"] = [
        one_recording(a_release("theirs", "A Record (Deluxe)", [("A Song", 200)]), "r")
    ]
    owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="m1", track="1")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    add = releases(view)["edition_note"]["choices"][0]
    assert "a folder of its own, apart from your A Record" in add["said"]
    assert "your A Record folder" not in add["said"]


def test_a_note_promised_a_folder_where_a_year_in_the_path_makes_another(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        one_recording(a_release("theirs", "A Record", [("A Song", 200)]), "rec")
    ]
    owned_row(service, title="Mine", mb_albumid="mine", mb_trackid="m1", track="1")
    beet_of(service).album_path = "$albumartist/$year - $album/$track $title"
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    add = releases(view)["edition_note"]["choices"][0]
    assert "or in a folder beside it" in add["said"]
    assert "your A Record folder" not in add["said"]


def test_a_note_about_various_artists_says_only_what_is_true_either_way() -> None:
    """Beets files such a release by its `comp` path, which `apart` does not read."""
    choice = M.Choice(
        "theirs", "g", "A Record", "Various Artists", "", "", number=2, recording="r"
    )
    held = M.Held("mine", "A Record", "Various Artists", 3, (1, 2, 3))
    made = M.Proposal(M.MATCHED, choice=choice, held=(held,), apart=False)
    song = TP.Song(1, "A Song", "A Band", "A Record", 200.0, proposal=made)
    add = F.edition_note(song)["choices"][0]  # type: ignore[index]
    assert "or in a folder beside it" in add["said"]


def test_the_paths_are_read_once_in_a_lookup_however_many_songs_it_asks_about(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("A Song", 200), ("B Song", 200)])
    ]
    app = build(service)
    tape = a_tape(app, service, row("A Song"), row("B Song"))
    looked_up(app, service, tape)
    asked = [c for c in beet_of(service).calls if "config" in c and "-d" in c]
    assert len(asked) == 1


def test_the_note_stays_after_the_single_is_chosen_with_the_single_marked(
    service: Service, catalogue: Catalogue
) -> None:
    an_owned_edition(service, catalogue)
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    looked_up(app, service, tape)
    note = releases(picked(app, service, tape, release="single").json())["edition_note"]
    add, alone = note["choices"]
    assert alone["current"] and not add["current"]
    assert add["release"] == "theirs"


def test_no_note_on_an_edition_the_library_has_or_an_album_it_does_not(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [a_release("r", "A Record", [("A Song", 200)])]
    owned_row(service, title="Mine", mb_albumid="r")
    app = build(service)
    view = looked_up(app, service, a_tape(app, service, row("A Song")))
    assert releases(view)["edition_note"] is None

    catalogue.albums["Other"] = [a_release("o", "Other", [("B Song", 200)])]
    other = a_tape(app, service, row("B Song", album="Other"))
    assert releases(looked_up(app, service, other))["edition_note"] is None


def test_a_person_choosing_beside_an_owned_edition_gets_the_note_too(
    service: Service, catalogue: Catalogue
) -> None:
    an_owned_edition(service, catalogue)
    app = build(service)
    tape = a_tape(app, service, row("A Song"))
    looked_up(app, service, tape)
    said = picked(app, service, tape, release="single")
    assert releases(said.json())["edition_note"]["choices"][1]["current"]
    said = picked(app, service, tape, release="theirs")
    assert releases(said.json())["edition_note"]["choices"][0]["current"]


def test_a_view_of_a_song_never_looked_up_offers_nothing(service: Service) -> None:
    app = build(service)
    view = get(
        app,
        f"/api/tapes/{a_tape(app, service, row('A Song'))}",
        service,
    ).json()
    assert releases(view) == {
        "number": 1,
        "options": [],
        "copy": None,
        "maybe_copy": "",
        "edition_note": None,
        "note": "",
        "library_note": "",
    }


def test_a_copy_found_at_lookup_is_found_again_at_file_and_written_to_the_playlist(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("One", 5), ("Two", 5)])
    ]
    beet = beet_of(service)
    one = owned_row(service, "One", length="0:05", mb_trackid="rec-r-1", mb_albumid="r")
    two = owned_row(service, "Two", length="0:05", mb_trackid="rec-r-2", mb_albumid="r")
    app = build(service)
    tape = placed(app, service)
    view = looked_up(app, service, tape)
    assert statuses(view) == [M.OWNED, M.OWNED]
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert post(app, f"/api/tapes/{tape}/file", service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert not [c for c in beet.calls if "import" in c]
    songs = T.read(service.layout, tape).songs
    assert [s.filing for s in songs] == [F.ALREADY, F.ALREADY]
    assert [s.path for s in songs] == [str(one), str(two)]
    playlist = get(app, f"/api/tapes/{tape}", service).json()["playlist"]
    assert Path(playlist["path"]).read_text(encoding="utf-8").count(".flac") == 2


def an_owned_song_and_another(
    app,
    service: Service,
    catalogue: Catalogue,  # type: ignore[no-untyped-def]
) -> str:
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("One", 5), ("Two", 5)])
    ]
    owned_row(service, "One", length="0:05", mb_trackid="rec-r-1", mb_albumid="r")
    tape = placed(app, service)
    view = looked_up(app, service, tape)
    assert statuses(view) == [M.OWNED, M.MATCHED]
    beet_of(service).recordings["Two"] = "rec-r-2"
    return tape


def test_a_song_found_in_the_library_was_cut_anyway_and_needs_no_cut(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = an_owned_song_and_another(app, service, catalogue)
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    done = T.read(service.layout, tape)
    assert done.songs[0].cut is None and done.songs[1].cut is not None
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert [r["cut"] for r in sorted(view["ear"], key=lambda r: r["number"])] == [
        TP.NO_CUT,
        TP.CUT,
    ]
    assert view["all_cut"] is True
    assert view["file_refusal"] == ""
    assert post(app, f"/api/tapes/{tape}/file", service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert [s.filing for s in T.read(service.layout, tape).songs] == [
        F.ALREADY,
        F.FILED,
    ]


def test_a_failed_cut_of_a_song_the_library_has_does_not_stop_the_rest_filing(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = an_owned_song_and_another(app, service, catalogue)
    beet_of(service).inner.bad.add("One")  # type: ignore[attr-defined]
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert get(app, f"/api/tapes/{tape}", service).json()["file_refusal"] == ""


def test_a_doubt_on_a_song_the_library_has_asks_nobody_and_stops_no_cut(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = an_owned_song_and_another(app, service, catalogue)
    current = T.read(service.layout, tape)
    doubt = replace(current.songs[0], verdict=TP.DOUBT, owes_ear=True)
    T.save(service.layout, replace(current, songs=(doubt, current.songs[1])))
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["cut_refusal"] == ""
    assert not any(r["doubtful"] for r in view["ear"])


def test_a_song_whose_library_copy_went_needs_cutting_again(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = an_owned_song_and_another(app, service, catalogue)
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    beet = beet_of(service)
    beet.rows = [r for r in beet.rows if r["title"] != "One"]
    assert post(app, f"/api/tapes/{tape}/file", service).status == 202
    assert job_of(app, tape, service)
    one = T.read(service.layout, tape).songs[0]
    assert one.proposal is not None and one.proposal.status == M.REVIEW
    assert TP.cut_status(one, 2, set()) == TP.NOT_CUT


@pytest.mark.parametrize("by_id", [True, False])
def test_a_copy_found_at_lookup_and_gone_at_file_goes_back_to_choosing_an_album(
    service: Service, catalogue: Catalogue, by_id: bool
) -> None:
    """It went on to file silently from the list, under whatever album the list said."""
    catalogue.albums["A Record"] = [
        a_release("r", "A Record", [("One", 5), ("Two", 5)])
    ]
    gone = owned_row(
        service, "One", length="0:05", mb_trackid="rec-r-1", mb_albumid="r"
    )
    owned_row(service, "Two", length="0:05", mb_trackid="rec-r-2", mb_albumid="r")
    app = build(service)
    tape = placed(app, service)
    looked_up(app, service, tape)
    if not by_id:
        current = T.read(service.layout, tape)
        one = current.songs[0]
        by_name = replace(one, proposal=replace(one.proposal, choice=None))  # type: ignore[arg-type]
        T.save(service.layout, replace(current, songs=(by_name, current.songs[1])))
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]

    beet = beet_of(service)
    beet.rows = [r for r in beet.rows if r["path"] != str(gone)]
    gone.unlink()
    assert post(app, f"/api/tapes/{tape}/file", service).status == 202
    assert "did not file" in job_of(app, tape, service)["error"]
    assert not [c for c in beet.calls if "import" in c]

    view = get(app, f"/api/tapes/{tape}", service).json()
    one = T.read(service.layout, tape).songs[0]
    assert one.filing == F.FAILED
    assert "is not there any more" in one.filing_note
    assert "choose an album" in one.filing_note
    assert one.settled_by == ""
    assert one.proposal is not None and one.proposal.status == M.REVIEW
    assert one.proposal.copy is None
    assert "no settled release" in view["file_refusal"]
    assert view["songs"][1]["filing"] == F.ALREADY
