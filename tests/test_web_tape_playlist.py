"""A tape's playlist and its archive step over the wire, by what beets is asked,
the file left in the library and what is still on disk. ADR-063."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner, Result, TimedOut
from ripdoctor.config.settings import Settings
from ripdoctor.core import filing as F
from ripdoctor.core import playlist as P
from ripdoctor.core import tape as TP
from ripdoctor.store import archive as AR
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
from tests.test_web_tape_cut import TWO, Cutting, cut, job_of, placed, settle_all
from tests.test_web_tape_file import Beet, cut_tape, file_it, pinned
from tests.test_web_tapes import a_service, a_tape_runner

KEYS = (
    "title",
    "artist",
    "album",
    "length",
    "path",
    "mb_trackid",
    "track",
    "mb_albumid",
)
FORMAT = "\x1f".join(f"${k}" for k in KEYS)


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
        if not said.endswith("$mb_albumid"):
            return super()._ls(args)
        assert said == FORMAT
        self.asked.append(args)
        if self.ls_fails:
            return Result(args, 1, b"", b"database is locked\n")
        query = list(args[args.index("-f") + 2 :])
        assert query[1::2] == [","] * (len(query) // 2)
        terms = [t.split(":", 1) for t in query[0::2]]
        lines = [
            "\x1f".join(r.get(k, "") for k in KEYS)
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


def songs_of(service: Service, tape: str) -> tuple[TP.Song, ...]:
    return T.read(service.layout, tape).songs


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


def corrupt(service: Service, tape: str, text: bytes) -> None:
    (T.dir_for(service.layout, tape) / S.STATE).write_bytes(text)


UNREADABLE_STATES = [
    b'{"file": "mix.m3u8", "left',
    b"\xff\xfe not text",
    b"[]",
    b"{}",
    b'{"left_out": [], "file": 5}',
    b'{"left_out": [], "file": "../../x.m3u8"}',
    b'{"left_out": [{"nonsense": 1}]}',
    b'{"left_out": [], "unheard_of": 1}',
]


@pytest.mark.parametrize("text", UNREADABLE_STATES)
def test_a_corrupt_playlist_memory_is_a_plain_message_on_the_tape_s_view(
    service: Service, text: bytes
) -> None:
    app = build(service)
    tape = filed(app, service)
    corrupt(service, tape, text)
    answer = get(app, f"/api/tapes/{tape}", service)
    assert answer.status == 200
    assert answer.json()["playlist"]["note"] == S.UNREADABLE
    assert answer.json()["playlist"]["archive_refusal"]
    assert get(app, "/api/tapes", service).status == 200


def test_a_rebuild_replaces_a_corrupt_playlist_memory_and_finds_the_same_file(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    before = lines_of(service, tape)
    where = file_of(service, tape)
    corrupt(service, tape, b"{ not json")
    assert not rebuild(app, tape, service)["error"]
    remembered = state_of(service, tape)
    assert remembered.file == where.name and remembered.note == ""
    assert lines_of(service, tape) == before
    assert view(app, tape, service)["playlist"]["note"] == ""


def test_another_tape_s_corrupt_playlist_memory_is_noted_when_filing_moves_on(
    service: Service,
) -> None:
    app = build(service)
    first = filed(app, service)
    corrupt(service, first, b"{ not json")
    second = filed(app, service)
    told = job_of(app, second, service)["result"]["notes"]
    assert told == [f"Mix: Vol. 1: its playlist was not rebuilt: {S.UNREADABLE}"]


def test_a_failed_filing_job_still_returns_its_notes_about_other_tapes(
    service: Service,
) -> None:
    app = build(service)
    first = filed(app, service)
    old = path_of_song(service, first, 1)
    beet_of(service).moves(old, Path(service.settings.library) / "moved" / "1.flac")
    gone = path_of_song(service, first, 2)
    beet_of(service).loses_file(gone)
    second = pinned(app, service)
    done = file_it(app, second, service)
    assert "did not file" in done["error"]
    assert "Mix: Vol. 1: playlist rewritten, 1 songs" in done["result"]["notes"]
    assert view(app, second, service)["job"]["result"] == done["result"]


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


def test_the_same_title_twice_on_a_from_list_album_gets_two_rows_and_two_lines(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service, TWO.replace("Two,A Band", "One,A Band"))
    settle_all(app, tape, service)
    assert cut(app, tape, service).status == 202
    assert not file_it(app, tape, service)["error"]
    assert [s.path.rsplit("/", 1)[1] for s in songs_of(service, tape)] == [
        "01 One.flac",
        "02 One.flac",
    ]
    wanted = ["../from-list/01 One.flac", "../from-list/02 One.flac"]
    assert lines_of(service, tape)[2:] == wanted
    assert not rebuild(app, tape, service)["error"]
    assert lines_of(service, tape)[2:] == wanted
    assert state_of(service, tape).left_out == ()


def test_the_same_title_twice_is_told_apart_when_a_rebuild_follows_it_to_a_new_album(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service, TWO.replace("Two,A Band", "One,A Band"))
    settle_all(app, tape, service)
    assert cut(app, tape, service).status == 202
    assert not file_it(app, tape, service)["error"]
    library = Path(service.settings.library)
    runner = beet_of(service)
    for number in (1, 2):
        old = path_of_song(service, tape, number)
        runner.moves(old, library / "moved" / f"{3 - number}.flac")
    assert not rebuild(app, tape, service)["error"]
    assert lines_of(service, tape)[2:] == ["../moved/2.flac", "../moved/1.flac"]


def test_a_song_owned_on_another_album_keeps_its_line_through_a_rebuild(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    runner = beet_of(service)
    where = runner.owns("One", album="Greatest Hits")
    assert not file_it(app, tape, service)["error"]
    assert songs_of(service, tape)[0].filing == F.ALREADY
    before = lines_of(service, tape)
    assert not rebuild(app, tape, service)["error"]
    assert lines_of(service, tape) == before
    assert path_of_song(service, tape, 1) == str(where)
    assert state_of(service, tape).left_out == ()
    assert archive(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]


def test_a_copy_a_person_chose_answers_its_playlist_through_a_rebuild(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    where = beet_of(service).owns("One", length="0:06")
    file_it(app, tape, service)
    assert (
        post(
            app, f"/api/tapes/{tape}/songs/1/use-copy", service, {"path": str(where)}
        ).status
        == 200
    )
    assert post(app, f"/api/tapes/{tape}/playlist", service).status == 202
    assert not job_of(app, tape, service)["error"]
    state = state_of(service, tape)
    assert (state.songs, state.left_out) == (2, ())
    assert T.read(service.layout, tape).songs[0].path == str(where)


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


def tapes_with_playlists(service: Service, *names: str) -> None:
    for at, name in enumerate(names):
        tape = T.create(service.layout, name, float(at))
        song = TP.Song(1, "One", "A Band", "A Record", 5.0)
        T.save(service.layout, replace(tape, songs=(song,)))
        S.save(service.layout, tape.id, P.State(file=P.file_of(tape)))


def beet_that_hangs(service: Service) -> list[float]:
    """Every wait it was given, each one running out."""
    runner = beet_of(service)
    waits: list[float] = []
    original = runner.run

    def hangs(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "ls" in argv:
            waits.append(timeout)
            raise TimedOut(argv, timeout or 0.0)
        return original(argv, stdin=stdin, timeout=timeout)

    runner.run = hangs  # type: ignore[method-assign]
    return waits


def test_a_hung_beets_costs_an_import_one_short_wait_and_the_rest_are_named(
    service: Service,
) -> None:
    tapes_with_playlists(service, "First", "Second", "Third", "Fourth")
    waits = beet_that_hangs(service)
    told = RT.after_import(service)
    assert waits == [TX.ONE_QUERY]
    assert told[0].startswith("First: beets could not answer")
    assert "not checked" in told[1]
    assert all(name in told[1] for name in ("Second", "Third", "Fourth"))
    assert "First" not in told[1]


def test_an_import_stops_asking_at_the_limit_and_names_the_rest(
    service: Service,
) -> None:
    tapes_with_playlists(service, "First", "Second", "Third")
    ticks = iter((0.0, 1.0, TX.ALL_QUERIES + 1.0))
    told = TX.rebuild_all(
        RT._place(service),
        RT.IMP.Beets(),
        service.jobs,
        3.0,
        clock=lambda: next(ticks),
    )
    assert len(beet_of(service).asked) == 1
    assert "not checked" in told[-1]
    assert "Second" in told[-1] and "Third" in told[-1] and "First" not in told[-1]


def test_the_last_of_the_time_is_all_a_query_is_given(service: Service) -> None:
    tapes_with_playlists(service, "First")
    waits = beet_that_hangs(service)
    ticks = iter((0.0, TX.ALL_QUERIES - 2.0))
    TX.rebuild_all(
        RT._place(service),
        RT.IMP.Beets(),
        service.jobs,
        3.0,
        clock=lambda: next(ticks),
    )
    assert waits == [2.0]


def test_a_rebuild_asked_for_on_a_page_has_the_long_wait(service: Service) -> None:
    tapes_with_playlists(service, "First")
    waits = beet_that_hangs(service)
    only = T.listed(service.layout).tapes[0].id
    state = TX.rebuild(RT._place(service), RT.IMP.Beets(), only, 3.0)
    assert waits == [300.0] and state.outcome == P.UNKNOWN


def test_the_hook_catches_what_it_owns_and_never_the_machine_going_away(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(_layout: object) -> bool:
        raise SystemExit(1)

    monkeypatch.setattr(TX, "any_known", gone)
    with pytest.raises(SystemExit):
        RT.after_import(service)


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
    row = "\x1f".join(["One", "A Band", "A Record", "0:05", str(moved), "", "", ""])
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


# ----------------------------------------------------------------- the archive gate


def archive(app, tape: str, service: Service):  # type: ignore[no-untyped-def]
    return post(app, f"/api/tapes/{tape}/archive", service)


def ready_to_archive(app, service: Service) -> str:  # type: ignore[no-untyped-def]
    tape = filed(app, service)
    runner = beet_of(service)
    runner.audio = T.audio_file(service.layout, tape)
    runner.cuts = T.dir_for(service.layout, tape) / T.CUTS
    return tape


def archived_at(service: Service, tape: str) -> Path:
    return service.layout.archive / f"_tape-{tape}"


def untouched(service: Service, tape: str) -> None:
    assert T.audio_file(service.layout, tape).is_file()
    assert len(T.cuts_on_disk(service.layout, tape)) == 2


def test_archive_is_refused_with_every_reason_while_a_song_is_unresolved(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).fails["rel-2"] = b"error: the disk said no\n"
    file_it(app, tape, service)
    refused = archive(app, tape, service)
    assert refused.status == 409
    assert "song 2 (Two)" in refused.json()["error"]
    assert "the disk said no" in refused.json()["error"]
    untouched(service, tape)
    assert not archived_at(service, tape).exists()
    assert view(app, tape, service)["playlist"]["archive_refusal"]


def test_strangers_of_the_same_name_do_not_stand_in_for_songs_that_failed_to_file(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    runner = beet_of(service)
    runner.fails[runner.LIST] = b"error: the disk said no\n"
    runner.owns("One", length="9:59", album="A Record")
    runner.owns("Two", length="9:59", album="A Record")
    assert file_it(app, tape, service)["error"]
    assert not rebuild(app, tape, service)["error"]
    gone = state_of(service, tape).left_out
    assert [(g.number, g.state) for g in gone] == [
        (1, P.NOT_FILED),
        (2, P.NOT_FILED),
    ]

    refused = archive(app, tape, service)
    assert refused.status == 409
    assert "song 1 (One)" in refused.json()["error"]
    untouched(service, tape)
    assert not archived_at(service, tape).exists()


def test_a_song_whose_filing_failed_holds_the_gate_though_a_row_now_has_its_id(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.fails["rel-2"] = b"error: the disk said no\n"
    assert file_it(app, tape, service)["error"]
    runner.recordings["Two"] = "rec-Two"
    runner.owns("Two", mb_trackid="rec-Two", album="Record rel-2")
    assert not rebuild(app, tape, service)["error"]
    assert state_of(service, tape).left_out == ()
    refused = archive(app, tape, service)
    assert (
        refused.status == 409
        and "song 2 (Two): did not file" in refused.json()["error"]
    )
    untouched(service, tape)


def test_every_song_skipped_leaves_no_stale_lines_says_so_and_clears_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    assert len(lines_of(service, tape)) == 4
    for number in (1, 2):
        answer = post(app, f"/api/tapes/{tape}/songs/{number}/skip", service)
        assert answer.status == 200

    assert not rebuild(app, tape, service)["error"]
    assert lines_of(service, tape)[2:] == []
    shown = view(app, tape, service)["playlist"]
    assert shown["note"] == P.ALL_SKIPPED and shown["songs"] == 0

    refused = archive(app, tape, service)
    assert refused.status == 409 and "every song is skipped" in refused.json()["error"]
    untouched(service, tape)
    assert not archived_at(service, tape).exists()


def test_a_tape_skipped_whole_before_any_playlist_has_no_file_and_says_why(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    for number in (1, 2):
        post(app, f"/api/tapes/{tape}/songs/{number}/skip", service)
    assert not rebuild(app, tape, service)["error"]
    done = state_of(service, tape)
    assert (done.file, done.note) == ("", P.ALL_SKIPPED)


def test_archive_is_refused_before_any_playlist_is_written(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    refused = archive(app, tape, service)
    assert refused.status == 409 and "no playlist" in refused.json()["error"]


def test_archive_asks_the_library_again_whatever_the_page_last_showed(
    service: Service,
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    beet_of(service).forgets(path_of_song(service, tape, 1))
    assert archive(app, tape, service).status == 202
    said = job_of(app, tape, service)["error"]
    assert "song 1 (One)" in said
    assert T.audio_file(service.layout, tape).is_file()
    assert not list(archived_at(service, tape).glob("*"))


def test_archive_is_refused_when_beets_cannot_answer_and_clears_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    beet_of(service).ls_fails = True
    assert archive(app, tape, service).status == 202
    assert "beets could not answer" in job_of(app, tape, service)["error"]
    untouched(service, tape)


def test_archive_is_refused_when_the_playlist_file_is_gone(service: Service) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    file_of(service, tape).unlink()
    refused = archive(app, tape, service)
    assert refused.status == 409 and "is gone" in refused.json()["error"]


def test_a_failed_read_back_clears_nothing_and_leaves_no_copy(service: Service) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    beet_of(service).inner.bad.add("part-")
    assert archive(app, tape, service).status == 202
    assert "did not read back" in job_of(app, tape, service)["error"]
    untouched(service, tape)
    assert list(archived_at(service, tape).iterdir()) == []
    assert state_of(service, tape).archived is None


def test_a_copy_of_another_length_clears_nothing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    monkeypatch.setattr(AR, "read_back", lambda *_a: 1.0)
    assert archive(app, tape, service).status == 202
    assert "did not read back" in job_of(app, tape, service)["error"]
    untouched(service, tape)


def test_the_copy_is_read_back_with_flac_and_a_decode_before_anything_is_cleared(
    service: Service,
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    runner = beet_of(service)
    assert archive(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]

    part = str(archived_at(service, tape) / f"part-{T.AUDIO}")
    assert ("flac", "-t", "-s", part) in runner.calls
    assert any(c[0] == "ffmpeg" and part in c for c in runner.calls)
    assert runner.at_read_back == [(True, {"01 One.flac", "02 Two.flac"})]

    assert (archived_at(service, tape) / T.AUDIO).is_file()
    assert not T.audio_file(service.layout, tape).exists()
    assert T.cuts_on_disk(service.layout, tape) == set()


def test_a_cut_of_a_skipped_song_is_moved_aside_and_not_deleted(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    post(app, f"/api/tapes/{tape}/songs/2/skip", service)
    file_it(app, tape, service)
    T.cut_file(service.layout, tape, "02 Two.flac").write_bytes(b"only copy")
    assert archive(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    aside = list((T.dir_for(service.layout, tape) / T.SUPERSEDED).iterdir())
    assert [p.read_bytes() for p in aside] == [b"only copy"]


def test_an_earlier_archived_copy_is_moved_aside_and_kept(service: Service) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    where = archived_at(service, tape)
    where.mkdir(parents=True)
    (where / T.AUDIO).write_bytes(b"an earlier take")
    assert archive(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    aside = list((where / AR.SUPERSEDED).iterdir())
    assert [p.read_bytes() for p in aside] == [b"an earlier take"]


def test_an_archived_tape_stays_listed_done_and_its_playlist_keeps_rebuilding(
    service: Service,
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    archive(app, tape, service)
    assert not job_of(app, tape, service)["error"]

    listed = get(app, "/api/tapes", service).json()["tapes"]
    assert [(t["id"], t.get("done")) for t in listed] == [(tape, True)]
    shown = view(app, tape, service)["playlist"]
    assert shown["done"] and shown["archived"]["seconds"] > 0
    assert "already archived" in shown["archive_refusal"]

    new = Path(service.settings.library) / "Reissue" / "1.flac"
    beet_of(service).moves(path_of_song(service, tape, 1), new)
    rebuild(app, tape, service)
    assert lines_of(service, tape)[2] == "../Reissue/1.flac"


def test_an_archived_tape_takes_no_new_audio_or_list(service: Service) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    archive(app, tape, service)
    begun = post(
        app, f"/api/tapes/{tape}/upload/begin", service, {"name": "a", "size": 1}
    )
    assert begun.status == 409 and "archived" in begun.json()["error"]
    listed = post(app, f"/api/tapes/{tape}/songs", service, {"songs": []})
    assert listed.status == 409 and "archived" in listed.json()["error"]


def test_an_archived_tape_takes_no_new_judgement_or_choice(service: Service) -> None:
    """The page offered none of these, and every route still took them."""
    app = build(service)
    tape = ready_to_archive(app, service)
    archive(app, tape, service)
    before = T.read(service.layout, tape)
    one = before.songs[0]
    for action, body in (
        ("release", {"release": "rel-other"}),
        ("from-list", {}),
        (
            "edges",
            {
                "start": one.start,
                "end": (one.end or 0.0) - 0.5,
                "seen_start": one.start,
                "seen_end": one.end,
            },
        ),
        ("heard", {}),
        ("skip", {}),
    ):
        answer = post(app, f"/api/tapes/{tape}/songs/1/{action}", service, body)
        assert answer.status == 409 and "archived" in answer.json()["error"], action
    matched = post(app, f"/api/tapes/{tape}/match", service, {"again": True})
    assert matched.status == 409 and "archived" in matched.json()["error"]
    assert T.read(service.layout, tape) == before


def test_a_tape_in_the_archive_never_reaches_the_record_picker(
    service: Service,
) -> None:
    app = build(service)
    tape = ready_to_archive(app, service)
    before = service.layout.albums(include_archive=True)
    archive(app, tape, service)
    assert archived_at(service, tape).is_dir()
    assert service.layout.albums(include_archive=True) == before
    assert not (service.layout.archive / tape).exists()


def test_a_tape_named_like_a_side_still_stays_out_of_the_record_picker(
    service: Service,
) -> None:
    before = service.layout.albums(include_archive=True)
    where = service.layout.archive / "_tape-side-a"
    where.mkdir(parents=True)
    (where / T.AUDIO).write_bytes(b"x")
    assert service.layout.albums(include_archive=True) == before


def test_the_page_shows_the_path_the_count_and_the_last_outcome(
    service: Service,
) -> None:
    app = build(service)
    tape = filed(app, service)
    shown = view(app, tape, service)["playlist"]
    assert shown["path"] == str(file_of(service, tape))
    assert shown["songs"] == 2 and shown["outcome"] == P.WRITTEN
    assert shown["at"] == service.now() and shown["archive_refusal"] == ""


def test_the_page_says_what_drives_the_server_and_what_removing_does_not_do() -> None:
    static = Path(RT.__file__).parent.parent / "static"
    markup = (static / "index.html").read_text()
    script = (static / "app.js").read_text()
    assert "drives the music server" in markup
    assert "does not remove its playlist" in markup
    assert "does not remove its playlist from the music server" in script
