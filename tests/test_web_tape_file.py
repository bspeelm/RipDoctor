"""Filing a tape over the wire, by the argv and stdin beets is sent, the rows it
leaves and the document that records them. ADR-063."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import Result
from ripdoctor.config.settings import Settings
from ripdoctor.core import filing as F
from ripdoctor.core import match as M
from ripdoctor.core import tape as TP
from ripdoctor.integrations import importer as IMP
from ripdoctor.store import tapes as T
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from ripdoctor.work import tape_jobs as TJ
from ripdoctor.work.jobs import Job
from tests.test_web_routes import a_service, get, post, signed_in
from tests.test_web_tape_cut import (
    TWO,
    Cutting,
    cut,
    directory,
    job_of,
    placed,
    settle_all,
)
from tests.test_web_tapes import a_tape_runner, save_list, seeing, send_csv

TG_TITLE = "TITLE"


class Gone(BaseException):
    """The machine going away: nothing in the program may catch this."""


def holds(value: str, asked: str) -> bool:
    """A query term as beets reads it, a regular expression after a second colon."""
    if asked.startswith(":"):
        return re.search(asked[1:], value) is not None
    return asked.casefold() in value.casefold()


class Beet:
    """A beets that moves what it is given and answers `ls` from its own rows.

    `fails`, by release (`LIST` for lookup-off): stderr, rc 1 and the files left.
    `loses`: rc 0, files taken, no row. `keeps`: rc 0, files left, no row.
    `dies`: the files taken and the rows written, then the machine goes away.
    `strands`: rows written against the copies, then the machine goes away.
    `refuses`: rows written against the copies, the files left, stderr and rc 1.
    `unmoved`: rows written against the copies, the files gone, rc 0.
    `elsewhere`: rows and files written outside the library, rc 0.
    """

    LIST = "from-list"

    def __init__(self, inner: Cutting, library: Path) -> None:
        self.inner, self.library = inner, library
        self.rows: list[dict[str, str]] = []
        self.tags: dict[str, dict[str, str]] = {}
        self.recordings: dict[str, str] = {}
        self.imports: list[tuple[tuple[str, ...], list[str]]] = []
        self.fails: dict[str, bytes] = {}
        self.loses: set[str] = set()
        self.keeps: set[str] = set()
        self.dies: set[str] = set()
        self.strands: set[str] = set()
        self.refuses: dict[str, bytes] = {}
        self.unmoved: set[str] = set()
        self.elsewhere: set[str] = set()
        self.remove_fails = False
        self.loaded = "embedart, replaygain"
        self.art = ""
        self.pictured: set[str] = set()
        self.embed_fails = False
        self.embed_lies = False
        self.gained: set[str] = set()
        self.gain_fails = False
        self.ls_fails = False
        self.album_path = "$albumartist/$album/$track $title"

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def owns(self, title: str, length: str = "0:05", **fields: str) -> Path:
        where = Path(fields.pop("file", str(self.library / "owned" / f"{title}.flac")))
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_bytes(b"filed")
        self.rows.append(
            {
                "title": title,
                "artist": "A Band",
                "album": "Elsewhere",
                "length": length,
                "path": str(where),
                **fields,
            }
        )
        return where

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = tuple(str(a) for a in argv)
        answer = self.inner.run(argv, stdin=stdin, timeout=timeout)
        if args[0] == "ffprobe" and stdin is not None:
            image = b'{"streams": [{"width": 1000, "height": 1000}]}'
            return Result(args, 0, image, b"")
        if args[0] == "ffmpeg" and args[-1].endswith(".jpg"):
            Path(args[-1]).write_bytes(b"\xff\xd8\xff" + b"\x00" * 4000)
            return Result(args, 0, b"", b"")
        if args[0] == "metaflac":
            if any(a.startswith("--import-picture-from=") for a in args):
                self.pictured.add(args[-1])
            if "--list" in args and args[-1] in self.pictured:
                return Result(args, 0, b"METADATA block #2\n", b"")
            if "--show-tag=REPLAYGAIN_TRACK_GAIN" in args and args[-1] in self.gained:
                return Result(args, 0, b"REPLAYGAIN_TRACK_GAIN=1.00 dB\n", b"")
            for arg in args[1:-1]:
                if arg.startswith("--set-tag="):
                    key, _, value = arg.removeprefix("--set-tag=").partition("=")
                    self.tags.setdefault(args[-1], {})[key] = value
        if args[0] != "beet":
            return answer
        if "version" in args:
            return Result(args, 0, f"plugins: {self.loaded}\n".encode(), b"")
        if "config" in args and "-d" in args:
            said = (
                f"directory: {self.library}\npaths:\n    default: {self.album_path}\n"
            )
            return Result(args, 0, said.encode(), b"")
        if "$artpath" in args:
            return Result(args, 0, f"{self.art or 'None'}\n".encode(), b"")
        if "$album_id" in args:
            row = next(r for r in self.rows if f"path:{r['path']}" == args[-1])
            return Result(args, 0, f"{self.album_ids(row)}\n".encode(), b"")
        if args[-1].startswith("album_id:"):
            same = [
                r["path"]
                for r in self.rows
                if self.album_ids(r) == int(args[-1].removeprefix("album_id:"))
            ]
            return Result(args, 0, "\n".join(same).encode(), b"")
        if "modify" in args:
            self.art = args[-1].removeprefix("artpath=")
            return Result(args, 0, b"", b"")
        if "embedart" in args:
            return self._embed(args)
        if "replaygain" in args:
            return self._gain(args)
        if "ls" in args:
            return self._ls(args)
        if "import" in args:
            return self._import(args)
        if "remove" in args:
            return self._remove(args)
        return answer

    def album_ids(self, row: dict[str, str]) -> int:
        names = list(dict.fromkeys(r["album"] for r in self.rows))
        return names.index(row["album"]) + 1

    def _row(self, copy: Path, where: Path, key: str) -> dict[str, str]:
        told = self.tags[str(copy)]
        return {
            "title": told[TG_TITLE],
            "artist": told.get("ARTIST", "A Band"),
            "albumartist": told.get("ALBUMARTIST", "A Band"),
            "album": told.get("ALBUM", key),
            "length": "0:05",
            "path": str(where),
            "mb_trackid": self.recordings.get(told[TG_TITLE], ""),
            "mb_albumid": "" if key == self.LIST else key,
            "track": told["TRACKNUMBER"],
        }

    def _embed(self, args: tuple[str, ...]) -> Result:
        if self.embed_fails:
            return Result(args, 1, b"", b"error: the art would not take\n")
        where = Path(args[-1].removeprefix("path:"))
        if not self.embed_lies:
            self.pictured.add(str(where))
        where.chmod(0o600)
        return Result(args, 0, b"", b"")

    def _gain(self, args: tuple[str, ...]) -> Result:
        if self.gain_fails:
            return Result(args, 1, b"", b"error: Fatal replay gain error\n")
        where = Path(args[-1].removeprefix("path:"))
        self.gained.add(str(where))
        where.chmod(0o600)
        return Result(args, 0, b"", b"")

    def _remove(self, args: tuple[str, ...]) -> Result:
        if self.remove_fails:
            return Result(args, 1, b"", b"database is locked\n")
        wanted = args[-1].removeprefix("path:")
        self.rows = [r for r in self.rows if r["path"] != wanted]
        return Result(args, 0, b"", b"")

    def _ls(self, args: tuple[str, ...]) -> Result:
        if self.ls_fails:
            return Result(args, 1, b"", b"database is locked\n")
        alternatives: list[list[list[str]]] = [[]]
        for term in args[args.index("-f") + 2 :]:
            if term == ",":
                alternatives.append([])
            else:
                alternatives[-1].append(term.split(":", 1))
        asked = [k.removeprefix("$") for k in args[args.index("-f") + 1].split("\x1f")]
        lines = [
            "\x1f".join(r.get(k, "") for k in asked)
            for r in self.rows
            if any(all(holds(r.get(k, ""), v) for k, v in t) for t in alternatives)
        ]
        return Result(args, 0, "\n".join(lines).encode(), b"")

    def _import(self, args: tuple[str, ...]) -> Result:
        where = Path(args[-1])
        key = (
            args[args.index("--search-id") + 1] if "--search-id" in args else self.LIST
        )
        self.imports.append((args, sorted(p.name for p in where.iterdir())))
        if key in self.fails:
            return Result(args, 1, b"", self.fails[key])
        if key in self.keeps:
            return Result(args, 0, b"Skipping.\n", b"")
        if key in self.strands or key in self.refuses or key in self.unmoved:
            for copy in sorted(where.iterdir()):
                self.rows.append(self._row(copy, copy, key))
            if key in self.strands:
                raise Gone
            if key in self.unmoved:
                for copy in sorted(where.iterdir()):
                    copy.unlink()
                return Result(args, 0, b"", b"")
            return Result(args, 1, b"", self.refuses[key])
        into = (
            self.library.parent / "outside" / key
            if key in self.elsewhere
            else self.library / key
        )
        into.mkdir(parents=True, exist_ok=True)
        cover = into / "cover.jpg"
        cover.write_bytes(b"art")
        cover.chmod(0o600)
        for copy in sorted(where.iterdir()):
            target = into / copy.name
            shutil.move(str(copy), target)
            target.chmod(0o600)
            if key in self.loses:
                continue
            self.rows.append(self._row(copy, target, key))
            if key != self.LIST:
                self.gained.add(str(target))
        if key in self.dies:
            raise Gone
        return Result(args, 0, b"", b"")


@pytest.fixture
def service(tmp_path: Path) -> Service:
    beet = Beet(Cutting(a_tape_runner()), tmp_path / "lib")
    made = a_service(tmp_path, runner=beet)  # type: ignore[arg-type]
    made.settings = Settings(library=str(tmp_path / "lib"), importer="beets")
    made.state_dir = str(tmp_path / "state")
    return made


def beet_of(service: Service) -> Beet:
    return service.runner  # type: ignore[return-value]


def cut_tape(app, service: Service) -> str:  # type: ignore[no-untyped-def]
    """Two songs, cut, each to file from the list unless `pin` says otherwise."""
    tape = placed(app, service)
    settle_all(app, tape, service)
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    return tape


def pin(service: Service, tape: str, number: int, release: str) -> None:
    current = T.read(service.layout, tape)
    title = current.songs[number - 1].title
    choice = M.Choice(
        release=release,
        group=f"group-{release}",
        release_title=f"Record {release}",
        album_artist="A Band",
        date="1975",
        status="Official",
        track_title=title,
        number=number,
        disc=1,
        recording=f"rec-{title}",
    )
    T.save(service.layout, TP.settle(current, number, choice))
    beet_of(service).recordings[title] = f"rec-{title}"


def pinned(app, service: Service, one: str = "rel-1", two: str = "rel-2") -> str:  # type: ignore[no-untyped-def]
    tape = cut_tape(app, service)
    pin(service, tape, 1, one)
    pin(service, tape, 2, two)
    return tape


def file_it(app, tape: str, service: Service) -> dict:  # type: ignore[no-untyped-def]
    answer = post(app, f"/api/tapes/{tape}/file", service)
    assert answer.status == 202, answer.json()
    return job_of(app, tape, service)


def songs(service: Service, tape: str) -> tuple[TP.Song, ...]:
    return T.read(service.layout, tape).songs


def imports_of(service: Service) -> list[tuple[str, ...]]:
    return [argv for argv, _ in beet_of(service).imports]


def a_job() -> Job:
    return Job(slug="x", op="file", started=0.0)


# ----------------------------------------------------------------- finding them again


def test_find_again_with_every_song_filed_has_nothing_to_place_and_is_refused(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    before = [(s.start, s.end) for s in songs(service, tape)]
    said = post(app, f"/api/tapes/{tape}/process", service)
    assert said.status == 409
    assert "nothing to place" in said.json()["error"]
    with pytest.raises(ValueError, match="nothing to place"):
        TJ.find_boundaries(service.runner, service.layout, tape, a_job())
    assert [(s.start, s.end) for s in songs(service, tape)] == before
    assert get(app, f"/api/tapes/{tape}", service).json()["place_refusal"]


def test_a_tape_with_a_held_song_and_a_lost_boundary_stops_asking_to_prepare(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    current = T.read(service.layout, tape)
    gaps = (replace(current.songs[0], start=None), current.songs[1])
    T.save(service.layout, replace(current, songs=gaps))
    assert get(app, f"/api/tapes/{tape}", service).json()["prepare_needed"] is False
    assert post(app, f"/api/tapes/{tape}/prepare", service).status == 202
    job = job_of(app, tape, service)
    assert "nothing to place" in job["error"]
    assert "nothing to place" in job["result"]["boundaries"]


def test_find_again_used_to_refuse_a_tape_with_one_song_filed_and_one_to_place(
    service: Service,
) -> None:
    """Which was the only way to get boundaries for a song whose copy has gone."""
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    current = T.read(service.layout, tape)
    filed, other = current.songs
    moved = replace(filed, start=(filed.start or 0.0) + 1.0)
    lost = replace(other, filing=F.PENDING, path="", start=None, end=None)
    T.save(service.layout, replace(current, songs=(moved, lost)))
    assert post(app, f"/api/tapes/{tape}/process", service).status == 202
    assert not job_of(app, tape, service)["error"]
    one, two = songs(service, tape)
    assert (one.start, one.end) == (moved.start, moved.end), "the filed song stays"
    assert two.start is not None and two.start >= (one.end or 0.0)


# ---------------------------------------------------------------- one run an album


def test_each_album_is_its_own_run_from_its_own_directory(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    seen = beet_of(service).imports
    assert [names for _, names in seen] == [["01 One.flac"], ["02 Two.flac"]]
    assert seen[0][0][-1] != seen[1][0][-1]
    assert [argv[argv.index("--search-id") + 1] for argv, _ in seen] == [
        "rel-1",
        "rel-2",
    ]


def test_two_songs_of_one_release_go_in_one_run(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service, "rel-1", "rel-1")
    assert not file_it(app, tape, service)["error"]
    assert [n for _, n in beet_of(service).imports] == [["01 One.flac", "02 Two.flac"]]


def test_a_pinned_run_sends_exactly_this_and_writes_only_title_and_number(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    file_it(app, tape, service)
    argv = imports_of(service)[0]
    override = str(Path(service.state_dir) / IMP.OVERRIDE_FILE)
    assert argv[:6] == ("beet", "--config", override, "import", "--search-id", "rel-1")
    assert len(argv) == 7
    runner = beet_of(service)
    at = runner.calls.index(argv)
    assert runner.fed[at] == b"A\n"
    assert sorted(next(iter(runner.tags.values()))) == ["TITLE", "TRACKNUMBER"]


def fed_to_import(service: Service) -> list[bytes]:
    runner = beet_of(service)
    return [f for c, f in zip(runner.calls, runner.fed, strict=True) if "import" in c]


def album_row(service: Service, tape: str, release: str) -> None:
    """A row of the album song 1 is pinned to, filed from `release`."""
    beet_of(service).owns(
        "Another",
        album="Record rel-1",
        albumartist="A Band",
        mb_albumid=release,
        length="9:00",
    )


def test_a_pinned_run_into_nothing_held_answers_only_the_release(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service) == [b"A\n", b"A\n"]
    argv = imports_of(service)[0]
    assert argv[3:5] == ("import", "--search-id")


def test_a_pinned_run_joining_its_own_edition_merges(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    album_row(service, tape, "rel-1")
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service) == [b"A\nM\nA\n", b"A\n"]
    assert songs(service, tape)[0].filing_note == ""


def test_merging_into_another_edition_once_rewrote_an_owned_album_so_it_keeps_both(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    album_row(service, tape, "rel-other")
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service) == [b"A\nK\n", b"A\n"]
    assert not [f for f in fed_to_import(service) if b"M" in f or b"R" in f]
    one = songs(service, tape)[0]
    assert one.filing == F.FILED
    assert "separate album entry" in one.filing_note
    assert "did not change" in one.filing_note


def canonically(service: Service, release: str) -> None:
    """A row of the album song 1 is pinned to, under the name beets itself keeps for
    the artist, where the release credits another."""
    beet_of(service).owns(
        "Another",
        album="Record rel-1",
        albumartist="The Artist As Beets Knows Them",
        mb_albumid=release,
        length="9:00",
    )


def test_an_edition_under_the_artists_own_name_is_seen_and_never_merged_into(
    service: Service,
) -> None:
    """Rows were read by the release's credit, so these were missed, and beets, asking
    by the artist's own name, could take a merge onto another edition."""
    app = build(service)
    tape = pinned(app, service)
    canonically(service, "rel-other")
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service)[0] == b"A\nK\n"


