"""What editing a tape's list leaves alone, over the wire. ADR-063."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ripdoctor.core import filing as F
from ripdoctor.core import match as M
from ripdoctor.core import tape as TP
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from tests.test_web_routes import a_service, get, post
from tests.test_web_tape_cut import TWO, placed
from tests.test_web_tapes import a_tape_runner, save_list, seeing, send_csv


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return a_service(tmp_path, runner=a_tape_runner())


def rows_of(service: Service, tape: str) -> list[dict[str, Any]]:
    return [
        {"title": s.title, "artist": s.artist, "album": s.album, "length": s.length}
        for s in T.read(service.layout, tape).songs
    ]


def send(app, service: Service, tape: str, rows: list[dict[str, Any]], **more: object):  # type: ignore[no-untyped-def]
    return save_list(app, tape, service, rows, **more)


def edges(service: Service, tape: str) -> list[tuple[float | None, float | None]]:
    return [(s.start, s.end) for s in T.read(service.layout, tape).songs]


def hand_set(app, service: Service, tape: str) -> None:  # type: ignore[no-untyped-def]
    current = T.read(service.layout, tape).songs[1]
    start, end = current.start, current.end
    body = seeing(service, tape, 2, start, (end or 0.0) - 0.5)
    assert post(app, f"/api/tapes/{tape}/songs/2/edges", service, body).status == 200


@pytest.mark.parametrize("field", ["title", "artist", "album"])
def test_editing_a_songs_words_keeps_a_hand_set_boundary(
    service: Service, field: str
) -> None:
    app = build(service)
    tape = placed(app, service)
    hand_set(app, service, tape)
    before = edges(service, tape)
    rows = rows_of(service, tape)
    rows[1][field] = "Changed"
    answer = send(app, service, tape, rows)
    assert answer.status == 200
    after = T.read(service.layout, tape)
    assert edges(service, tape) == before
    assert after.songs[1].set_by == TP.EAR
    assert getattr(after.songs[1], field) == "Changed"


def test_editing_a_songs_words_keeps_found_boundaries_and_what_was_heard(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    assert post(app, f"/api/tapes/{tape}/songs/1/heard", service).status == 200
    before = edges(service, tape)
    rows = rows_of(service, tape)
    rows[0]["title"] = "Won"
    assert send(app, service, tape, rows).status == 200
    assert edges(service, tape) == before
    assert T.read(service.layout, tape).songs[0].heard is True
    assert T.read(service.layout, tape).gaps


def test_editing_a_songs_words_asks_about_only_that_song_again(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    current = T.read(service.layout, tape)
    made = M.Proposal(M.REVIEW, "a doubt")
    T.save(service.layout, TP.with_proposals(current, {1: made, 2: made}))
    rows = rows_of(service, tape)
    rows[0]["artist"] = "Another Band"
    assert send(app, service, tape, rows).status == 200
    after = T.read(service.layout, tape)
    assert after.songs[0].proposal is None
    assert after.songs[1].proposal == made
    assert [s.number for s in TP.to_match(after)] == [1]


def test_a_persons_choice_goes_with_the_words_it_answered(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    assert post(app, f"/api/tapes/{tape}/songs/1/from-list", service).status == 200
    rows = rows_of(service, tape)
    rows[0]["title"] = "Won"
    assert send(app, service, tape, rows).status == 200
    song = T.read(service.layout, tape).songs[0]
    assert (song.proposal, song.settled_by) == (None, "")


def test_the_same_list_sent_again_changes_nothing(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    hand_set(app, service, tape)
    before = T.read(service.layout, tape)
    assert send(app, service, tape, rows_of(service, tape)).status == 200
    assert T.read(service.layout, tape) == before


def test_a_new_length_keeps_the_boundaries_and_measures_them_again(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    before = T.read(service.layout, tape).songs[0]
    assert before.verdict == TP.AGREES
    rows = rows_of(service, tape)
    rows[0]["length"] = 60.0
    assert send(app, service, tape, rows).status == 200
    song = T.read(service.layout, tape).songs[0]
    assert (song.start, song.end) == (before.start, before.end)
    assert song.verdict == TP.DOUBT
    assert song.delta == pytest.approx((before.end or 0) - (before.start or 0) - 60.0)
    assert "55.0 s shorter than the list" in song.reason


def test_a_new_length_on_a_hand_set_boundary_moves_nothing_and_stays_theirs(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    hand_set(app, service, tape)
    before = T.read(service.layout, tape).songs[1]
    rows = rows_of(service, tape)
    rows[1]["length"] = 60.0
    assert send(app, service, tape, rows).status == 200
    song = T.read(service.layout, tape).songs[1]
    assert (song.start, song.end, song.set_by) == (before.start, before.end, TP.EAR)
    assert song.delta == pytest.approx((before.end or 0) - (before.start or 0) - 60.0)
    assert not TP.doubtful(song)


def test_a_new_length_unsettles_what_the_catalogue_said_and_not_what_a_person_did(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    choice = M.Choice("r", "g", "A Record", "A Band", "1990", "Official", number=1)
    current = T.read(service.layout, tape)
    matched = M.Proposal(M.MATCHED, "", choice)
    current = TP.with_proposals(current, {1: matched})
    current = TP.settle(current, 2, choice)
    T.save(service.layout, current)
    rows = rows_of(service, tape)
    rows[0]["length"] = 60.0
    rows[1]["length"] = 60.0
    assert send(app, service, tape, rows).status == 200
    one, two = T.read(service.layout, tape).songs
    assert (one.proposal, one.settled_by) == (None, "")
    assert two.settled_by == M.PERSON
    assert two.proposal is not None and two.proposal.choice == choice


@pytest.mark.parametrize("state", [F.FILED, F.ALREADY, F.UNKNOWN])
def test_a_song_the_library_holds_or_may_hold_cannot_be_edited(
    service: Service, state: str
) -> None:
    app = build(service)
    tape = placed(app, service)
    current = T.read(service.layout, tape)
    T.save(service.layout, F.recorded(current, 1, state, "/lib/a.flac"))
    rows = rows_of(service, tape)
    rows[0]["title"] = "Won"
    refused = send(app, service, tape, rows)
    assert refused.status == 409
    assert "stay as they are" in refused.json()["error"]
    assert T.read(service.layout, tape).songs[0].title == "One"


def test_a_song_sent_to_beets_with_no_answer_cannot_be_edited(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    current = T.read(service.layout, tape)
    begun = (replace(current.songs[0], import_begun=True), current.songs[1])
    T.save(service.layout, replace(current, songs=begun))
    rows = rows_of(service, tape)
    rows[0]["length"] = 60.0
    assert send(app, service, tape, rows).status == 409


def test_a_song_whose_filing_failed_can_be_edited_and_tried_afresh(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    current = T.read(service.layout, tape)
    T.save(service.layout, F.recorded(current, 1, F.FAILED, note="beets said no"))
    rows = rows_of(service, tape)
    rows[0]["title"] = "Won"
    assert send(app, service, tape, rows).status == 200
    song = T.read(service.layout, tape).songs[0]
    assert (song.filing, song.filing_note) == (TP.PENDING, "")


@pytest.mark.parametrize("how", ["moved", "added", "removed"])
def test_moving_adding_or_removing_with_a_hand_set_boundary_is_refused(
    service: Service, how: str
) -> None:
    app = build(service)
    tape = placed(app, service)
    hand_set(app, service, tape)
    before = T.read(service.layout, tape)
    rows = rows_of(service, tape)
    if how == "moved":
        rows.reverse()
    elif how == "added":
        rows.append({"title": "More", "artist": "A", "album": "", "length": 5})
    else:
        rows.pop()
    refused = send(app, service, tape, rows, confirm=True)
    assert refused.status == 409
    assert "which audio belongs to which song" in refused.json()["error"]
    assert T.read(service.layout, tape) == before


@pytest.mark.parametrize("how", ["moved", "added", "removed"])
def test_moving_adding_or_removing_found_boundaries_is_said_twice(
    service: Service, how: str
) -> None:
    app = build(service)
    tape = placed(app, service)
    before = T.read(service.layout, tape)
    rows = rows_of(service, tape)
    if how == "moved":
        rows.reverse()
    elif how == "added":
        rows.append({"title": "More", "artist": "A", "album": "", "length": 5})
    else:
        rows.pop()
    asked = send(app, service, tape, rows)
    assert asked.status == 409
    assert asked.json()["needs_confirm"] is True
    assert T.read(service.layout, tape) == before
    assert send(app, service, tape, rows, confirm=True).status == 200
    assert all(s.start is None for s in T.read(service.layout, tape).songs)


def test_a_row_taking_on_another_rows_words_is_a_move_and_not_an_edit(
    service: Service,
) -> None:
    """Moving a row and editing it in one list has to read as moved, or audio is left
    against the wrong song with every boundary kept."""
    app = build(service)
    tape = placed(app, service)
    rows = rows_of(service, tape)
    rows[1], rows[0] = dict(rows[0]), {**rows[1], "title": "Deux"}
    assert send(app, service, tape, rows).status == 409


def test_a_list_built_before_any_boundary_needs_no_saying_twice(
    service: Service,
) -> None:
    app = build(service)
    tape = post(app, "/api/tapes", service, {"name": "Mix"}).json()["id"]
    assert send_csv(app, tape, service, TWO).status == 200
    rows = rows_of(service, tape)
    rows.reverse()
    assert send(app, service, tape, rows).status == 200
    assert send_csv(app, tape, service, TWO).status == 200


def test_a_file_over_found_boundaries_is_said_twice_and_clears_nothing_until_it_is(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    before = T.read(service.layout, tape)
    asked = send_csv(app, tape, service, TWO)
    assert asked.status == 409
    assert asked.json()["needs_confirm"] is True
    assert T.read(service.layout, tape) == before
    assert send_csv(app, tape, service, TWO, confirm=True).status == 200
    assert all(s.start is None for s in T.read(service.layout, tape).songs)


def test_an_edit_on_the_way_to_a_job_button_clears_nothing(service: Service) -> None:
    """A click on Match or Find blurs an edited field, so the list is saved on the way
    to the job, and that once took every found boundary with it."""
    app = build(service)
    tape = placed(app, service)
    before = edges(service, tape)
    rows = rows_of(service, tape)
    rows[0]["title"] = "Won"
    assert send(app, service, tape, rows).status == 200
    assert post(app, f"/api/tapes/{tape}/match", service, {}).status == 202
    assert edges(service, tape) == before

    rows = rows_of(service, tape)
    rows[1]["album"] = "Another Record"
    assert send(app, service, tape, rows).status == 200
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert [(s["start"], s["end"]) for s in view["songs"]] == before
    assert view["gaps"]


def test_a_list_edit_that_would_clear_boundaries_cannot_slip_through_a_job_button(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    before = edges(service, tape)
    rows = rows_of(service, tape)
    rows.reverse()
    assert send(app, service, tape, rows).status == 409
    assert post(app, f"/api/tapes/{tape}/match", service, {}).status == 202
    assert edges(service, tape) == before
