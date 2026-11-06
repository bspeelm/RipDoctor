"""One import into the library at a time, across tapes and records. ADR-063."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.config.settings import Settings
from ripdoctor.core import filing as F
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from ripdoctor.work import jobs as J
from tests.test_web_library import I_SEP, ready_to_import
from tests.test_web_precut import MBID, a_precut_service, an_album, upload
from tests.test_web_routes import a_service, post
from tests.test_web_tape_cut import Cutting
from tests.test_web_tape_file import Beet, file_it, pinned, songs
from tests.test_web_tapes import a_tape_runner


class Noticing:
    """Records whether the lock was held at each command that matters."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.held: list[tuple[str, bool]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        joined = " ".join(args)
        if args[0] == "beet" and "import" in args and "preview" not in joined:
            self.held.append(("import", J._IMPORTS.locked()))
        if args[0] == "beet" and "ls" in args and "album::" in joined:
            self.held.append(("read", J._IMPORTS.locked()))
        return self.inner.run(argv, stdin=stdin, timeout=timeout)  # type: ignore[attr-defined]


@pytest.fixture
def service(tmp_path: Path) -> Service:
    beet = Beet(Cutting(a_tape_runner()), tmp_path / "lib")
    made = a_service(tmp_path, runner=beet)  # type: ignore[arg-type]
    made.settings = Settings(library=str(tmp_path / "lib"), importer="beets")
    made.state_dir = str(tmp_path / "state")
    return made


def test_the_read_and_the_import_of_a_tape_both_hold_the_lock(service: Service) -> None:
    """Another job filing between the two left a run answering for a library that had
    changed under it."""
    app = build(service)
    tape = pinned(app, service)
    noticing = Noticing(service.runner)
    service.runner = noticing  # type: ignore[assignment]
    assert not file_it(app, tape, service)["error"]
    assert noticing.held[:2] == [("read", True), ("import", True)]
    assert all(held for _, held in noticing.held)
    assert not J._IMPORTS.locked()


def test_a_tape_run_waits_for_a_held_lock_says_so_and_then_files(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    service.jobs = J.Jobs(now=service.now)
    with J._IMPORTS:
        assert post(app, f"/api/tapes/{tape}/file", service).status == 202
        job = next(iter(service.jobs.running.values()))
        for _ in range(500):
            if "waiting for another import" in job.detail:
                break
            time.sleep(0.01)
        assert "waiting for another import" in job.detail
        assert not service.runner.imports  # type: ignore[attr-defined]
    for _ in range(500):
        if job.done:
            break
        time.sleep(0.01)
    assert job.done and not job.error
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_lock_held_too_long_fails_the_song_plainly_with_nothing_imported(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(J, "IMPORT_WAIT", 0.01)
    app = build(service)
    tape = pinned(app, service)
    with J._IMPORTS:
        job = file_it(app, tape, service)
    assert "did not file" in job["error"]
    assert not service.runner.imports  # type: ignore[attr-defined]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert "another import has held the library" in one.filing_note
    assert one.import_begun is False


def test_a_lock_is_given_back_when_an_import_raises() -> None:
    with pytest.raises(RuntimeError), J.one_import():
        raise RuntimeError("beets fell over")
    assert not J._IMPORTS.locked()


def test_an_import_waits_its_turn_without_a_job_to_tell() -> None:
    order: list[str] = []

    def second() -> None:
        with J.one_import():
            order.append("second")

    other = threading.Thread(target=second)
    with J.one_import():
        other.start()
        time.sleep(0.05)
        order.append("first")
    other.join(5)
    assert order == ["first", "second"]


def test_a_records_import_holds_the_same_lock(tmp_path: Path) -> None:
    service, _library = ready_to_import(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    rows = f"A Record{I_SEP}/music/A Band/A Record/01 One.flac"
    inner = FakeRunner(installed={"beet"}).expect("ls", stdout=rows.encode())
    noticing = Noticing(inner)
    service.runner = noticing  # type: ignore[assignment]
    post(build(service), "/api/import/album", service, {"mbid": "chosen"})
    assert ("import", True) in noticing.held
    assert not J._IMPORTS.locked()


def test_a_precut_albums_import_holds_the_same_lock(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    assert not upload(service, an_album()).json()["error"]
    noticing = Noticing(service.runner)
    service.runner = noticing  # type: ignore[assignment]
    post(build(service), "/api/precut/take", service, {"mbid": MBID})
    assert ("import", True) in noticing.held
