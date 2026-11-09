"""A tape archives in the job that resolves it, and only then. ADR-065."""

from __future__ import annotations

import pytest

from ripdoctor.store import archive as AR
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from tests.test_web_tape_file import file_it, pinned
from tests.test_web_tape_playlist import (
    archive,
    archived_at,
    beet_of,
    rebuild,
    service,  # noqa: F401 - a fixture
    state_of,
    untouched,
    view,
)


def resolving(app, service: Service) -> str:  # type: ignore[no-untyped-def]  # noqa: F811
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.audio = T.audio_file(service.layout, tape)
    runner.cuts = T.dir_for(service.layout, tape) / T.CUTS
    return tape


def test_filing_the_last_song_archives_in_that_job_and_says_both(
    service: Service,  # noqa: F811
) -> None:
    app = build(service)
    tape = resolving(app, service)
    job = file_it(app, tape, service)
    assert not job["error"], job["error"]
    assert job["result"]["archive"]["archived"]
    assert archived_at(service, tape).is_dir()
    assert not T.audio_file(service.layout, tape).is_file()
    assert view(app, tape, service)["playlist"]["done"]


def test_a_song_not_resolved_holds_it_up_and_is_named(
    service: Service,  # noqa: F811
) -> None:
    app = build(service)
    tape = resolving(app, service)
    beet_of(service).fails["rel-2"] = b"error: the disk said no\n"
    file_it(app, tape, service)
    assert "song 2" in view(app, tape, service)["playlist"]["archive_refusal"]
    untouched(service, tape)
    assert not archived_at(service, tape).exists()


def test_a_failed_copy_leaves_every_song_filed_and_can_be_pressed_again(
    service: Service,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build(service)
    tape = resolving(app, service)
    real = AR.keep_tape

    def full(*_a: object, **_k: object) -> dict[str, object]:
        raise OSError("the archive cannot be written")

    monkeypatch.setattr(AR, "keep_tape", full)
    job = file_it(app, tape, service)
    assert not job["error"], job["error"]
    finished = job["result"]["archive"]
    assert not finished["archived"] and "cannot be written" in finished["why"]
    untouched(service, tape)
    shown = view(app, tape, service)
    assert (
        shown["all_filed"]
        and "cannot be written" in shown["playlist"]["archive_failed"]
    )
    assert not shown["playlist"]["archive_refusal"]

    monkeypatch.setattr(AR, "keep_tape", real)
    assert archive(app, tape, service).status == 202
    assert view(app, tape, service)["playlist"]["done"]
    assert not state_of(service, tape).archive_failed


def test_a_playlist_written_by_a_press_archives_a_tape_already_resolved(
    service: Service,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The way a tape filed before this reaches the archive."""
    app = build(service)
    tape = resolving(app, service)
    real = AR.keep_tape

    def full(*_a: object, **_k: object) -> dict[str, object]:
        raise OSError("the archive cannot be written")

    monkeypatch.setattr(AR, "keep_tape", full)
    file_it(app, tape, service)
    monkeypatch.setattr(AR, "keep_tape", real)
    job = rebuild(app, tape, service)
    assert job["result"]["archive"]["archived"], job
