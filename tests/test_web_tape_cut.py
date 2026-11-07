"""Cutting a tape over the wire, by the argv it sends and the files it leaves.
ADR-063."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner, Result
from ripdoctor.core import match as M
from ripdoctor.core import tape as TP
from ripdoctor.store import tapes as T
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from ripdoctor.work import tape_jobs as TJ
from ripdoctor.work.jobs import Job
from tests.test_web_routes import a_service, get, post
from tests.test_web_tapes import (
    a_new_tape,
    a_tape_runner,
    process,
    seeing,
    send_csv,
    upload,
)

TWO = (
    "playlist,owned,position,title,artist,album,duration\r\n"
    "Mix,no,1,One,A Band,A Record,0:05\r\n"
    "Mix,no,2,Two,A Band,A Record,0:05\r\n"
)


def is_cut(argv: tuple[str, ...]) -> bool:
    return argv[0] == "ffmpeg" and "-ss" in argv


class Cutting:
    """Around a tape's runner: a cut writes its seek times, so a file says which
    edges it was made from, and `verify` can be told to fail or `ffmpeg` to die."""

    def __init__(self, inner: FakeRunner) -> None:
        self.inner = inner
        self.bad: set[str] = set()
        self.dies_on: str = ""
        self.seen_at_cut: dict[str, bytes | None] = {}

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = tuple(str(a) for a in argv)
        if is_cut(args):
            name = Path(args[-1]).name
            if self.dies_on and self.dies_on in name:
                raise RuntimeError("the machine went away")
            real = Path(args[-1]).parent.parent / T.CUTS / name
            self.seen_at_cut[name] = real.read_bytes() if real.exists() else None
        if args[0] == "flac" and any(b in args[-1] for b in self.bad):
            self.inner.calls.append(args)
            return Result(args, 1, b"", b"")
        answer = self.inner.run(argv, stdin=stdin, timeout=timeout)
        if is_cut(args):
            Path(args[-1]).write_bytes(
                f"{args[args.index('-ss') + 1]}-{args[args.index('-to') + 1]}".encode()
            )
        return answer


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return a_service(tmp_path, runner=Cutting(a_tape_runner()))  # type: ignore[arg-type]


def cuts_sent(service: Service) -> list[tuple[str, ...]]:
    return [c for c in service.runner.calls if is_cut(c)]  # type: ignore[attr-defined]


def job_of(app, tape: str, service: Service) -> dict:  # type: ignore[no-untyped-def]
    return get(app, f"/api/job/{T.key_of(tape)}", service).json()


def placed(app, service: Service, text: str = TWO) -> str:  # type: ignore[no-untyped-def]
    tape = a_new_tape(app, service)
    assert send_csv(app, tape, service, text).status == 200
    upload(app, tape, service)
    process(app, tape, service)
    return tape


def settle_all(app, tape: str, service: Service) -> None:  # type: ignore[no-untyped-def]
    for song in T.read(service.layout, tape).songs:
        url = f"/api/tapes/{tape}/songs/{song.number}/from-list"
        assert post(app, url, service).status == 200


def ready(app, service: Service) -> str:  # type: ignore[no-untyped-def]
    tape = placed(app, service)
    settle_all(app, tape, service)
    return tape


def cut(app, tape: str, service: Service) -> H.Response:  # type: ignore[no-untyped-def]
    return post(app, f"/api/tapes/{tape}/cut", service)


def set_song_2(app, tape: str, service: Service, start: float, end: float) -> None:  # type: ignore[no-untyped-def]
    """Inside the silence before it, so song 1 has no cause to move or doubt."""
    url = f"/api/tapes/{tape}/songs/2/edges"
    body = seeing(service, tape, 2, start, end)
    assert post(app, url, service, body).status == 200


def directory(service: Service, tape: str) -> Path:
    return T.dir_for(service.layout, tape) / T.CUTS


# ----------------------------------------------------------------------- refused


def test_cut_is_refused_while_a_doubt_is_unheard_and_cuts_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service, TWO.replace("Two,A Band,A Record,0:05", "Two,B,C,0:30"))
    settle_all(app, tape, service)
    refused = cut(app, tape, service)
    assert refused.status == 409
    assert "song 2 (Two)" in refused.json()["error"]
    assert "doubt" in refused.json()["error"]
    assert cuts_sent(service) == []

    assert post(app, f"/api/tapes/{tape}/songs/2/heard", service).status == 200
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]


@pytest.mark.parametrize(("verb", "body"), [("heard", {}), ("skip", {})])
def test_a_page_left_open_cannot_judge_a_song_moved_since(
    service: Service, verb: str, body: dict[str, object]
) -> None:
    """#45: a move sent what it had seen and a verdict did not, so a verdict could land
    on boundaries the person giving it had never looked at."""
    app = build(service)
    tape = placed(app, service, TWO)
    song = T.read(service.layout, tape).songs[1]
    assert song.start is not None and song.end is not None
    url = f"/api/tapes/{tape}/songs/2"
    moved = {"start": song.start, "end": song.end - 0.5}
    seen = {"seen_start": song.start, "seen_end": song.end}
    assert post(app, f"{url}/edges", service, {**moved, **seen}).status == 200
    before = T.read(service.layout, tape)
    refused = post(app, f"{url}/{verb}", service, {**body, **seen})
    assert refused.status == 409 and "has changed since" in refused.json()["error"]
    assert T.read(service.layout, tape) == before
    fresh = {"seen_start": moved["start"], "seen_end": moved["end"]}
    if verb == "skip":
        assert post(app, f"{url}/{verb}", service, {**body, **fresh}).status == 200


def test_a_verdict_is_sent_with_what_the_page_was_looking_at() -> None:
    js = (Path(__file__).parent.parent / "ripdoctor/web/static/app.js").read_text()
    judge = js[js.index("function tapeJudge(") :]
    judge = judge[: judge.index("function zoomBy(")]
    assert "{ seen_start: start, seen_end: end }" in judge
    assert "if (e.status === 409 && tape) showTape(" in judge
    assert judge.count("...seenOf(key)") == 2


def test_cut_goes_ahead_with_albums_unsettled_while_file_refuses(
    service: Service,
) -> None:
    """Cut once refused for want of an album it does not use, and nobody got past it."""
    app = build(service)
    tape = placed(app, service)
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert len(cuts_sent(service)) == 2
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["all_cut"] is True
    assert "no settled release" in view["file_refusal"]
    assert "song 1 (One), song 2 (Two)" in view["file_refusal"]
    refused = post(app, f"/api/tapes/{tape}/file", service)
    assert refused.status == 409
    assert "no settled release" in refused.json()["error"]


def test_the_view_says_why_a_tape_cannot_be_filed_yet(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["cut_refusal"] == ""
    assert "no settled release" in view["file_refusal"]
    assert "not cut" in view["file_refusal"]
    settle_all(app, tape, service)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert "no settled release" not in view["file_refusal"]
    assert "not cut" in view["file_refusal"]


def test_a_doubt_refuses_the_cut_and_the_filing_with_it(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    current = T.read(service.layout, tape)
    doubted = (replace(current.songs[0], verdict=TP.DOUBT), current.songs[1])
    T.save(service.layout, replace(current, songs=doubted))
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert "still in doubt" in view["cut_refusal"]
    assert "still in doubt" in view["file_refusal"]


def test_changing_a_release_after_the_cut_keeps_the_cut_and_files_the_new_one(
    service: Service,
) -> None:
    """A cut is of audio and owes nothing to the album it is filed under."""
    app = build(service)
    tape = placed(app, service)
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    settle_all(app, tape, service)
    choice = M.Choice(
        release="rel-new",
        group="g",
        release_title="B Record",
        album_artist="A Band",
        date="1999",
        status="Official",
        track_title="One",
        number=1,
        disc=1,
    )
    current = T.read(service.layout, tape)
    T.save(service.layout, TP.settle(current, 1, choice))
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["all_cut"] is True
    assert view["file_refusal"] == ""
    assert cut(app, tape, service).status == 202
    done = job_of(app, tape, service)
    assert not done["error"]
    assert done["result"]["cut"] == []
    assert set(done["result"]["kept"]) == {"song 1 (One)", "song 2 (Two)"}


def test_the_job_reads_the_gate_again_rather_than_trusting_the_request(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    current = T.read(service.layout, tape)
    doubted = (replace(current.songs[0], verdict=TP.DOUBT), current.songs[1])
    T.save(service.layout, replace(current, songs=doubted))
    with pytest.raises(ValueError, match="still in doubt"):
        TJ.cut(
            service.runner,
            service.layout,
            tape,
            Job(slug="x", op="cut", started=0.0),
            0.0,
        )
    assert cuts_sent(service) == []


def test_cut_needs_the_audio(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(app, tape, service, TWO)
    assert cut(app, tape, service).status == 409


def test_cut_waits_for_whatever_holds_the_tape(service: Service) -> None:
    app = build(service)
    tape = ready(app, service)
    with service.jobs.claim(T.key_of(tape), "something"):
        assert cut(app, tape, service).status == 409


# -------------------------------------------------------------------------- cuts


def test_a_cut_seeks_exactly_and_re_encodes_from_the_tape(service: Service) -> None:
    app = build(service)
    tape = ready(app, service)
    assert cut(app, tape, service).status == 202
    done = job_of(app, tape, service)
    assert done["op"] == "cut"
    assert not done["error"], done["error"]

    song = T.read(service.layout, tape).songs[0]
    root = T.dir_for(service.layout, tape)
    assert cuts_sent(service)[0] == (
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{song.start:.3f}",
        "-to",
        f"{song.end:.3f}",
        "-i",
        str(root / T.AUDIO),
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        "-frame_size",
        "4096",
        str(root / T.PARTIAL / "01 One.flac"),
    )


def test_every_cut_is_verified_where_it_was_written_before_taking_its_name(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    root = T.dir_for(service.layout, tape)
    tested = [c[-1] for c in service.runner.calls if c[0] == "flac"]  # type: ignore[attr-defined]
    assert str(root / T.PARTIAL / "01 One.flac") in tested
    assert sorted(os.listdir(directory(service, tape))) == [
        "01 One.flac",
        "02 Two.flac",
    ]
    assert os.listdir(root / T.PARTIAL) == []


def test_each_song_records_its_cut_and_the_view_says_all_are_cut(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    song = T.read(service.layout, tape).songs[1]
    assert song.cut == TP.CutRecord(TP.CUT, "02 Two.flac", song.start, song.end)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["all_cut"]
    assert {r["cut"] for r in view["ear"]} == {TP.CUT}


def test_song_100_files_after_song_11_on_disk(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    songs = tuple(
        TP.Song(
            n,
            "Same",
            "A Band",
            "",
            1.0,
            start=float(n),
            end=n + 0.5,
            verdict=TP.AGREES,
            settled_by=M.PERSON,
        )
        for n in range(1, 101)
    )
    T.save(service.layout, TP.Tape(id=tape, name="Mix", songs=songs))
    T.audio_file(service.layout, tape).write_bytes(b"fLaC")
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    names = os.listdir(directory(service, tape))
    in_text_order = sorted(names)
    assert in_text_order[10].startswith("011 ")
    assert in_text_order[99].startswith("100 ")
    assert len(names) == 100


# -------------------------------------------------------------------------- skip


def test_a_skipped_song_is_not_cut_and_is_named_by_the_job_and_the_view(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/songs/1/from-list", service)
    post(app, f"/api/tapes/{tape}/songs/2/skip", service)
    assert cut(app, tape, service).status == 202

    done = job_of(app, tape, service)
    assert not done["error"], done["error"]
    assert done["skipped"] == ["song 2 (Two)"]
    assert done["result"]["skipped"] == ["song 2 (Two)"]
    assert [c[-1].rsplit("/", 1)[1] for c in cuts_sent(service)] == ["01 One.flac"]
    assert os.listdir(directory(service, tape)) == ["01 One.flac"]

    view = get(app, f"/api/tapes/{tape}", service).json()
    assert {r["number"]: r["cut"] for r in view["ear"]} == {1: TP.CUT, 2: TP.SKIPPED}
    assert view["all_cut"], "a skipped song does not hold the tape back"


def test_skipping_a_song_already_cut_moves_its_file_aside_and_back_recuts(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    post(app, f"/api/tapes/{tape}/songs/2/skip", service)
    cut(app, tape, service)
    root = T.dir_for(service.layout, tape)
    assert os.listdir(directory(service, tape)) == ["01 One.flac"]
    assert any("02 Two.flac" in n for n in os.listdir(root / T.SUPERSEDED))

    post(app, f"/api/tapes/{tape}/songs/2/skip", service, {"skip": False})
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert {r["number"]: r["cut"] for r in view["ear"]}[2] == TP.NOT_CUT
    before = len(cuts_sent(service))
    cut(app, tape, service)
    assert len(cuts_sent(service)) == before + 1


# ------------------------------------------------------------------------ failure


def test_a_failing_verify_marks_the_song_names_it_and_the_rest_continue(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    service.runner.bad.add("01 One")  # type: ignore[attr-defined]
    assert cut(app, tape, service).status == 202

    done = job_of(app, tape, service)
    assert "song 1 (One)" in done["error"]
    assert "would not decode" in done["error"]
    assert len(cuts_sent(service)) == 2

    one, two = T.read(service.layout, tape).songs
    assert one.cut and one.cut.status == TP.CUT_FAILED
    assert two.cut and two.cut.status == TP.CUT
    assert os.listdir(directory(service, tape)) == ["02 Two.flac"]
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert not view["all_cut"], "the tape cannot proceed until this is resolved"
    assert {r["number"]: r["cut"] for r in view["ear"]}[1] == TP.CUT_FAILED


def test_cutting_again_retries_only_the_song_that_failed(service: Service) -> None:
    app = build(service)
    tape = ready(app, service)
    service.runner.bad.add("01 One")  # type: ignore[attr-defined]
    cut(app, tape, service)
    service.runner.bad.clear()  # type: ignore[attr-defined]
    cut(app, tape, service)

    assert not job_of(app, tape, service)["error"]
    assert [c[-1].rsplit("/", 1)[1] for c in cuts_sent(service)] == [
        "01 One.flac",
        "02 Two.flac",
        "01 One.flac",
    ]
    assert get(app, f"/api/tapes/{tape}", service).json()["all_cut"]


def test_a_missing_program_fails_the_job_with_its_message(tmp_path: Path) -> None:
    service = a_service(tmp_path, runner=Cutting(a_tape_runner()))  # type: ignore[arg-type]
    app = build(service)
    tape = ready(app, service)
    service.runner.inner.installed = {"flac"}  # type: ignore[attr-defined]
    cut(app, tape, service)
    assert "ffmpeg is not installed" in job_of(app, tape, service)["error"]


# ------------------------------------------------------------------------- resume


def test_an_interrupted_cut_resumes_without_recutting_what_was_verified(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    service.runner.dies_on = "02 Two"  # type: ignore[attr-defined]
    cut(app, tape, service)
    assert "went away" in job_of(app, tape, service)["error"]
    first, second = T.read(service.layout, tape).songs
    assert first.cut and first.cut.status == TP.CUT, "saved as soon as it was verified"
    assert second.cut is None

    service.runner.dies_on = ""  # type: ignore[attr-defined]
    cut(app, tape, service)
    done = job_of(app, tape, service)
    assert not done["error"], done["error"]
    assert done["result"]["kept"] == ["song 1 (One)"]
    assert done["result"]["cut"] == ["song 2 (Two)"]
    assert [c[-1].rsplit("/", 1)[1] for c in cuts_sent(service)] == [
        "01 One.flac",
        "02 Two.flac",
    ], "song 1 was cut once across both runs"


def test_a_record_whose_file_has_gone_is_cut_again(service: Service) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    (directory(service, tape) / "01 One.flac").unlink()
    cut(app, tape, service)
    assert job_of(app, tape, service)["result"]["cut"] == ["song 1 (One)"]
    assert (directory(service, tape) / "01 One.flac").is_file()


def test_what_a_crash_left_half_written_is_cleared_and_never_taken_for_a_cut(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    partial = T.fresh_partial(service.layout, tape)
    (partial / "01 One.flac").write_bytes(b"half")
    (partial / "07 A Song Since Dropped.flac").write_bytes(b"half")
    cut(app, tape, service)
    assert os.listdir(partial) == []
    assert (directory(service, tape) / "01 One.flac").read_bytes() != b"half"


# -------------------------------------------------------------------------- recut


def test_a_changed_boundary_recuts_and_the_old_cut_is_moved_aside(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    target = directory(service, tape) / "02 Two.flac"
    old = target.read_bytes()
    made = len(cuts_sent(service))

    set_song_2(app, tape, service, 6.5, 11.5)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert {r["number"]: r["cut"] for r in view["ear"]}[2] == TP.OUT_OF_DATE
    assert not view["all_cut"]

    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert target.read_bytes() == b"6.500-11.500"
    assert len(cuts_sent(service)) == made + 1, "song 1 did not change"
    aside = list((T.dir_for(service.layout, tape) / T.SUPERSEDED).iterdir())
    assert [p.read_bytes() for p in aside] == [old]
    assert service.runner.seen_at_cut["02 Two.flac"] == old, (  # type: ignore[attr-defined]
        "the real name held the old cut while the new one was written"
    )


def test_a_recut_that_fails_verify_leaves_the_old_file_whole_and_says_failed(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    target = directory(service, tape) / "02 Two.flac"
    old = target.read_bytes()
    set_song_2(app, tape, service, 6.5, 11.5)
    service.runner.bad.add("02 Two")  # type: ignore[attr-defined]
    assert cut(app, tape, service).status == 202

    assert "song 2 (Two)" in job_of(app, tape, service)["error"]
    assert target.read_bytes() == old
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert {r["number"]: r["cut"] for r in view["ear"]}[2] == TP.CUT_FAILED
    assert not view["all_cut"]


def test_a_recut_whose_record_was_never_saved_is_not_read_as_the_old_cut(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file holds the new edges and that outcome was never saved. Moving the
    boundary back must not make that read as a cut of the old edges."""
    app = build(service)
    tape = ready(app, service)
    set_song_2(app, tape, service, 6.5, 11.5)
    cut(app, tape, service)
    target = directory(service, tape) / "02 Two.flac"
    old = target.read_bytes()
    assert old == b"6.500-11.500"

    set_song_2(app, tape, service, 6.25, 11.25)
    saving, taking = T.save, T.take_cut
    taken = []

    def take(*args: object) -> None:
        taking(*args)  # type: ignore[arg-type]
        taken.append(True)

    def save(*args: object) -> None:
        if taken:
            raise OSError("no room left to save")
        saving(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(T, "take_cut", take)
    monkeypatch.setattr(T, "save", save)
    cut(app, tape, service)
    assert "no room left" in job_of(app, tape, service)["error"]
    assert target.read_bytes() == b"6.250-11.250"
    monkeypatch.setattr(T, "take_cut", taking)
    monkeypatch.setattr(T, "save", saving)

    set_song_2(app, tape, service, 6.5, 11.5)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert {r["number"]: r["cut"] for r in view["ear"]}[2] == TP.NOT_CUT
    assert not view["all_cut"]
    assert cut(app, tape, service).status == 202
    done = job_of(app, tape, service)
    assert done["result"]["cut"] == ["song 2 (Two)"]
    assert target.read_bytes() == old


def test_a_crash_while_the_file_is_replaced_leaves_no_record_of_the_old_cut(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    set_song_2(app, tape, service, 6.25, 11.25)

    def crash(*_args: object) -> None:
        raise OSError("the machine went away")

    monkeypatch.setattr(T, "take_cut", crash)
    cut(app, tape, service)
    assert "went away" in job_of(app, tape, service)["error"]
    assert T.read(service.layout, tape).songs[1].cut is None


def test_cutting_again_after_a_failed_recut_recuts_though_the_old_file_is_there(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    target = directory(service, tape) / "02 Two.flac"
    set_song_2(app, tape, service, 6.5, 11.5)
    service.runner.bad.add("02 Two")  # type: ignore[attr-defined]
    cut(app, tape, service)
    service.runner.bad.clear()  # type: ignore[attr-defined]

    assert cut(app, tape, service).status == 202
    done = job_of(app, tape, service)
    assert not done["error"]
    assert done["result"]["cut"] == ["song 2 (Two)"]
    assert target.read_bytes() == b"6.500-11.500"


def test_recutting_twice_keeps_every_superseded_cut(service: Service) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    set_song_2(app, tape, service, 6.5, 11.5)
    assert cut(app, tape, service).status == 202
    set_song_2(app, tape, service, 6.25, 11.25)
    assert cut(app, tape, service).status == 202
    aside = T.dir_for(service.layout, tape) / T.SUPERSEDED
    assert len(os.listdir(aside)) == 2


def test_a_new_list_leaves_no_cut_behind_under_a_name_nothing_claims(
    service: Service,
) -> None:
    app = build(service)
    tape = ready(app, service)
    cut(app, tape, service)
    current = T.read(service.layout, tape)
    open_to_change = tuple(replace(s, settled_by=M.CATALOGUE) for s in current.songs)
    T.save(service.layout, replace(current, songs=open_to_change))
    edited = TWO.replace(",Two,", ",Deux,")
    assert send_csv(app, tape, service, edited, confirm=True).status == 200
    process(app, tape, service)
    settle_all(app, tape, service)
    assert cut(app, tape, service).status == 202
    assert not job_of(app, tape, service)["error"]
    assert sorted(os.listdir(directory(service, tape))) == [
        "01 One.flac",
        "02 Deux.flac",
    ]
    aside = T.dir_for(service.layout, tape) / T.SUPERSEDED
    assert any("02 Two.flac" in n for n in os.listdir(aside))