def test_a_later_song_joining_an_album_under_the_artists_own_name_merges(
    service: Service,
) -> None:
    """It was answered as if nothing were held, and the duplicate question beets then
    asked failed that song on every retry."""
    app = build(service)
    tape = pinned(app, service)
    canonically(service, "rel-1")
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service)[0] == b"A\nM\nA\n"


def test_a_library_holding_two_editions_is_never_merged_into_either(
    service: Service,
) -> None:
    """Merging takes in every album row of that name, whichever it was pinned to."""
    app = build(service)
    tape = pinned(app, service)
    album_row(service, tape, "rel-1")
    album_row(service, tape, "rel-other")
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service)[0] == b"A\nK\n"


def test_an_owned_album_row_with_no_release_is_not_merged_into(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    album_row(service, tape, "")
    assert not file_it(app, tape, service)["error"]
    assert fed_to_import(service)[0] == b"A\nK\n"


def test_a_library_that_cannot_say_what_it_holds_imports_nothing(
    service: Service,
) -> None:
    """Which answer is safe depends on it, so a run does not go ahead on a guess."""
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    real = runner._ls

    def locked(args: tuple[str, ...]) -> Result:
        if "$mb_albumid" in args[args.index("-f") + 1]:
            return Result(args, 1, b"", b"database is locked\n")
        return real(args)

    runner._ls = locked  # type: ignore[method-assign]
    job = file_it(app, tape, service)
    assert "did not file" in job["error"]
    assert not imports_of(service)
    assert "database is locked" in songs(service, tape)[0].filing_note


@pytest.mark.parametrize("failing", [False, True])
def test_replace_is_never_sent_and_nothing_is_removed(
    service: Service, failing: bool
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.owns("Two", album="Record rel-2", mb_albumid="rel-2", length="9:00")
    if failing:
        runner.fails["rel-1"] = b"error: stdin stream ended\n"
    file_it(app, tape, service)
    sent = fed_to_import(service)
    assert sent and not [f for f in sent if b"R" in f]
    assert not [c for c in runner.calls if c[0] == "beet" and "remove" in c]


# --------------------------------------------------------------------- the pre-check


def test_the_library_is_asked_about_each_song_before_its_import(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service, "rel-1", "rel-1")
    file_it(app, tape, service)
    calls = beet_of(service).calls
    at = calls.index(imports_of(service)[0])
    for title in ("One", "Two"):
        first = next(
            i for i, c in enumerate(calls) if c[-1] == f"mb_trackid:rec-{title}"
        )
        assert first < at


def test_a_song_owned_by_recording_id_is_not_imported_and_says_where(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    where = beet_of(service).owns("One", mb_trackid="rec-One")
    assert not file_it(app, tape, service)["error"]
    assert [n for _, n in beet_of(service).imports] == [["02 Two.flac"]]
    one = songs(service, tape)[0]
    assert (one.filing, one.path) == (F.ALREADY, str(where))


def test_a_row_with_no_id_is_shown_for_a_person_to_confirm_and_nothing_is_imported(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    where = beet_of(service).owns("One", length="0:06")
    assert file_it(app, tape, service)["error"]
    assert ["01 One.flac"] not in [n for _, n in beet_of(service).imports]
    one = songs(service, tape)[0]
    assert (one.filing, one.path) == (F.FAILED, "")
    assert str(where) in one.filing_note and "rec-One" in one.filing_note


def doubted_copy(app, service: Service) -> tuple[str, Path]:  # type: ignore[no-untyped-def]
    tape = pinned(app, service)
    where = beet_of(service).owns("One", length="0:06")
    assert file_it(app, tape, service)["error"]
    return tape, where


def use_copy(app, tape: str, service: Service, path: object):  # type: ignore[no-untyped-def]
    return post(
        app, f"/api/tapes/{tape}/songs/1/use-copy", service, {"path": str(path)}
    )


def test_a_no_id_doubt_shows_the_row_and_nothing_else_has_a_copy_to_use(
    service: Service,
) -> None:
    app = build(service)
    tape, where = doubted_copy(app, service)
    outcomes = {
        o["number"]: o for o in get(app, f"/api/tapes/{tape}", service).json()["filing"]
    }
    assert outcomes[1]["unconfirmed"] == str(where)
    assert not outcomes[2]["unconfirmed"]


def test_using_the_copy_in_the_library_records_it_as_the_person_s_at_that_path(
    service: Service,
) -> None:
    app = build(service)
    tape, where = doubted_copy(app, service)
    where.chmod(0o600)
    answer = use_copy(app, tape, service, where)
    assert answer.status == 200, answer.json()
    row = {o["number"]: o for o in answer.json()["filing"]}[1]
    assert (row["state"], row["path"]) == (F.ALREADY, str(where))
    assert row["settled_by"] == M.PERSON and not row["unconfirmed"]
    assert answer.json()["all_filed"]
    assert stat.S_IMODE(where.stat().st_mode) == 0o600


def test_a_copy_a_person_chose_is_not_moved_by_a_later_run(
    service: Service,
) -> None:
    app = build(service)
    tape, where = doubted_copy(app, service)
    use_copy(app, tape, service, where)
    before = len(beet_of(service).imports)
    runs = TJ.F.runs(T.read(service.layout, tape))
    assert 1 not in [t.song.number for run in runs for t in run.tracks]
    assert post(app, f"/api/tapes/{tape}/match", service, {"again": True}).status == 202
    job_of(app, tape, service)
    one = songs(service, tape)[0]
    assert (one.filing, one.path, one.settled_by) == (F.ALREADY, str(where), M.PERSON)
    assert len(beet_of(service).imports) == before


def test_a_copy_nothing_is_waiting_on_cannot_be_used(service: Service) -> None:
    app = build(service)
    tape, where = doubted_copy(app, service)
    assert (
        use_copy(app, tape, service, service.settings.library + "/other.flac").status
        == 409
    )
    assert use_copy(app, tape, service, "").status == 409
    assert (
        post(
            app, f"/api/tapes/{tape}/songs/2/use-copy", service, {"path": str(where)}
        ).status
        == 409
    )
    assert songs(service, tape)[0].filing == F.FAILED


def test_a_copy_gone_from_the_library_cannot_be_used(service: Service) -> None:
    app = build(service)
    tape, where = doubted_copy(app, service)
    where.unlink()
    answer = use_copy(app, tape, service, where)
    assert answer.status == 409 and "any more" in answer.json()["error"]
    assert songs(service, tape)[0].filing == F.FAILED


def test_a_from_list_song_owned_by_title_and_length_is_not_imported(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    where = beet_of(service).owns("One", length="0:06", mb_trackid="any")
    assert not file_it(app, tape, service)["error"]
    assert [n for _, n in beet_of(service).imports] == [["02 Two.flac"]]
    one = songs(service, tape)[0]
    assert (one.filing, one.path) == (F.ALREADY, str(where))


def test_a_live_row_of_another_recording_is_not_owned_and_the_song_is_imported(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    live = beet_of(service).owns(
        "One", album="Live Somewhere", mb_trackid="rec-of-the-live-one"
    )
    assert not file_it(app, tape, service)["error"]
    assert imports_of(service)[0][-2] == "rel-1"
    one = songs(service, tape)[0]
    assert one.filing == F.FILED and one.path != str(live)


def test_the_same_title_on_a_live_album_is_refused_by_length_and_the_song_files(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).owns("One", length="7:30", album="Live Somewhere")
    assert not file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FILED


def test_a_copy_of_an_owned_song_never_joins_its_album_s_merge(
    service: Service,
) -> None:
    """Merging one files it, silently, as a different track."""
    app = build(service)
    tape = pinned(app, service, "rel-1", "rel-1")
    beet_of(service).owns("One", mb_trackid="rec-One", mb_albumid="rel-1")
    file_it(app, tape, service)
    assert [n for _, n in beet_of(service).imports] == [["02 Two.flac"]]
    assert [s.filing for s in songs(service, tape)] == [F.ALREADY, F.FILED]


def test_a_title_match_with_no_length_fails_the_song_rather_than_assuming(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).owns("One", length="")
    job = file_it(app, tape, service)
    assert "song 1 (One)" in job["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert "no length to compare" in one.filing_note
    assert ["01 One.flac"] not in [n for _, n in beet_of(service).imports]


def test_a_library_that_cannot_be_asked_imports_nothing(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).ls_fails = True
    assert file_it(app, tape, service)["error"]
    assert imports_of(service) == []
    assert all("database is locked" in s.filing_note for s in songs(service, tape))


def test_a_name_beets_would_read_as_query_syntax_is_asked_for_plainly(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service, TWO.replace("A Band,A Record", '=A Band,"A Record,"'))
    settle_all(app, tape, service)
    assert cut(app, tape, service).status == 202
    assert not file_it(app, tape, service)["error"]
    asked = [c for c in beet_of(service).calls if c[0] == "beet" and "ls" in c]
    terms = {t for c in asked for t in c[c.index("-f") + 2 :]}
    assert {F.term("artist", "=A Band"), F.term("album", "A Record,")} <= terms
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]
    assert not [t for t in terms if t.endswith(",") or t.split(":", 1)[1][:1] == "="]


# ---------------------------------------------------------------------- from a list


def test_from_list_songs_of_one_album_go_in_one_lookup_off_run_with_full_tags(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    assert not file_it(app, tape, service)["error"]
    runner = beet_of(service)
    assert [n for _, n in runner.imports] == [["01 One.flac", "02 Two.flac"]]
    argv = imports_of(service)[0]
    assert argv[3:5] == ("import", "-A")
    assert "--search-id" not in argv
    assert runner.fed[runner.calls.index(argv)] == b"K\n"
    assert sorted(runner.tags.values(), key=lambda t: t["TRACKNUMBER"]) == [
        {
            "TITLE": title,
            "TRACKNUMBER": number,
            "ARTIST": "A Band",
            "ALBUMARTIST": "A Band",
            "ALBUM": "A Record",
        }
        for number, title in (("1", "One"), ("2", "Two"))
    ]
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_from_list_album_already_there_says_one_more_entry_and_never_counts_them(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    beet_of(service).owns(
        "Another", album="A Record", albumartist="A Band", length="9:00"
    )
    assert not file_it(app, tape, service)["error"]
    notes = [s.filing_note for s in songs(service, tape)]
    assert all("one more album entry" in note for note in notes)
    assert not any("two" in note or "second" in note for note in notes)


def test_a_from_list_album_not_there_says_nothing_of_a_second_row(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    file_it(app, tape, service)
    assert [s.filing_note for s in songs(service, tape)] == ["", ""]


# ----------------------------------------------------------------------- the read-back


def test_one_album_failing_leaves_the_others_filed(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).fails["rel-1"] = b"error: something went wrong\n"
    job = file_it(app, tape, service)
    assert "1 of 2" in job["error"]
    assert "song 1 (One)" in job["error"]
    one, two = songs(service, tape)
    assert (one.filing, two.filing) == (F.FAILED, F.FILED)
    assert "something went wrong" in one.filing_note


def test_rc_0_with_no_row_afterward_is_a_failure(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).loses.add("rel-1")
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert "another track" in one.filing_note


def test_rc_0_with_the_file_left_behind_is_a_failure(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).keeps.add("rel-1")
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert one.filing_note == "beets did not take it: Skipping."


def test_a_row_whose_file_is_missing_after_the_import_is_a_failure(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    original = beet_of(service)._import

    def then_lost(args: tuple[str, ...]) -> Result:
        answer = original(args)
        Path(beet_of(service).rows[-1]["path"]).unlink()
        return answer

    beet_of(service)._import = then_lost  # type: ignore[method-assign]
    assert file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FAILED


def test_a_nonzero_exit_with_the_song_verifiably_there_counts_as_filed(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    original = beet_of(service)._import

    def grumbling(args: tuple[str, ...]) -> Result:
        original(args)
        return Result(args, 1, b"", b"a plugin complained\n")

    beet_of(service)._import = grumbling  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_nonzero_exit_with_the_song_there_says_what_beets_did_not_finish_on_the_page(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    original = beet_of(service)._import

    def locked(args: tuple[str, ...]) -> Result:
        original(args)
        return Result(args, 1, b"", b"sqlite3.OperationalError: database is locked\n")

    beet_of(service)._import = locked  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    view = get(app, f"/api/tapes/{tape}", service).json()
    for outcome in view["filing"]:
        assert outcome["state"] == F.FILED
        assert "beets exited 1" in outcome["note"]
        assert "art" in outcome["note"]
        assert "database is locked" in outcome["note"]


def test_a_zero_exit_with_the_song_there_has_no_complaint_to_make(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    file_it(app, tape, service)
    assert [s.filing_note for s in songs(service, tape)] == ["", ""]


def test_a_nonzero_exit_with_the_row_only_on_our_copy_is_not_filed(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).refuses["rel-1"] = b"sqlite3.OperationalError: locked\n"
    assert file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FAILED


def test_a_tool_that_fails_while_tagging_fails_that_album_and_no_other(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    inner = beet_of(service).inner.inner
    inner.replies.insert(
        0,
        (
            lambda a: a[0] == "metaflac" and "One" in a[-1],
            Result((), 1, b"", b"metaflac: cannot write\n"),
        ),
    )
    assert file_it(app, tape, service)["error"]
    one, two = songs(service, tape)
    assert (one.filing, two.filing) == (F.FAILED, F.FILED)
    assert "cannot write" in one.filing_note


def test_a_relative_row_crash_tells_the_person_the_fix(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).fails["rel-1"] = (
        b"Traceback (most recent call last):\n"
        b"FileNotFoundError: [Errno 2] No such file: 'Music/a.flac'\n"
    )
    job = file_it(app, tape, service)
    assert job["error"]
    note = songs(service, tape)[0].filing_note
    assert "relative path" in note
    assert "Back up the library database" in note


def test_a_missing_absolute_path_is_named_and_not_called_a_relative_row() -> None:
    why = IMP.TapeRun(
        1, "FileNotFoundError: [Errno 2] No such file or directory: b'/lib/a.flac'\n"
    ).why()
    assert "/lib/a.flac" in why
    assert "relative" not in why
    assert "beet remove" in why


def test_a_relative_row_beets_resolved_against_where_it_runs_is_still_called_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lib"
    (root / "Band").mkdir(parents=True)
    (root / "Band" / "a.flac").write_bytes(b"filed")
    elsewhere = tmp_path / "ran-here"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    said = (
        "FileNotFoundError: [Errno 2] No such file or directory: "
        f"b'{elsewhere.resolve()}/Band/a.flac'\n"
    )
    assert "relative path" in IMP.TapeRun(1, said, str(root)).why()
    assert "relative path" not in IMP.TapeRun(1, said).why()


def test_a_missing_path_under_where_beets_runs_with_nothing_under_the_root_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    said = (
        "FileNotFoundError: [Errno 2] No such file or directory: "
        f"b'{tmp_path.resolve()}/lib/Band/a.flac'\n"
    )
    why = IMP.TapeRun(1, said, str(tmp_path / "lib")).why()
    assert "relative path" not in why
    assert "beet remove" in why


def test_a_missing_path_with_an_apostrophe_is_named_whole() -> None:
    why = IMP.TapeRun(
        1,
        'FileNotFoundError: [Errno 2] No such file or directory: "/lib/You\'ll.flac"\n',
    ).why()
    assert "/lib/You'll.flac" in why


def test_a_relative_row_crash_in_bytes_is_still_called_a_relative_row() -> None:
    why = IMP.TapeRun(
        1, "FileNotFoundError: [Errno 2] No such file or directory: b'Music/a.flac'\n"
    ).why()
    assert "relative path" in why


def test_a_bogus_release_says_what_beets_said_about_it_and_not_only_the_prompt() -> (
    None
):
    why = IMP.TapeRun(
        1,
        "/filing/1 (2 items)\nNo matching release found for 2 tracks.\n"
        "For help, see: https://example.invalid\n[S]kip, Use as-is? \n"
        "error: stdin stream ended while input required\n",
    ).why()
    assert "No matching release found for 2 tracks." in why
    assert "check the release" in why


# ------------------------------------------------------- rows that are not the library


def killed_midway(service: Service, tape: str) -> None:
    settings = service.settings
    with pytest.raises(Gone):
        TJ.file_songs(
            beet_of(service),  # type: ignore[arg-type]
            IMP.Beets(config=str(Path(service.state_dir) / IMP.OVERRIDE_FILE)),
            service.layout,
            tape,
            a_job(),
            2000.0,
            library=settings.library,
            file_mode=settings.file_mode,
            dir_mode=settings.dir_mode,
        )


def removes_of(service: Service) -> list[tuple[str, ...]]:
    return [c for c in beet_of(service).calls if c[0] == "beet" and "remove" in c]


def test_an_import_killed_with_its_row_on_our_copy_is_filed_by_the_retry(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service, "rel-1", "rel-1")
    runner = beet_of(service)
    runner.strands.add("rel-1")
    killed_midway(service, tape)
    held = [r["path"] for r in runner.rows]
    assert len(held) == 2
    assert all(f"/{T.FILING}/" in p for p in held)

    runner.strands.clear()
    runner.imports.clear()
    assert not file_it(app, tape, service)["error"]
    assert [n for _, n in runner.imports] == [["01 One.flac", "02 Two.flac"]]
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]
    for song in songs(service, tape):
        assert Path(song.path).is_file()
        assert Path(song.path).is_relative_to(tmp_path / "lib")
    assert not [r for r in runner.rows if f"/{T.FILING}/" in r["path"]]


def test_clearing_our_row_is_by_its_exact_path_and_never_deletes(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    kept = runner.owns("Other", mb_trackid="rec-Other")
    runner.strands.add("rel-1")
    killed_midway(service, tape)
    stranded = next(r["path"] for r in runner.rows if f"/{T.FILING}/" in r["path"])
    runner.strands.clear()
    file_it(app, tape, service)

    override = str(Path(service.state_dir) / IMP.OVERRIDE_FILE)
    assert removes_of(service) == [
        ("beet", "--config", override, "remove", "-f", f"path:{stranded}")
    ]
    assert kept.is_file()
    assert str(kept) in [r["path"] for r in runner.rows]


def test_a_row_merely_beside_our_copies_is_not_cleared(service: Service) -> None:
    """They come from another program, and this forgets what they name."""
    app = build(service)
    tape = pinned(app, service)
    beside = str(T.filing_dir(service.layout, tape)) + "-kept/Other.flac"
    beet_of(service).owns("Other", mb_trackid="rec-Other", file=beside)
    file_it(app, tape, service)
    assert removes_of(service) == []


def test_a_from_list_song_s_row_on_our_copy_is_not_taken_for_owned_by_its_title(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    runner = beet_of(service)
    runner.strands.add(runner.LIST)
    killed_midway(service, tape)
    runner.strands.clear()
    runner.imports.clear()
    assert not file_it(app, tape, service)["error"]
    assert len(runner.imports) == 1
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_row_outside_the_library_is_not_already_there_and_nothing_is_imported(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    outside = tmp_path / "outside" / "One.flac"
    runner.owns("One", mb_trackid="rec-One", file=str(outside))
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert str(outside) in one.filing_note
    assert ["01 One.flac"] not in [n for _, n in runner.imports]
    assert removes_of(service) == []
    assert outside.is_file()


def test_a_row_in_the_tape_s_own_directory_is_not_owned_and_not_ours_to_clear(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    service.settings = Settings(library=str(tmp_path), importer="beets")
    runner = beet_of(service)
    cut_of_it = directory(service, tape) / "01 One.flac"
    runner.owns("One", mb_trackid="rec-One", file=str(cut_of_it))
    assert file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FAILED
    assert removes_of(service) == []


def test_rc_0_that_leaves_the_row_on_a_copy_now_gone_is_a_failure_and_clears_it(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.unmoved.add("rel-1")
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert not [r for r in runner.rows if f"/{T.FILING}/" in r["path"]]


def test_a_song_beets_filed_outside_the_library_is_a_failure_that_says_where(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).elsewhere.add("rel-1")
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert str(tmp_path / "outside" / "rel-1" / "01 One.flac") in one.filing_note
    assert "outside the library" in one.filing_note


def test_an_unwritable_target_says_what_to_do_and_the_retry_needs_no_clearing(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.refuses["rel-1"] = b"PermissionError: [Errno 13] Permission denied: 'x'\n"
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FAILED
    assert "Permission denied" in one.filing_note
    assert "file again" in one.filing_note
    assert not [r for r in runner.rows if f"/{T.FILING}/" in r["path"]]

    runner.refuses.clear()
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert (one.filing, one.filing_note) == (F.FILED, "")


def test_a_row_that_cannot_be_cleared_yet_is_said_and_the_retry_clears_it(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.refuses["rel-1"] = b"error: the disk is unhappy\n"
    runner.remove_fails = True
    assert file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert "could not clear" in one.filing_note
    assert "the disk is unhappy" in one.filing_note

    runner.refuses.clear()
    runner.remove_fails = False
    assert not file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FILED
    assert not [r for r in runner.rows if f"/{T.FILING}/" in r["path"]]


def test_a_copy_stays_until_its_own_song_is_verified(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service, "rel-1", "rel-2")
    runner = beet_of(service)
    runner.keeps.add("rel-1")
    assert file_it(app, tape, service)["error"]
    filing = T.filing_dir(service.layout, tape)
    assert [p.name for p in filing.rglob("*.flac")] == ["01 One.flac"]

    runner.keeps.clear()
    assert not file_it(app, tape, service)["error"]
    assert not filing.exists()


# ---------------------------------------------------------------- retry and restart


def test_a_retry_does_only_what_is_unfinished(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.fails["rel-1"] = b"error: not today\n"
    file_it(app, tape, service)
    runner.fails.clear()
    runner.imports.clear()
    filed_at = songs(service, tape)[1].path
    runner.calls.clear()
    runner.fed.clear()

    assert not file_it(app, tape, service)["error"]
    assert [n for _, n in runner.imports] == [["01 One.flac"]]
    assert not [c for c in runner.calls if c[-1] == "mb_trackid:rec-Two"]
    one, two = songs(service, tape)
    assert (one.filing, one.filing_note) == (F.FILED, "")
    assert (two.filing, two.path) == (F.FILED, filed_at)


def test_filing_a_finished_tape_again_touches_nothing(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    file_it(app, tape, service)
    runner = beet_of(service)
    runner.calls.clear()
    runner.fed.clear()
    assert not file_it(app, tape, service)["error"]
    assert runner.calls == []


def test_a_restart_mid_run_keeps_what_was_recorded_and_finds_what_beets_took(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.dies.add("rel-2")
    settings = service.settings
    with pytest.raises(Gone):
        TJ.file_songs(
            runner,  # type: ignore[arg-type]
            IMP.Beets(config=str(Path(service.state_dir) / IMP.OVERRIDE_FILE)),
            service.layout,
            tape,
            a_job(),
            2000.0,
            library=settings.library,
            file_mode=settings.file_mode,
            dir_mode=settings.dir_mode,
        )
    one, two = songs(service, tape)
    assert (one.filing, two.filing) == (F.FILED, TP.PENDING)

    runner.dies.clear()
    runner.imports.clear()
    assert not file_it(app, tape, service)["error"]
    assert runner.imports == []
    one, two = songs(service, tape)
    assert (one.filing, two.filing) == (F.FILED, F.FILED)
    assert Path(two.path).is_file()


def test_a_song_recovered_after_a_restart_gets_its_modes_back(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.dies.add("rel-1")
    killed_midway(service, tape)
    assert [s.import_begun for s in songs(service, tape)] == [True, False]

    runner.dies.clear()
    assert not file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FILED
    assert not songs(service, tape)[0].import_begun
    for found in (tmp_path / "lib" / "rel-1").glob("*.flac"):
        assert stat.S_IMODE(found.stat().st_mode) == service.settings.file_mode


def list_is_out(service: Service, tape: str) -> None:
    """The list says 30 s and the cut is 5: a person heard it and accepted that."""
    current = T.read(service.layout, tape)
    long = tuple(replace(s, length=30.0) for s in current.songs)
    T.save(service.layout, replace(current, songs=long))


def timing_out(service: Service, word: str) -> None:
    runner = beet_of(service)
    original = runner.run

    def hangs(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if word in argv:
            raise subprocess.TimeoutExpired(argv, timeout or 0.0)
        return original(argv, stdin=stdin, timeout=timeout)

    runner.run = hangs  # type: ignore[method-assign]


def locked_after_the_import(service: Service) -> None:
    runner = beet_of(service)
    original = runner._import

    def imported_then_locked(args: tuple[str, ...]) -> Result:
        answer = original(args)
        runner.ls_fails = True
        return answer

    runner._import = imported_then_locked  # type: ignore[method-assign]


def test_a_gain_that_times_out_is_a_note_and_the_song_stays_filed_with_its_modes(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    timing_out(service, "replaygain")
    assert not file_it(app, tape, service)["error"]
    for song in songs(service, tape):
        assert song.filing == F.FILED and "timed out" in song.filing_note
        assert not song.import_begun
        assert (
            stat.S_IMODE(Path(song.path).stat().st_mode) == service.settings.file_mode
        )
    assert len(imports_of(service)) == 1


def test_a_read_back_that_cannot_answer_is_unknown_and_the_retry_only_reads_back(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    list_is_out(service, tape)
    runner = beet_of(service)
    locked_after_the_import(service)
    job = file_it(app, tape, service)
    assert "did not file" in job["error"]
    for song in songs(service, tape):
        assert (song.filing, song.import_begun) == (F.UNKNOWN, True)
        assert "database is locked" in song.filing_note
    rows = list(runner.rows)
    assert len(rows) == 2

    runner.ls_fails = False
    assert not file_it(app, tape, service)["error"]
    assert len(imports_of(service)) == 1
    assert runner.rows == rows
    for song in songs(service, tape):
        assert (song.filing, song.import_begun) == (F.FILED, False)
        assert (
            stat.S_IMODE(Path(song.path).stat().st_mode) == service.settings.file_mode
        )


def test_a_retry_the_library_still_cannot_answer_imports_nothing_and_stays_unknown(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    runner = beet_of(service)
    locked_after_the_import(service)
    file_it(app, tape, service)
    assert file_it(app, tape, service)["error"]
    assert len(runner.imports) == 1
    for song in songs(service, tape):
        assert (song.filing, song.import_begun) == (F.UNKNOWN, True)


def test_an_import_that_times_out_is_unknown_and_never_imported_over(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    original = runner._import

    def filed_then_hung(args: tuple[str, ...]) -> Result:
        original(args)
        raise subprocess.TimeoutExpired(args, 3600.0)

    runner._import = filed_then_hung  # type: ignore[method-assign]
    assert file_it(app, tape, service)["error"]
    assert [s.filing for s in songs(service, tape)] == [F.UNKNOWN, F.UNKNOWN]

    runner._import = original  # type: ignore[method-assign]
    runner.imports.clear()
    assert not file_it(app, tape, service)["error"]
    assert runner.imports == []
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_retry_told_there_is_no_row_files_the_song_as_usual(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service, "rel-1", "rel-1")
    runner = beet_of(service)
    original = runner._import

    def locked_with_nothing_filed(args: tuple[str, ...]) -> Result:
        runner.ls_fails = True
        return Result(args, 1, b"", b"database is locked\n")

    runner._import = locked_with_nothing_filed  # type: ignore[method-assign]
    file_it(app, tape, service)
    assert [s.filing for s in songs(service, tape)] == [F.UNKNOWN, F.UNKNOWN]

    runner._import = original  # type: ignore[method-assign]
    runner.ls_fails = False
    assert not file_it(app, tape, service)["error"]
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_stranger_of_the_same_name_is_not_taken_for_an_interrupted_import(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    runner = beet_of(service)
    current = T.read(service.layout, tape)
    begun = tuple(replace(s, import_begun=True) for s in current.songs)
    T.save(service.layout, replace(current, songs=begun))
    stranger = runner.owns("One", length="9:59", album="A Record", track="01")
    assert not file_it(app, tape, service)["error"]
    assert len(runner.imports) == 1
    assert songs(service, tape)[0].path != str(stranger)


def test_a_song_that_landed_outside_the_library_is_read_back_and_not_imported_again(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.elsewhere.add("rel-1")
    assert file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].import_begun
    runner.imports.clear()
    assert file_it(app, tape, service)["error"]
    assert "outside the library" in songs(service, tape)[0].filing_note
    assert runner.imports == []


def test_each_attempt_at_an_album_uses_a_new_directory(service: Service) -> None:
    """beets asks about resuming a path it was interrupted on."""
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.fails["rel-1"] = b"error: not today\n"
    file_it(app, tape, service)
    service.now = lambda: 3000.0
    file_it(app, tape, service)
    dirs = [a[-1] for a in imports_of(service) if "rel-1" in a]
    assert len(dirs) == 2
    assert dirs[0] != dirs[1]


# ---------------------------------------------------------------- what stays behind


def test_a_verified_cut_stays_where_it_was_and_filing_leaves_no_copies(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    before = {p.name: p.read_bytes() for p in directory(service, tape).iterdir()}
    file_it(app, tape, service)
    after = {p.name: p.read_bytes() for p in directory(service, tape).iterdir()}
    assert before == after
    assert before
    assert not (T.dir_for(service.layout, tape) / T.FILING).exists()


def test_tags_are_written_to_the_copy_and_never_to_the_cut(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    file_it(app, tape, service)
    written = list(beet_of(service).tags)
    assert written
    assert all(f"/{T.FILING}/" in path for path in written)


def test_modes_are_set_on_each_album_directory_after_its_import(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    file_it(app, tape, service)
    for release in ("rel-1", "rel-2"):
        for found in (tmp_path / "lib" / release).glob("*.flac"):
            assert stat.S_IMODE(found.stat().st_mode) == service.settings.file_mode


def test_an_owned_album_s_modes_are_left_alone(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    where = beet_of(service).owns("One", mb_trackid="rec-One")
    where.chmod(0o600)
    where.parent.chmod(0o700)
    file_it(app, tape, service)
    assert stat.S_IMODE(where.stat().st_mode) == 0o600
    assert stat.S_IMODE(where.parent.stat().st_mode) == 0o700


def test_a_song_only_the_pre_check_recognises_after_its_import_began_gets_its_modes(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    where = beet_of(service).owns("One", length="0:06")
    where.chmod(0o600)
    T.save(service.layout, F.begun(T.read(service.layout, tape), {1}))
    assert not file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FILED
    assert stat.S_IMODE(where.stat().st_mode) == service.settings.file_mode


def test_modes_that_cannot_be_set_are_a_note_on_a_song_that_stays_filed(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_a: object) -> int:
        raise PermissionError("not yours to change")

    monkeypatch.setattr(TJ.TG, "set_file_modes", refused)
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FILED and "not yours to change" in one.filing_note


def test_a_song_joining_a_folder_stripped_its_setgid_and_opened_what_was_there(
    service: Service, tmp_path: Path
) -> None:
    """Measured on a real album: its directory went 2775 to 775, and an old cover from
    600 to 664, for one song a tape added beside it."""
    folder = tmp_path / "lib" / "rel-1"
    folder.mkdir(parents=True)
    folder.chmod(0o2775)
    old_track, old_cover = folder / "03 Old.flac", folder / "cover-old.jpg"
    for theirs in (old_track, old_cover):
        theirs.write_bytes(b"theirs")
        theirs.chmod(0o600)
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).art = str(folder / "cover.jpg")
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert Path(one.path).parent == folder
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2775
    assert stat.S_IMODE(old_track.stat().st_mode) == 0o600
    assert stat.S_IMODE(old_cover.stat().st_mode) == 0o600
    mode = service.settings.file_mode
    assert stat.S_IMODE(Path(one.path).stat().st_mode) == mode
    assert stat.S_IMODE((folder / "cover.jpg").stat().st_mode) == mode


def test_a_folder_a_tape_s_import_made_is_readable_by_what_serves_the_library(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet = beet_of(service)
    original = beet._import

    def under_a_umask(args: tuple[str, ...]) -> Result:
        said = original(args)
        (tmp_path / "lib" / args[args.index("--search-id") + 1]).chmod(0o700)
        return said

    beet._import = under_a_umask  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    want = service.settings.dir_mode
    folder = Path(songs(service, tape)[0].path).parent
    assert stat.S_IMODE(folder.stat().st_mode) & want == want


# ---------------------------------------------------------------- what beets left out


def embeds_of(service: Service) -> list[tuple[str, ...]]:
    return [c for c in beet_of(service).calls if "embedart" in c]


def test_a_song_joining_an_album_with_art_gets_it_embedded_alone_with_modes_restored(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.art = "/lib/rel-1/cover.jpg"
    assert not file_it(app, tape, service)["error"]
    one, two = songs(service, tape)
    override = str(Path(service.state_dir) / IMP.OVERRIDE_FILE)
    assert embeds_of(service) == [
        (
            "beet",
            "--config",
            override,
            "embedart",
            "-y",
            "-f",
            runner.art,
            f"path:{s.path}",
        )
        for s in (one, two)
    ]
    assert one.filing_note == ""
    assert stat.S_IMODE(Path(one.path).stat().st_mode) == service.settings.file_mode


def test_a_song_with_art_already_is_not_rewritten(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.art = "/lib/cover.jpg"
    original = runner._import

    def fetched_it(args: tuple[str, ...]) -> Result:
        answer = original(args)
        runner.pictured |= {r["path"] for r in runner.rows}
        return answer

    runner._import = fetched_it  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    assert embeds_of(service) == []


def test_an_album_with_no_art_has_nothing_to_embed_and_nothing_to_say(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    assert embeds_of(service) == []
    assert [s.filing_note for s in songs(service, tape)] == ["", ""]


def test_without_the_embedart_plugin_a_joining_song_is_left_as_the_first_was(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.art, runner.loaded = "/lib/cover.jpg", "fetchart"
    assert not file_it(app, tape, service)["error"]
    assert embeds_of(service) == []


@pytest.mark.parametrize("how", ["embed_fails", "embed_lies"])
def test_art_that_did_not_embed_stays_filed_and_says_so(
    service: Service, how: str
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.art = "/lib/cover.jpg"
    setattr(runner, how, True)
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FILED
    assert "art did not embed" in one.filing_note


def test_a_library_that_cannot_say_what_art_it_has_is_a_note_not_a_failure(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    original = beet_of(service).run

    def locked(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "$artpath" in argv:
            return Result(tuple(argv), 1, b"", b"database is locked\n")
        return original(argv, stdin=stdin, timeout=timeout)

    beet_of(service).run = locked  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FILED
    assert "database is locked" in one.filing_note


def test_a_recovered_song_gets_the_art_a_restart_kept_from_it(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.dies.add("rel-1")
    killed_midway(service, tape)
    runner.dies.clear()
    runner.art = "/lib/rel-1/cover.jpg"
    assert not file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FILED
    assert f"path:{songs(service, tape)[0].path}" in [c[-1] for c in embeds_of(service)]


def test_an_owned_song_s_art_is_left_alone(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    where = runner.owns("One", mb_trackid="rec-One")
    runner.art = "/lib/cover.jpg"
    file_it(app, tape, service)
    assert f"path:{where}" not in [c[-1] for c in embeds_of(service)]


def gains_of(service: Service) -> list[tuple[str, ...]]:
    return [c for c in beet_of(service).calls if "replaygain" in c]


def test_from_list_songs_get_their_gain_one_file_at_a_time_with_modes_restored(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    assert not file_it(app, tape, service)["error"]
    one, two = songs(service, tape)
    override = str(Path(service.state_dir) / IMP.OVERRIDE_FILE)
    assert gains_of(service) == [
        ("beet", "--config", override, "replaygain", "-f", f"path:{s.path}")
        for s in (one, two)
    ]
    assert (one.filing_note, two.filing_note) == ("", "")
    assert stat.S_IMODE(Path(one.path).stat().st_mode) == service.settings.file_mode


def test_a_song_whose_import_wrote_its_gain_is_not_measured_again(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    assert gains_of(service) == []


def test_a_from_list_song_with_no_gain_plugin_says_so(service: Service) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    beet_of(service).loaded = "fetchart"
    assert not file_it(app, tape, service)["error"]
    assert gains_of(service) == []
    for song in songs(service, tape):
        assert song.filing == F.FILED
        assert "no ReplayGain" in song.filing_note


def test_a_pinned_song_is_not_told_off_for_a_plugin_nobody_loaded(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    runner.loaded = "fetchart"
    original = runner._import

    def ungained(args: tuple[str, ...]) -> Result:
        answer = original(args)
        runner.gained.clear()
        return answer

    runner._import = ungained  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    assert [s.filing_note for s in songs(service, tape)] == ["", ""]


def test_a_gain_that_failed_stays_filed_and_says_so(service: Service) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    beet_of(service).gain_fails = True
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FILED
    assert "ReplayGain did not reach this file" in one.filing_note
    assert "Fatal replay gain error" in one.filing_note


# ------------------------------------------------------------------------- the gate


def test_filing_is_refused_until_the_tape_is_cut(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    settle_all(app, tape, service)
    refused = post(app, f"/api/tapes/{tape}/file", service)
    assert refused.status == 409
    assert "song 1 (One), song 2 (Two): not cut" in refused.json()["error"]
    assert imports_of(service) == []


def test_filing_is_refused_while_a_song_is_unsettled(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    refused = post(app, f"/api/tapes/{tape}/file", service)
    assert refused.status == 409
    assert "no settled release" in refused.json()["error"]


def test_a_boundary_moved_after_the_cut_refuses_filing_until_it_is_cut_again(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    url = f"/api/tapes/{tape}/songs/2/edges"
    current = songs(service, tape)[1]
    body = seeing(service, tape, 2, current.start, (current.end or 0.0) - 0.5)
    assert post(app, url, service, body).status == 200
    assert post(app, f"/api/tapes/{tape}/file", service).status == 409


def test_a_skipped_song_is_neither_cut_nor_filed_and_stays_named(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    settle_all(app, tape, service)
    assert post(app, f"/api/tapes/{tape}/songs/2/skip", service).status == 200
    assert cut(app, tape, service).status == 202
    job = file_it(app, tape, service)
    assert not job["error"]
    assert job["skipped"] == ["song 2 (Two)"]
    assert [n for _, n in beet_of(service).imports] == [["01 One.flac"]]
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert [o["state"] for o in view["filing"]] == [F.FILED, TP.SKIPPED]
    assert view["all_filed"]


def test_the_job_reads_the_gate_again(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    with pytest.raises(ValueError, match="no settled release"):
        TJ.file_songs(
            service.runner,
            IMP.Beets(),
            service.layout,
            tape,
            a_job(),
            0.0,
            library="/lib",
            file_mode=0o664,
            dir_mode=0o775,
        )
    assert imports_of(service) == []


def test_filing_needs_a_library(service: Service) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    service.settings = Settings(library="", importer="beets")
    refused = post(app, f"/api/tapes/{tape}/file", service)
    assert refused.status == 409
    assert "library" in refused.json()["error"]


def test_filing_needs_beets(service: Service) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    service.settings = Settings(library="/lib", importer="tagger")
    refused = post(app, f"/api/tapes/{tape}/file", service)
    assert refused.status == 409
    assert "needs beets" in refused.json()["error"]


def test_the_view_gates_the_button_and_lists_failures_first(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    first = get(app, f"/api/tapes/{tape}", service).json()
    assert first["file_refusal"] == ""
    assert not first["all_filed"]
    assert [o["state"] for o in first["filing"]] == [TP.PENDING, TP.PENDING]

    beet_of(service).fails["rel-2"] = b"error: not today\n"
    file_it(app, tape, service)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert [(o["number"], o["state"]) for o in view["filing"]] == [
        (2, F.FAILED),
        (1, F.FILED),
    ]
    assert "not today" in view["filing"][0]["note"]
    assert view["filing"][1]["path"]


# ----------------------------------------------- a filed song and what can change it


@pytest.mark.parametrize("state", [F.FILED, F.ALREADY])
def test_a_song_in_the_library_keeps_the_release_it_was_filed_as(
    service: Service, state: str
) -> None:
    """The choice stayed live on a filed song, and the next rebuild looked for a
    recording the library's copy did not have."""
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    current = T.read(service.layout, tape)
    T.save(service.layout, TP._changed(current, 1, filing=state))
    before = T.read(service.layout, tape)
    for action, body in (("release", {"release": "rel-other"}), ("from-list", {})):
        answer = post(app, f"/api/tapes/{tape}/songs/1/{action}", service, body)
        assert answer.status == 409, action
        assert "already in the library" in answer.json()["error"]
    assert T.read(service.layout, tape) == before


def test_a_song_whose_filing_failed_can_still_be_changed(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    beet_of(service).fails["rel-1"] = b"no such release"
    file_it(app, tape, service)
    assert songs(service, tape)[0].filing == F.FAILED
    answer = post(app, f"/api/tapes/{tape}/songs/1/from-list", service)
    assert answer.status == 200, answer.json()
    assert F.choice_of(songs(service, tape)[0]) is None


def test_a_song_not_confirmed_keeps_its_release_until_a_read_back_answers(
    service: Service,
) -> None:
    """Another choice would look for another recording, find no row and import a
    second copy of what the first run may have filed."""
    app = build(service)
    tape = pinned(app, service, two="rel-1")
    runner = beet_of(service)
    locked_after_the_import(service)
    file_it(app, tape, service)
    before = T.read(service.layout, tape)
    assert before.songs[0].filing == F.UNKNOWN
    for action, body in (("release", {"release": "rel-other"}), ("from-list", {})):
        answer = post(app, f"/api/tapes/{tape}/songs/1/{action}", service, body)
        assert answer.status == 409, action
        assert "until a read-back answers" in answer.json()["error"]
    assert T.read(service.layout, tape) == before
    imported = len(runner.imports)
    runner.ls_fails = False
    assert not file_it(app, tape, service)["error"]
    assert songs(service, tape)[0].filing == F.FILED
    assert len(runner.imports) == imported


def test_a_boundary_on_a_filed_song_is_refused_and_it_is_never_filed_twice(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    before = T.read(service.layout, tape)
    imports = len(beet_of(service).imports)
    current = before.songs[1]
    body = seeing(service, tape, 2, current.start, (current.end or 0.0) - 0.5)
    refused = post(app, f"/api/tapes/{tape}/songs/2/edges", service, body)
    assert refused.status == 409
    assert "already in the library" in refused.json()["error"]
    assert T.read(service.layout, tape) == before
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    answer = post(app, f"/api/tapes/{tape}/file", service)
    assert answer.status == 202
    assert not job_of(app, tape, service)["error"]
    two = songs(service, tape)[1]
    assert (two.filing, two.path) == (F.FILED, current.path)
    assert len(beet_of(service).imports) == imports


def test_a_new_list_after_beets_removes_the_copies_clears_filing_and_files_them_again(
    service: Service,
) -> None:
    """The advice a refusal gives has to be true: with the copies removed with beets, a
    new list goes through to filing them again."""
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    before = len(beet_of(service).imports)
    filed_at = songs(service, tape)[0].path
    for song in songs(service, tape):
        Path(song.path).unlink()
        beet_of(service).run(("beet", "remove", "-f", f"path:{song.path}"))
    assert send_csv(app, tape, service, TWO, confirm=True).status == 200
    assert {s.filing for s in songs(service, tape)} == {TP.PENDING}
    assert post(app, f"/api/tapes/{tape}/process", service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    settle_all(app, tape, service)
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    assert one.filing == F.FILED
    assert len(beet_of(service).imports) > before
    assert Path(filed_at).name == Path(one.path).name


@pytest.mark.parametrize("how", ["csv", "hand"])
def test_a_confirmed_new_list_dropped_the_lock_on_a_filed_song_and_is_refused(
    service: Service, how: str
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    before = songs(service, tape)
    gone = before[0]
    Path(gone.path).unlink()
    beet_of(service).run(("beet", "remove", "-f", f"path:{gone.path}"))
    rows = [{"title": "New", "artist": "A Band", "album": "A", "length": "0:05"}]
    said = (
        send_csv(app, tape, service, TWO, confirm=True)
        if how == "csv"
        else save_list(app, tape, service, rows, confirm=True)
    )
    assert said.status == 409
    assert "song 2 (Two)" in said.json()["error"]
    assert "song 1" not in said.json()["error"]
    assert "remove that copy with beets" in said.json()["error"]
    assert songs(service, tape) == before


def test_a_new_list_is_refused_when_beets_cannot_say_a_copy_is_gone(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    beet_of(service).ls_fails = True
    assert send_csv(app, tape, service, TWO, confirm=True).status == 409


def beets_asked(service: Service) -> list[tuple[str, ...]]:
    """Every `beet ls` from here on, without changing what it answers."""
    runner = beet_of(service)
    original = runner.run
    seen: list[tuple[str, ...]] = []

    def run(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "ls" in argv:
            seen.append(tuple(str(a) for a in argv))
        return original(argv, stdin=stdin, timeout=timeout)

    runner.run = run  # type: ignore[method-assign]
    return seen


def test_a_new_list_asked_beets_once_for_every_filed_song_on_the_request_thread(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    one, two = songs(service, tape)
    Path(one.path).unlink()
    beet_of(service).run(("beet", "remove", "-f", f"path:{one.path}"))
    asked = beets_asked(service)
    said = send_csv(app, tape, service, TWO, confirm=True)
    assert len(asked) == 1, asked
    assert f"path:{one.path}" in asked[0] and f"path:{two.path}" in asked[0]
    assert said.status == 409 and "song 2 (Two)" in said.json()["error"]
    assert "song 1" not in said.json()["error"]


def test_a_new_list_with_a_boundary_set_by_ear_asked_beets_before_refusing(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    current = T.read(service.layout, tape)
    set_by_ear = (replace(current.songs[0], set_by=TP.EAR), current.songs[1])
    T.save(service.layout, replace(current, songs=set_by_ear))
    asked = beets_asked(service)
    said = send_csv(app, tape, service, TWO, confirm=True)
    assert said.status == 409 and "set by ear" in said.json()["error"]
    assert asked == []


def test_a_new_list_is_answered_plainly_when_beets_runs_out_of_time(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    before = songs(service, tape)
    timing_out(service, "ls")
    said = send_csv(app, tape, service, TWO, confirm=True)
    assert said.status == 503
    assert "did not answer in time" in said.json()["error"]
    assert songs(service, tape) == before


def test_a_new_list_cleared_not_confirmed_and_could_import_a_second_copy(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    current = T.read(service.layout, tape)
    T.save(service.layout, F.recorded(current, 1, F.UNKNOWN, "", "could not ask"))
    said = send_csv(app, tape, service, TWO, confirm=True)
    assert said.status == 409
    assert "press File again" in said.json()["error"]
    assert songs(service, tape)[0].filing == F.UNKNOWN


def test_a_release_held_by_a_lost_import_is_freed_by_filing_again(
    service: Service,
) -> None:
    """The server died with the song at beets: the choice waits, and File answers."""
    app = build(service)
    tape = pinned(app, service)
    current = T.read(service.layout, tape)
    T.save(
        service.layout,
        replace(
            current,
            songs=tuple(replace(s, import_begun=True) for s in current.songs),
        ),
    )
    url = f"/api/tapes/{tape}/songs/1/from-list"
    refused = post(app, url, service)
    assert refused.status == 409
    assert "press File again" in refused.json()["error"]
    assert not file_it(app, tape, service)["error"]
    assert {s.import_begun for s in songs(service, tape)} == {False}
    assert {s.filing for s in songs(service, tape)} == {F.FILED}


def lost_at_beets(service: Service, tape: str) -> None:
    current = T.read(service.layout, tape)
    two = replace(current.songs[1], filing=TP.PENDING, path="", import_begun=True)
    T.save(service.layout, replace(current, songs=(current.songs[0], two)))


def test_a_song_lost_at_beets_cannot_be_moved_and_file_again_finds_the_library_s_copy(
    service: Service,
) -> None:
    """The server died at the import: a move let through here was then answered by the
    old row, and the page showed a current cut while the library held the old audio."""
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    filed = songs(service, tape)[1]
    imports = len(beet_of(service).imports)
    lost_at_beets(service, tape)
    body = seeing(service, tape, 2, filed.start, (filed.end or 0.0) - 0.2)
    refused = post(app, f"/api/tapes/{tape}/songs/2/edges", service, body)
    assert refused.status == 409
    assert "press File again" in refused.json()["error"]
    assert not file_it(app, tape, service)["error"]
    two = songs(service, tape)[1]
    assert (two.filing, two.path) == (F.FILED, filed.path)
    assert not two.import_begun
    assert len(beet_of(service).imports) == imports


def test_a_song_lost_at_beets_with_nothing_in_the_library_files_and_is_not_a_dead_end(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    lost_at_beets(service, tape)
    assert not file_it(app, tape, service)["error"]
    two = songs(service, tape)[1]
    assert (two.filing, two.import_begun) == (F.FILED, False)
    assert two.path


def test_the_advice_on_a_filed_song_s_boundary_can_be_followed_to_a_new_cut_of_it(
    service: Service,
) -> None:
    """It told a person to load the list again, which is refused once a boundary is
    theirs. This follows what it says now, from the refusal to the new file."""
    app = build(service)
    tape = placed(app, service)
    settle_all(app, tape, service)
    url = f"/api/tapes/{tape}/songs/2/edges"
    assert post(app, url, service, seeing(service, tape, 2, 6.5, 12.0)).status == 200
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    pin(service, tape, 1, "rel-1")
    pin(service, tape, 2, "rel-2")
    assert not file_it(app, tape, service)["error"]
    filed = songs(service, tape)[1]

    refused = post(app, url, service, seeing(service, tape, 2, 6.5, 11.0))
    assert refused.status == 409
    said = refused.json()["error"]
    assert "remove that copy with beets" in said
    assert "remove the tape and start it over" in said
    relisted = send_csv(app, tape, service, TWO)
    assert relisted.status == 409
    assert "remove the tape" in relisted.json()["error"]

    beet = beet_of(service)
    imports = len(beet.imports)
    Path(filed.path).unlink()
    beet.rows = [r for r in beet.rows if r["path"] != filed.path]
    assert post(app, f"/api/tapes/{tape}/remove", service).status == 200

    again = placed(app, service)
    settle_all(app, again, service)
    url = f"/api/tapes/{again}/songs/2/edges"
    assert post(app, url, service, seeing(service, again, 2, 6.5, 11.0)).status == 200
    assert cut(app, again, service).status == 202
    assert not job_of(app, again, service)["error"]
    pin(service, again, 1, "rel-1")
    pin(service, again, 2, "rel-2")
    assert not file_it(app, again, service)["error"]
    two = songs(service, again)[1]
    assert (two.filing, two.start, two.end) == (F.FILED, 6.5, 11.0)
    assert Path(two.path).is_file()
    one = songs(service, again)[0]
    assert one.filing == F.ALREADY, "the copy nobody removed is not filed twice"
    assert len(beet.imports) == imports + 1


# ------------------------------------------------------------------- a missing cover


def test_a_filed_song_with_no_cover_says_so_and_one_with_a_cover_does_not(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    said = get(app, f"/api/tapes/{tape}", service).json()["filing"]
    assert all(o["no_cover"] for o in said)

    other = build(service)
    beet_of(service).rows.clear()
    beet_of(service).art = "/lib/rel-1/cover.jpg"
    tape = pinned(other, service, one="rel-1", two="rel-1")
    assert not file_it(other, tape, service)["error"]
    assert not any(s.no_cover for s in songs(service, tape))


def test_a_library_that_cannot_say_is_not_told_there_is_no_cover(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    original = beet_of(service).run

    def locked(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "$artpath" in argv:
            return Result(tuple(argv), 1, b"", b"database is locked\n")
        return original(argv, stdin=stdin, timeout=timeout)

    beet_of(service).run = locked  # type: ignore[method-assign]
    assert not file_it(app, tape, service)["error"]
    assert not any(s.no_cover for s in songs(service, tape))


def test_a_cover_question_that_timed_out_is_left_unsaid_and_stops_no_job(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    timing_out(service, "$artpath")
    beets = IMP.Beets.with_override(service.state_dir)
    library = service.settings.library
    TJ.covers_known(service.runner, beets, service.layout, tape, library)
    assert not any(s.no_cover for s in songs(service, tape))


@pytest.mark.parametrize("word", ["$album_id", "$artpath", "modify"])
def test_a_cover_upload_that_timed_out_is_a_plain_reason_and_not_a_500(
    service: Service, word: str
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    timing_out(service, word)
    answer = covered(service, tape)
    assert answer.status == 503
    assert "did not answer in time" in answer.json()["error"]


def covered(service: Service, tape: str, number: int = 1, body: bytes = b"an image"):  # type: ignore[no-untyped-def]
    app = build(service)
    url = f"/api/tapes/{tape}/songs/{number}/cover"
    return post_raw(app, url, service, body)


def post_raw(app, url: str, service: Service, body: bytes):  # type: ignore[no-untyped-def]
    return app.dispatch(
        H.Request.of("POST", url, headers=signed_in(service), body=body)
    )


def test_one_upload_embeds_and_sets_the_album_s_art_for_all_its_songs(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    one, two = songs(service, tape)
    answer = covered(service, tape)
    assert answer.status == 200, answer.text
    assert answer.json()["cover"]["embedded"] == 2
    folder = Path(one.path).parent
    assert (folder / "cover.jpg").is_file()
    assert {one.path, two.path} <= beet_of(service).pictured
    assert beet_of(service).art == str(folder / "cover.jpg")
    asked = next(c for c in beet_of(service).calls if "modify" in c)
    assert "-M" in asked and "-W" in asked and "-a" in asked
    assert not any(s.no_cover for s in songs(service, tape))
    assert stat.S_IMODE(Path(one.path).stat().st_mode) == service.settings.file_mode


def test_a_cover_for_a_shared_folder_leaves_the_other_album_alone(
    service: Service,
) -> None:
    """A song added beside an album held was promised those tracks do not change."""
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-2")
    assert not file_it(app, tape, service)["error"]
    one = songs(service, tape)[0]
    folder = Path(one.path).parent
    neighbour = folder / "99 Neighbour.flac"
    neighbour.write_bytes(b"theirs")
    existing = folder / "cover.jpg"
    existing.write_bytes(b"their cover")
    answer = covered(service, tape)
    assert answer.status == 200, answer.text
    assert str(neighbour) not in beet_of(service).pictured
    assert existing.read_bytes() == b"their cover"
    assert beet_of(service).art.startswith(str(folder / "cover-"))


def test_a_cover_changed_the_mode_of_a_neighbour_s_file_and_touches_only_its_own(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    one, two = songs(service, tape)
    folder = Path(one.path).parent
    neighbour = folder / "99 Neighbour.flac"
    neighbour.write_bytes(b"theirs")
    neighbour.chmod(0o600)
    their_cover = folder / "cover.jpg"
    their_cover.write_bytes(b"their cover")
    their_cover.chmod(0o640)
    folder.chmod(0o2775)
    assert covered(service, tape).status == 200
    mode = service.settings.file_mode
    assert stat.S_IMODE(neighbour.stat().st_mode) == 0o600
    assert stat.S_IMODE(their_cover.stat().st_mode) == 0o640
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2775
    assert stat.S_IMODE(Path(one.path).stat().st_mode) == mode
    assert stat.S_IMODE(Path(two.path).stat().st_mode) == mode
    assert stat.S_IMODE(Path(beet_of(service).art).stat().st_mode) == mode


def test_a_cover_that_could_not_set_a_mode_is_still_a_cover_and_says_so(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The art was in the files when the chmod failed: a 500 there left it embedded
    with the song still saying it had none."""
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]

    def refused(*_: object) -> None:
        raise PermissionError("not the owner")

    monkeypatch.setattr(Path, "chmod", refused)
    answer = covered(service, tape)
    assert answer.status == 200, answer.text
    assert "its modes could not be set" in answer.json()["cover"]["failed"][0]
    assert not any(s.no_cover for s in songs(service, tape))


def no_embedding_in(service: Service, path: str) -> None:
    runner = beet_of(service)
    original = runner.run

    def refused(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if (
            args[0] == "metaflac"
            and args[-1] == path
            and any(a.startswith("--import-picture-from=") for a in args)
        ):
            raise OSError("read-only file system")
        return original(argv, stdin=stdin, timeout=timeout)

    runner.run = refused  # type: ignore[method-assign]


def test_a_cover_only_some_files_took_could_not_be_tried_again(
    service: Service,
) -> None:
    """The album row was given the art, so the next upload was told it had one already,
    and a rebuild then stopped offering it for the file still without."""
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    no_embedding_in(service, songs(service, tape)[1].path)
    answer = covered(service, tape)
    assert answer.status == 200, answer.text
    assert answer.json()["cover"]["failed"]
    assert not beet_of(service).art
    assert songs(service, tape)[0].no_cover is True

    runner = beet_of(service)
    runner.run = type(runner).run.__get__(runner)  # type: ignore[method-assign]
    again = covered(service, tape)
    assert again.status == 200, again.text
    assert again.json()["cover"]["embedded"] == 2
    assert beet_of(service).art
    assert not any(s.no_cover for s in songs(service, tape))


def test_an_interrupted_song_found_again_was_offered_a_cover_upload_then_refused_it(
    service: Service,
) -> None:
    """Its outcome called it already there, so the row offered Upload and the route said
    it was not this tape's to change. The tape's own import made it."""
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    beet_of(service).dies.add("rel-1")
    with pytest.raises(Gone):
        file_it(app, tape, service)
    beet_of(service).dies.clear()
    assert not file_it(app, tape, service)["error"]
    view = get(app, f"/api/tapes/{tape}", service).json()
    offered = [o for o in view["filing"] if o["no_cover"]]
    assert offered and all(o["state"] == F.FILED for o in offered)
    assert covered(service, tape).status == 200


def test_an_old_document_s_recovered_song_is_not_offered_what_the_route_refuses(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    current = T.read(service.layout, tape)
    was = replace(current.songs[0], filing=F.ALREADY, no_cover=True)
    T.save(service.layout, replace(current, songs=(was, *current.songs[1:])))
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert not next(o for o in view["filing"] if o["number"] == 1)["no_cover"]


def test_a_folder_a_cover_cannot_be_written_in_is_a_plain_refusal_and_not_a_500(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]

    def refused(*_: object, **__: object) -> None:
        raise PermissionError("no write access")

    monkeypatch.setattr("ripdoctor.web.routes.art.ART.install", refused)
    answer = covered(service, tape)
    assert answer.status == 409
    assert "the cover was not set: no write access" in answer.json()["error"]
    assert all(s.no_cover for s in songs(service, tape))


def test_an_image_that_is_not_one_is_refused_and_the_song_still_says_no_cover(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    original = beet_of(service).run

    def no_image(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if argv[0] == "ffprobe":
            return Result(tuple(argv), 1, b"", b"")
        return original(argv, stdin=stdin, timeout=timeout)

    beet_of(service).run = no_image  # type: ignore[method-assign]
    assert covered(service, tape).status == 400
    assert all(s.no_cover for s in songs(service, tape))
    assert not beet_of(service).art


def test_a_song_this_tape_only_found_could_take_a_cover_and_is_refused(
    service: Service,
) -> None:
    """Its album is the person's own, and the tape has no business rewriting it."""
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    current = T.read(service.layout, tape)
    found = replace(current.songs[0], filing=F.ALREADY)
    T.save(service.layout, replace(current, songs=(found, current.songs[1])))
    answer = covered(service, tape)
    assert answer.status == 409
    assert "not filed by this tape" in answer.json()["error"]
    assert not beet_of(service).art


def test_a_song_with_a_cover_could_be_given_another_and_is_refused(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    T.save(service.layout, F.with_cover_known(T.read(service.layout, tape), ()))
    answer = covered(service, tape)
    assert answer.status == 409
    assert "not missing a cover" in answer.json()["error"]


def test_a_file_beets_lists_outside_the_library_could_be_rewritten_and_is_refused(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = pinned(app, service, one="rel-1", two="rel-1")
    assert not file_it(app, tape, service)["error"]
    away = tmp_path / "elsewhere" / "other.flac"
    away.parent.mkdir()
    away.write_bytes(b"not the library's")
    row = beet_of(service).rows[1]
    row["path"] = str(away)
    answer = covered(service, tape)
    assert answer.status == 409
    assert "outside the library" in answer.json()["error"]
    assert str(away) not in beet_of(service).pictured


def test_a_song_not_filed_cannot_take_a_cover(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert covered(service, tape).status == 409


def test_writing_the_playlist_again_tells_a_song_filed_before_covers_were_asked_about(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    assert not file_it(app, tape, service)["error"]
    saved = T.read(service.layout, tape)
    T.save(service.layout, F.with_cover_known(saved, ()))
    assert not any(s.no_cover for s in songs(service, tape))
    assert post(app, f"/api/tapes/{tape}/playlist", service).status == 202
    assert all(s.no_cover for s in songs(service, tape))


def test_an_old_document_with_no_word_on_covers_loads() -> None:
    song = TP.Song(number=1, title="A", artist="B", album="C", length=1.0).to_dict()
    del song["no_cover"]
    assert TP.Song.from_dict(song).no_cover is False


# ------------------------------------------------- a library gone from under its mount


def off_its_mount(service: Service) -> Path:
    """Rows beets still holds, and a root with nothing in it. Hand back where the
    library went."""
    root = Path(service.settings.library)
    beet_of(service).owns("Another", album="Elsewhere", length="9:00")
    aside = root.with_name("lib-aside")
    root.rename(aside)
    root.mkdir()
    return aside


def test_filing_onto_an_empty_mount_point_once_said_filed_and_wrote_a_playlist(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    aside = off_its_mount(service)
    job = file_it(app, tape, service)
    assert "did not file" in job["error"]
    assert imports_of(service) == []
    assert [s.filing for s in songs(service, tape)] == [F.FAILED, F.FAILED]
    assert all("off its mount" in s.filing_note for s in songs(service, tape))
    assert not any(Path(service.settings.library).iterdir())

    Path(service.settings.library).rmdir()
    aside.rename(service.settings.library)
    assert not file_it(app, tape, service)["error"]
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]


def test_a_song_whose_import_began_stays_unanswered_when_the_mount_has_gone(
    service: Service,
) -> None:
    """Its row may be there once the mount is back, so the mark that keeps a retry from
    importing it twice has to outlast this."""
    app = build(service)
    tape = pinned(app, service)
    T.save(service.layout, F.begun(T.read(service.layout, tape), {1}))
    off_its_mount(service)
    file_it(app, tape, service)
    one, two = songs(service, tape)
    assert one.import_begun and one.filing == F.UNKNOWN
    assert "off its mount" in one.filing_note
    assert two.filing == F.FAILED and not imports_of(service)


def test_a_truly_empty_library_files_its_first_album(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    Path(service.settings.library).mkdir(exist_ok=True)
    assert not any(Path(service.settings.library).iterdir())
    assert not beet_of(service).rows
    assert not file_it(app, tape, service)["error"]
    assert [s.filing for s in songs(service, tape)] == [F.FILED, F.FILED]
