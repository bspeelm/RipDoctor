"""A tape's playlist and its archive step over the wire, by what beets is asked,
the file left in the library and what is still on disk. ADR-063."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner, Result
from ripdoctor.config.settings import Settings
from ripdoctor.core import filing as F
from ripdoctor.core import playlist as P
from ripdoctor.core import tape as TP
from ripdoctor.store import playlists as S
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.routes import tapes as RT
from ripdoctor.web.service import Service
from ripdoctor.work import tape_finish as TX
from tests.test_web_library import ready_to_import
from tests.test_web_precut import MATCH, MBID, Moving, a_precut_service, an_album
from tests.test_web_precut import upload as upload_album
from tests.test_web_routes import get, post
from tests.test_web_tape_cut import Cutting, job_of
from tests.test_web_tape_file import Beet, cut_tape, file_it, pinned
from tests.test_web_tapes import a_service, a_tape_runner

FORMAT = "\x1f".join(
    f"${k}"
    for k in (
        "mb_trackid",
        "mb_albumid",
        "title",
        "artist",
        "album",
        "length",
        "path",
    )
)


class Library(Beet):
    """Beets answering the one query a playlist asks, comma for OR as it reads it."""

    def __init__(self, inner: Cutting, library: Path) -> None:
        super().__init__(inner, library)
        self.asked: list[tuple[str, ...]] = []
        self.at_read_back: list[tuple[bool, set[str]]] = []
        self.audio: Path | None = None
        self.cuts: Path | None = None

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if argv[0] == "flac" and self.audio is not None and "part-" in str(argv[-1]):
            cuts = {p.name for p in self.cuts.iterdir()} if self.cuts else set()
            self.at_read_back.append((self.audio.is_file(), cuts))
        return super().run(argv, stdin=stdin, timeout=timeout)

    def _ls(self, args: tuple[str, ...]) -> Result:
        said = args[args.index("-f") + 1]
        if not said.startswith("$mb_trackid"):
            return super()._ls(args)
        assert said == FORMAT
        self.asked.append(args)
        if self.ls_fails:
            return Result(args, 1, b"", b"database is locked\n")
        query = list(args[args.index("-f") + 2 :])
        assert query[1::2] == [","] * (len(query) // 2)
        terms = [t.split(":", 1) for t in query[0::2]]
        keys = ("mb_trackid", "mb_albumid", "title", "artist", "album", "length")
        lines = [
            "\x1f".join([*(r.get(k, "") for k in keys), r["path"]])
            for r in self.rows
            if any(v.casefold() in r.get(k, "").casefold() for k, v in terms)
        ]
        return Result(args, 0, "\n".join(lines).encode(), b"")

    def moves(self, old: str, new: Path) -> None:
        """An album replaced: the file and its row are somewhere else."""
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old, new)
        for row in self.rows:
            if row["path"] == old:
                row["path"] = str(new)

    def loses_file(self, path: str) -> None:
        Path(path).unlink()

    def forgets(self, path: str) -> None:
        Path(path).unlink()
        self.rows = [r for r in self.rows if r["path"] != path]


@pytest.fixture
def service(tmp_path: Path) -> Service:
    beet = Library(Cutting(a_tape_runner()), tmp_path / "lib")
    made = a_service(tmp_path, runner=beet)  # type: ignore[arg-type]
    made.settings = Settings(library=str(tmp_path / "lib"), importer="beets")
    made.state_dir = str(tmp_path / "state")
    return made


def beet_of(service: Service) -> Library:
    return service.runner  # type: ignore[return-value]


def state_of(service: Service, tape: str) -> P.State:
    return S.read(service.layout, tape)


def file_of(service: Service, tape: str) -> Path:
    return S.path_of(service.settings.library, state_of(service, tape).file)


def lines_of(service: Service, tape: str) -> list[str]:
    return file_of(service, tape).read_text().splitlines()


def view(app, tape: str, service: Service) -> dict:  # type: ignore[no-untyped-def]
    return get(app, f"/api/tapes/{tape}", service).json()


def rebuild(app, tape: str, service: Service) -> dict:  # type: ignore[no-untyped-def]
    answer = post(app, f"/api/tapes/{tape}/playlist", service)
    assert answer.status == 202, answer.json()
    return job_of(app, tape, service)


def filed(app, service: Service) -> str:  # type: ignore[no-untyped-def]
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    return tape


def path_of_song(service: Service, tape: str, number: int) -> str:
    return T.read(service.layout, tape).songs[number - 1].path


# ------------------------------------------------------------------ after filing


def test_filing_writes_the_playlist_in_list_order_from_where_each_song_went(
    service: Service,
) -> None:
    tape = filed(build(service), service)
    assert file_of(service, tape).parent == Path(service.settings.library) / "Playlists"
    assert lines_of(service, tape) == [
        "#EXTM3U",
        "#PLAYLIST:Mix: Vol. 1",
        "../rel-1/01 One.flac",
        "../rel-2/02 Two.flac",
    ]
    done = state_of(service, tape)
    assert (done.outcome, done.songs, done.left_out) == (P.WRITTEN, 2, ())


def test_a_song_that_failed_filing_is_named_and_the_rest_are_still_written(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).fails["rel-2"] = b"error: the disk said no\n"
    assert file_it(app, tape, service)["error"]
    assert lines_of(service, tape)[2:] == ["../rel-1/01 One.flac"]
    gone = state_of(service, tape).left_out
    assert [(g.number, g.state) for g in gone] == [(2, P.NOT_FILED)]
    assert "the disk said no" in gone[0].why
    shown = view(app, tape, service)["playlist"]
    assert shown["left_out"][0]["number"] == 2 and shown["songs"] == 1


def test_a_skipped_song_has_no_line_and_is_named_as_skipped(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    post(app, f"/api/tapes/{tape}/songs/2/skip", service)
    assert not file_it(app, tape, service)["error"]
    assert lines_of(service, tape)[2:] == ["../rel-1/01 One.flac"]
    assert [g.state for g in state_of(service, tape).left_out] == [TP.SKIPPED]


def test_a_recorded_path_is_checked_on_disk_and_not_believed(service: Service) -> None:
    app = build(service)
    tape = filed(app, service)
    Path(path_of_song(service, tape, 1)).unlink()
    TX.after_filing(
        RT._place(service), RT.IMP.Beets(), service.jobs, tape, service.now()
    )
    assert lines_of(service, tape)[2:] == ["../rel-2/02 Two.flac"]
    assert state_of(service, tape).left_out[0].state == P.MISSING


def test_nothing_resolving_writes_no_file_rather_than_an_empty_playlist(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    for release in ("rel-1", "rel-2"):
        beet_of(service).fails[release] = b"error: no\n"
    assert file_it(app, tape, service)["error"]
    done = state_of(service, tape)
    assert done.file == "" and done.outcome == P.NOTHING
    assert len(done.left_out) == 2
    assert not (Path(service.settings.library) / "Playlists").exists()


def test_filing_one_tape_brings_another_tapes_playlist_up_to_date(
    service: Service,
) -> None:
    app = build(service)
    first = filed(app, service)
    old = path_of_song(service, first, 1)
    beet_of(service).moves(old, Path(service.settings.library) / "moved" / "1.flac")
    second = filed(app, service)
    assert second != first
    assert lines_of(service, first)[2] == "../moved/1.flac"
    assert lines_of(service, second)[2] == "../moved/1.flac"
    told = job_of(app, second, service)["result"]["notes"]
    assert told == ["Mix: Vol. 1: playlist rewritten, 2 songs"]


# ----------------------------------------------------------------------- rebuild


def test_rebuild_after_an_album_is_replaced_follows_it_to_its_new_location(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    new = Path(service.settings.library) / "A Band" / "Reissue" / "01 One.flac"
    beet_of(service).moves(path_of_song(service, tape, 1), new)
    assert not rebuild(app, tape, service)["error"]
    assert lines_of(service, tape)[2:] == [
        "../A Band/Reissue/01 One.flac",
        "../rel-2/02 Two.flac",
    ]
    assert path_of_song(service, tape, 1) == str(new)
    assert state_of(service, tape).outcome == P.WRITTEN


def test_one_query_answers_for_the_whole_tape(service: Service) -> None:
    app = build(service)
    tape = filed(app, service)
    rebuild(app, tape, service)
    asked = beet_of(service).asked
    override = str(Path(service.state_dir) / "beets-override.yaml")
    assert asked == [
        (
            "beet",
            "--config",
            override,
            "ls",
            "-f",
            FORMAT,
            "mb_trackid:rec-One",
            ",",
            "mb_trackid:rec-Two",
        )
    ]


def test_rebuild_with_nothing_changed_leaves_the_file_alone(service: Service) -> None:
    app = build(service)
    tape = filed(app, service)
    target = file_of(service, tape)
    os.utime(target, ns=(1, 1))
    assert not rebuild(app, tape, service)["error"]
    assert target.stat().st_mtime_ns == 1
    assert state_of(service, tape).outcome == P.UNCHANGED


def test_a_song_gone_from_the_library_is_named_and_never_silently_dropped(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    beet_of(service).forgets(path_of_song(service, tape, 2))
    rebuild(app, tape, service)
    assert lines_of(service, tape)[2:] == ["../rel-1/01 One.flac"]
    gone = view(app, tape, service)["playlist"]["left_out"]
    assert [(g["number"], g["state"]) for g in gone] == [(2, P.MISSING)]
    assert "no row" in gone[0]["why"]


def test_a_row_whose_file_went_is_named_with_the_row(service: Service) -> None:
    app = build(service)
    tape = filed(app, service)
    path = path_of_song(service, tape, 2)
    beet_of(service).loses_file(path)
    rebuild(app, tape, service)
    assert path in state_of(service, tape).left_out[0].why


def test_two_rows_for_one_song_are_named_and_neither_is_chosen(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    runner = beet_of(service)
    runner.moves(
        path_of_song(service, tape, 2),
        Path(service.settings.library) / "A" / "2.flac",
    )
    other = runner.owns("Two", mb_trackid="rec-Two", mb_albumid="other")
    for row in runner.rows:
        if row["title"] == "Two" and "other" not in row.get("mb_albumid", ""):
            row["mb_albumid"] = "another"
    rebuild(app, tape, service)
    gone = state_of(service, tape).left_out
    assert [(g.number, g.state) for g in gone] == [(2, P.AMBIGUOUS)]
    assert str(other) in gone[0].why
    assert lines_of(service, tape)[2:] == ["../rel-1/01 One.flac"]


def test_a_song_with_no_id_is_found_by_title_artist_and_album(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    assert not file_it(app, tape, service)["error"]
    runner = beet_of(service)
    old = path_of_song(service, tape, 1)
    new = Path(service.settings.library) / "Elsewhere" / "1.flac"
    runner.moves(old, new)
    rebuild(app, tape, service)
    assert lines_of(service, tape)[2] == "../Elsewhere/1.flac"
    assert runner.asked[-1][-1] == "artist:A Band"


def test_a_beets_failure_is_unknown_never_missing_and_leaves_the_file_alone(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    target = file_of(service, tape)
    before = target.read_bytes()
    os.utime(target, ns=(1, 1))
    beet_of(service).ls_fails = True
    assert not rebuild(app, tape, service)["error"]
    done = state_of(service, tape)
    assert done.outcome == P.UNKNOWN and "database is locked" in done.note
    assert done.left_out == ()
    assert target.read_bytes() == before and target.stat().st_mtime_ns == 1


def test_every_file_gone_at_once_leaves_the_playlist_as_it_was(
    service: Service,
) -> None:
    """What a library that has dropped off its mount looks like from here."""
    app = build(service)
    tape = filed(app, service)
    before = file_of(service, tape).read_bytes()
    for number in (1, 2):
        beet_of(service).loses_file(path_of_song(service, tape, number))
    rebuild(app, tape, service)
    done = state_of(service, tape)
    assert done.outcome == P.NOTHING and len(done.left_out) == 2
    assert file_of(service, tape).read_bytes() == before


def test_a_row_outside_the_library_is_named_and_gets_no_line(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = filed(app, service)
    outside = tmp_path / "elsewhere" / "1.flac"
    beet_of(service).moves(path_of_song(service, tape, 1), outside)
    rebuild(app, tape, service)
    assert lines_of(service, tape)[2:] == ["../rel-2/02 Two.flac"]
    gone = state_of(service, tape).left_out[0]
    assert gone.state == P.OUTSIDE and str(outside) in gone.why


def test_a_library_reached_through_a_symlink_still_gets_relative_lines(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    real = Path(service.settings.library)
    real.mkdir(parents=True, exist_ok=True)
    link = tmp_path / "linked"
    link.symlink_to(real)
    service.settings = replace(service.settings, library=str(link))
    file_it(app, tape, service)
    assert lines_of(service, tape)[2:] == [
        "../rel-1/01 One.flac",
        "../rel-2/02 Two.flac",
    ]


def test_a_file_that_is_someone_elses_is_kept_and_the_failure_is_said(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    target = file_of(service, tape)
    target.write_text("#EXTM3U\n#PLAYLIST:Someone Else\n../a.flac\n")
    rebuild(app, tape, service)
    done = state_of(service, tape)
    assert done.outcome == P.FAILED and "Someone Else" not in done.note
    assert "is not the playlist" in done.note
    assert "Someone Else" in target.read_text()


def test_a_busy_tape_is_said_and_one_tapes_fault_does_not_stop_the_next(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    name = T.read(service.layout, tape).name
    place = RT._place(service)
    with service.jobs.claim(T.key_of(tape), "something else"):
        told = TX.rebuild_all(place, RT.IMP.Beets(), service.jobs, 3.0)
    assert told == [f"{name}: busy, so its playlist was not rebuilt"]

    other = T.create(service.layout, "Other", 9.0)
    S.save(service.layout, other.id, P.State(file="x.m3u8"))
    (T.dir_for(service.layout, tape) / S.STATE).write_text("{not json")
    told = TX.rebuild_all(place, RT.IMP.Beets(), service.jobs, 3.0)
    assert any(
        line.startswith(f"{name}: its playlist was not rebuilt") for line in told
    )
    assert any(line.startswith("Other: ") for line in told)


def test_a_tape_with_no_playlist_is_not_asked_about(service: Service) -> None:
    T.create(service.layout, "Never Written", 1.0)
    told = TX.rebuild_all(RT._place(service), RT.IMP.Beets(), service.jobs, 3.0)
    assert told == [] and beet_of(service).asked == []


# -------------------------------------------------------------------- the album flow


class Taking(FakeRunner):
    """A beets that takes what review holds, as `move: yes` does."""

    def __init__(self, review: Path, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.review = review

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "import" in argv:
            for track in self.review.glob("*.flac"):
                track.unlink()
        return super().run(argv, stdin=stdin, timeout=timeout)


def an_album_service(tmp_path: Path) -> tuple[Service, Taking]:
    service, library = ready_to_import(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    service.state_dir = str(tmp_path / "state")
    filed_at = library / "A Band" / "A Record" / "01 One.flac"
    filed_at.parent.mkdir(parents=True)
    filed_at.write_bytes(b"a")
    runner = Taking(service.layout.review_dir("album"), installed={"beet"})
    runner.expect("ls", stdout=f"A Record\x1f{filed_at}\n".encode())
    service.runner = runner
    return service, runner


def a_tape_with_a_playlist(service: Service, path: str = "") -> TP.Tape:
    tape = T.create(service.layout, "Mix", 1.0)
    song = TP.Song(1, "One", "A Band", "A Record", 5.0, filing=F.FILED, path=path)
    tape = replace(tape, songs=(song,))
    T.save(service.layout, tape)
    S.save(service.layout, tape.id, P.State(file=P.file_of(tape), songs=1))
    return tape


def import_album(service: Service) -> dict:
    answer = post(build(service), "/api/import/album", service, {"mbid": "m"})
    assert answer.status == 202, answer.json()
    return get(build(service), "/api/job/album", service).json()


def test_an_album_import_with_no_tapes_sends_beets_nothing_new(tmp_path: Path) -> None:
    service, runner = an_album_service(tmp_path)
    done = import_album(service)
    assert not done["error"]
    ls = [c for c in runner.calls if "ls" in c]
    assert ls and all(c[c.index("-f") + 1] == "$album\x1f$path" for c in ls)
    assert not [n for n in done["result"]["notes"] if "playlist" in n]


def test_a_finished_album_import_rebuilds_a_tapes_playlist_and_notes_it(
    tmp_path: Path,
) -> None:
    service, runner = an_album_service(tmp_path)
    tape = a_tape_with_a_playlist(service, "/gone.flac")
    library = Path(service.settings.library)
    moved = library / "new place" / "1.flac"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"a")
    row = "\x1f".join(["", "", "One", "A Band", "A Record", "0:05", str(moved)])
    runner.replies.insert(
        0, (lambda a: "$mb_trackid" in " ".join(a), Result((), 0, row.encode(), b""))
    )
    done = import_album(service)
    assert not done["error"] and done["result"]["tracks"] == 1
    assert "Mix: playlist rewritten, 1 songs" in done["result"]["notes"]
    assert (
        (S.path_of(str(library), P.file_of(tape)))
        .read_text()
        .endswith("../new place/1.flac\n")
    )


def test_an_album_import_still_succeeds_when_the_rebuild_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _runner = an_album_service(tmp_path)
    a_tape_with_a_playlist(service)

    def breaks(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("the rebuild fell over")

    monkeypatch.setattr(TX, "rebuild_all", breaks)
    done = import_album(service)
    assert not done["error"]
    assert done["result"]["tracks"] == 1 and done["result"]["importer"] == "beets"
    assert (
        "playlists were not rebuilt: the rebuild fell over" in done["result"]["notes"]
    )


def test_a_failed_album_import_rebuilds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _library = ready_to_import(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    service.state_dir = str(tmp_path / "state")
    service.runner = FakeRunner(installed={"beet"})
    a_tape_with_a_playlist(service)
    called: list[int] = []
    monkeypatch.setattr(TX, "rebuild_all", lambda *a, **k: called.append(1) or [])
    assert "still in review" in import_album(service)["error"]
    assert called == []


def test_a_precut_album_still_succeeds_when_the_rebuild_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib" / "A Band" / "A Record"
    service = a_precut_service(tmp_path)
    staged = service.layout.work / "precut"
    service.runner = (
        Moving(staged, installed={"beet"})
        .expect(lambda a: "import" in a, stdout=MATCH)
        .expect("ls", stdout=f"A Record\x1f{lib}/01 One.flac\n".encode())
    )
    upload_album(service, an_album())
    lib.mkdir(parents=True)
    (lib / "01 One.flac").write_bytes(b"a")
    a_tape_with_a_playlist(service)

    def breaks(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("the rebuild fell over")

    monkeypatch.setattr(TX, "rebuild_all", breaks)
    body = post(build(service), "/api/precut/take", service, {"mbid": MBID}).json()
    assert not body["error"], body["error"]
    assert (
        "playlists were not rebuilt: the rebuild fell over" in body["result"]["notes"]
    )
    assert not staged.exists()


def test_with_the_fallback_tagger_a_written_playlist_is_said_not_to_be_rebuilt(
    tmp_path: Path,
) -> None:
    service, _library = ready_to_import(tmp_path)
    a_tape_with_a_playlist(service)
    assert RT.after_import(service) == ["playlists were not rebuilt: that takes beets"]


def test_the_page_shows_the_path_the_count_and_the_last_outcome(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    shown = view(app, tape, service)["playlist"]
    assert shown["path"] == str(file_of(service, tape))
    assert shown["songs"] == 2 and shown["outcome"] == P.WRITTEN
    assert shown["at"] == service.now()
