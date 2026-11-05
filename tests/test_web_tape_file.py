"""Filing a tape over the wire, by the argv and stdin beets is sent, the rows it
leaves and the document that records them. ADR-063."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from ripdoctor.audio.runner import Result
from ripdoctor.config.settings import Settings
from ripdoctor.core import filing as F
from ripdoctor.core import match as M
from ripdoctor.core import tape as TP
from ripdoctor.integrations import importer as IMP
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from ripdoctor.work import tape_jobs as TJ
from ripdoctor.work.jobs import Job
from tests.test_web_routes import a_service, get, post
from tests.test_web_tape_cut import Cutting, cut, directory, job_of, placed, settle_all
from tests.test_web_tapes import a_tape_runner

TG_TITLE = "TITLE"


class Gone(BaseException):
    """The machine going away: nothing in the program may catch this."""


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
        self.ls_fails = False

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
        if args[0] == "metaflac":
            if "--list" in args and args[-1] in self.pictured:
                return Result(args, 0, b"METADATA block #2\n", b"")
            for arg in args[1:-1]:
                if arg.startswith("--set-tag="):
                    key, _, value = arg.removeprefix("--set-tag=").partition("=")
                    self.tags.setdefault(args[-1], {})[key] = value
        if args[0] != "beet":
            return answer
        if "version" in args:
            return Result(args, 0, f"plugins: {self.loaded}\n".encode(), b"")
        if "$artpath" in args:
            return Result(args, 0, f"{self.art or 'None'}\n".encode(), b"")
        if "embedart" in args:
            return self._embed(args)
        if "ls" in args:
            return self._ls(args)
        if "import" in args:
            return self._import(args)
        if "remove" in args:
            return self._remove(args)
        return answer

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
        }

    def _embed(self, args: tuple[str, ...]) -> Result:
        if self.embed_fails:
            return Result(args, 1, b"", b"error: the art would not take\n")
        where = Path(args[-1].removeprefix("path:"))
        if not self.embed_lies:
            self.pictured.add(str(where))
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
        terms = [t.split(":", 1) for t in args[args.index("-f") + 2 :]]
        lines = [
            "\x1f".join(
                r.get(k, "") for k in ("title", "artist", "album", "length", "path")
            )
            for r in self.rows
            if all(v.casefold() in r.get(k, "").casefold() for k, v in terms)
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
    assert runner.fed[at] == b"A\nM\nA\n"
    assert sorted(next(iter(runner.tags.values()))) == ["TITLE", "TRACKNUMBER"]


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
    sent = [f for c, f in zip(runner.calls, runner.fed, strict=True) if "import" in c]
    assert sent and all(f == b"A\nM\nA\n" for f in sent)
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


def test_a_song_owned_by_title_and_length_with_no_id_is_not_imported(
    service: Service,
) -> None:
    app = build(service)
    tape = pinned(app, service)
    where = beet_of(service).owns("One", length="0:06")
    file_it(app, tape, service)
    assert ["01 One.flac"] not in [n for _, n in beet_of(service).imports]
    one = songs(service, tape)[0]
    assert (one.filing, one.path) == (F.ALREADY, str(where))


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
    assert runner.fed[runner.calls.index(argv)] == b"A\nM\nA\n"
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


def test_a_from_list_album_already_there_says_it_made_a_second_row(
    service: Service,
) -> None:
    app = build(service)
    tape = cut_tape(app, service)
    beet_of(service).owns(
        "Another", album="A Record", albumartist="A Band", length="9:00"
    )
    assert not file_it(app, tape, service)["error"]
    assert all("two album rows" in s.filing_note for s in songs(service, tape))


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
    assert (one.filing, two.filing) == (F.FILED, F.ALREADY)
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
    assert songs(service, tape)[0].filing == F.ALREADY
    assert not songs(service, tape)[0].import_begun
    for found in (tmp_path / "lib" / "rel-1").iterdir():
        assert stat.S_IMODE(found.stat().st_mode) == service.settings.file_mode


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
        for found in (tmp_path / "lib" / release).iterdir():
            assert stat.S_IMODE(found.stat().st_mode) == service.settings.file_mode


def test_an_owned_album_s_modes_are_left_alone(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    where = beet_of(service).owns("One", mb_trackid="rec-One")
    where.chmod(0o600)
    file_it(app, tape, service)
    assert stat.S_IMODE(where.stat().st_mode) == 0o600


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
    assert songs(service, tape)[0].filing == F.ALREADY
    assert f"path:{songs(service, tape)[0].path}" in [c[-1] for c in embeds_of(service)]


def test_an_owned_song_s_art_is_left_alone(service: Service) -> None:
    app = build(service)
    tape = pinned(app, service)
    runner = beet_of(service)
    where = runner.owns("One", mb_trackid="rec-One")
    runner.art = "/lib/cover.jpg"
    file_it(app, tape, service)
    assert f"path:{where}" not in [c[-1] for c in embeds_of(service)]


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
    body = {"start": current.start, "end": (current.end or 0.0) - 0.5}
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
