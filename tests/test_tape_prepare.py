"""One job from a tape's audio and list to boundaries and albums. ADR-063."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from ripdoctor.core import match as M
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import incoming as IN
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from tests.test_web_routes import a_service, get, post
from tests.test_web_tape_cut import TWO, Cutting, cuts_sent, placed
from tests.test_web_tape_match import Catalogue, Clock, an_edition
from tests.test_web_tapes import a_new_tape, a_tape_runner, save_list, send_csv, upload


def both_songs() -> list[dict[str, object]]:
    return [an_edition("r", tracks=[("One", 5), ("Two", 5)])]


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue({"A Record": both_songs()})


@pytest.fixture
def service(tmp_path: Path, catalogue: Catalogue) -> Service:
    made = a_service(tmp_path, runner=Cutting(a_tape_runner()))  # type: ignore[arg-type]
    clock = Clock()
    catalogue.clock = clock
    made.fetcher = catalogue
    made.pace = MB.Pace(now=clock.now, sleep=clock.sleep)
    return made


def a_service_as(service: Service) -> Service:
    """The same library and tapes, as a process that has just started sees them."""
    made = a_service(
        service.layout.root.parent, runner=service.runner, layout=service.layout
    )
    made.fetcher, made.pace = service.fetcher, service.pace
    return made


def ready(app, service: Service) -> str:  # type: ignore[no-untyped-def]
    tape = a_new_tape(app, service)
    assert send_csv(app, tape, service, TWO).status == 200
    upload(app, tape, service)
    return tape


def prepared(app, service: Service, tape: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    assert post(app, f"/api/tapes/{tape}/prepare", service).status == 202
    return get(app, f"/api/job/{T.key_of(tape)}", service).json()


def test_one_job_flows_through_audio_boundaries_and_lookup(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is True
    job = prepared(app, service, tape)
    assert job["op"] == "prepare"
    assert not job["error"]
    assert job["done"] is True
    assert job["phase"] == "albums"
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["audio"] is True
    assert all(s["start"] is not None for s in view["songs"])
    assert [s["proposal"]["status"] for s in view["songs"]] == [M.MATCHED, M.MATCHED]
    assert view["prepare_needed"] is False
    assert view["cut_refusal"] == ""
    assert IN.state(service.layout, T.key_of(tape)) is None


def test_each_phase_is_reported_as_it_runs(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = ready(app, service)
    jobs = service.jobs.running
    seen: list[str] = []

    def phase_now() -> None:
        for job in jobs.values():
            if job.phase and job.phase not in seen:
                seen.append(job.phase)

    asked, ran = catalogue.get, service.runner.run

    def noticing_get(url: str, headers: dict[str, str]) -> bytes:
        phase_now()
        return asked(url, headers)

    def noticing_run(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        phase_now()
        return ran(argv, stdin=stdin, timeout=timeout)

    service.fetcher = type("Noticing", (), {"get": staticmethod(noticing_get)})()
    service.runner = type("Noticing", (), {"run": staticmethod(noticing_run)})()  # type: ignore[assignment]
    prepared(app, service, tape)
    assert seen == ["audio", "boundaries", "albums"]


def test_a_lookup_failure_does_not_stop_the_boundaries(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.down = {"recording"}
    app = build(service)
    tape = ready(app, service)
    job = prepared(app, service, tape)
    assert not job["error"]
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert all(s["start"] is not None for s in view["songs"])
    assert {s["proposal"]["status"] for s in view["songs"]} == {M.FAILED}
    assert view["cut_refusal"] == ""


def test_a_lookup_that_dies_is_said_after_the_boundaries_are_saved(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.broken = {"recording"}
    app = build(service)
    tape = ready(app, service)
    job = prepared(app, service, tape)
    assert "albums: the worker died" in job["error"]
    assert job["result"]["boundaries"] == ""
    assert all(s.start is not None for s in T.read(service.layout, tape).songs)


def test_a_boundary_failure_does_not_stop_the_lookup(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = ready(app, service)
    runner = service.runner
    real = runner.run

    def failing(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = tuple(str(a) for a in argv)
        if "astats" in " ".join(args):
            raise RuntimeError("the measure fell over")
        return real(argv, stdin=stdin, timeout=timeout)

    service.runner = type("Failing", (), {"run": staticmethod(failing)})()  # type: ignore[assignment]
    job = prepared(app, service, tape)
    assert "boundaries: the measure fell over" in job["error"]
    assert job["result"]["albums"] == ""
    songs = T.read(service.layout, tape).songs
    assert all(s.start is None for s in songs)
    assert all(s.proposal and s.proposal.status == M.MATCHED for s in songs)


def test_an_interrupted_job_run_again_does_only_what_is_left(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.broken = {'recording:"Two"'}
    app = build(service)
    tape = ready(app, service)
    assert "albums: the worker died" in prepared(app, service, tape)["error"]
    before = T.read(service.layout, tape)
    assert before.songs[0].proposal is not None and before.songs[1].proposal is None
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is False

    catalogue.broken = set()
    catalogue.calls.clear()
    runner = service.runner
    measures = len([c for c in runner.calls if "astats" in " ".join(c)])
    assert not prepared(app, service, tape)["error"]
    after = T.read(service.layout, tape)
    assert after.songs[1].proposal is not None
    assert [(s.start, s.end) for s in after.songs] == [
        (s.start, s.end) for s in before.songs
    ]
    assert len([c for c in runner.calls if "astats" in " ".join(c)]) == measures
    assert len(catalogue.calls) == 1
    assert 'recording:"Two"' in urllib.parse.unquote(catalogue.calls[0])


def test_a_second_run_with_nothing_owed_changes_nothing_and_asks_nothing(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = ready(app, service)
    assert not prepared(app, service, tape)["error"]
    current = T.read(service.layout, tape)
    assert current.songs[0].settled_by == M.CATALOGUE
    catalogue.calls.clear()
    assert not prepared(app, service, tape)["error"]
    assert catalogue.calls == []
    assert T.read(service.layout, tape) == current


def test_a_failed_lookup_is_not_run_again_by_itself_but_is_by_looking_again(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.down = {"recording"}
    app = build(service)
    tape = ready(app, service)
    prepared(app, service, tape)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["prepare_needed"] is False
    catalogue.down = set()
    assert post(app, f"/api/tapes/{tape}/match", service).status == 202
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert {s["proposal"]["status"] for s in view["songs"]} == {M.MATCHED}


def test_a_text_edit_owes_a_lookup_and_nothing_else(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = ready(app, service)
    prepared(app, service, tape)
    before = T.read(service.layout, tape)
    rows = [
        {"title": s.title, "artist": s.artist, "album": s.album, "length": s.length}
        for s in before.songs
    ]
    rows[1]["album"] = "Another Record"
    assert save_list(app, tape, service, rows).status == 200
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is True
    catalogue.calls.clear()
    assert not prepared(app, service, tape)["error"]
    assert len(catalogue.calls) == 1
    after = T.read(service.layout, tape)
    assert [(s.start, s.end) for s in after.songs] == [
        (s.start, s.end) for s in before.songs
    ]


def test_nothing_is_owed_before_both_are_in(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is False
    assert post(app, f"/api/tapes/{tape}/prepare", service).status == 409
    assert send_csv(app, tape, service, TWO).status == 200
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is False
    assert post(app, f"/api/tapes/{tape}/prepare", service).status == 409


def test_find_again_and_look_again_are_still_each_their_own_job(
    service: Service, catalogue: Catalogue
) -> None:
    app = build(service)
    tape = placed(app, service)
    catalogue.calls.clear()
    assert post(app, f"/api/tapes/{tape}/process", service).status == 202
    assert catalogue.calls == []
    assert not cuts_sent(service)
    assert post(app, f"/api/tapes/{tape}/match", service, {"again": True}).status == 202
    assert len(catalogue.calls) == 2


def test_a_failed_run_waits_for_a_person_even_after_a_restart(
    service: Service, catalogue: Catalogue
) -> None:
    """The page knew a run had failed only from a job a restart forgets, and started it
    again on the next look."""
    catalogue.broken = {"recording"}
    app = build(service)
    tape = ready(app, service)
    assert "albums: the worker died" in prepared(app, service, tape)["error"]
    saved = T.read(service.layout, tape)
    assert "the worker died" in saved.prepare_failed
    restarted = build(a_service_as(service))
    view = get(restarted, f"/api/tapes/{tape}", service).json()
    assert view["job"] is None
    assert view["prepare_needed"] is False
    assert "the worker died" in view["prepare_failed"]


def test_what_a_person_presses_after_a_failed_run_clears_it(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.broken = {"recording"}
    app = build(service)
    tape = ready(app, service)
    prepared(app, service, tape)
    catalogue.broken = set()
    assert post(app, f"/api/tapes/{tape}/match", service, {}).status == 202
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["prepare_failed"] == ""


def test_an_edit_after_a_failed_run_lets_the_tape_go_on_by_itself(
    service: Service, catalogue: Catalogue
) -> None:
    catalogue.broken = {"recording"}
    app = build(service)
    tape = ready(app, service)
    prepared(app, service, tape)
    current = T.read(service.layout, tape)
    assert current.prepare_failed
    rows = [
        {"title": s.title, "artist": s.artist, "album": s.album, "length": s.length}
        for s in current.songs
    ]
    rows[0]["title"] = "One Again"
    assert save_list(app, tape, service, rows).status == 200
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is True
