"""A tape's ear check over the wire, with no ffmpeg. ADR-063."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio.split import TICK_HZ
from ripdoctor.core import tape as TP
from ripdoctor.store import cache as C
from ripdoctor.store import tapes as T
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from tests.test_web_routes import a_service, get, post
from tests.test_web_tapes import (
    a_new_tape,
    a_tape_runner,
    process,
    send_csv,
    upload,
)

ONE_DOUBT = (
    "playlist,owned,position,title,artist,album,duration\r\n"
    "Mix,no,1,One,A Band,A Record,0:05\r\n"
    "Mix,no,2,Two,A Band,A Record,0:30\r\n"
)


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return a_service(tmp_path, runner=a_tape_runner())


def placed(app, service: Service, text: str = ONE_DOUBT) -> str:  # type: ignore[no-untyped-def]
    tape = a_new_tape(app, service)
    assert send_csv(app, tape, service, text).status == 200
    upload(app, tape, service)
    process(app, tape, service)
    return tape


def job_of(app, tape: str, service: Service) -> dict:  # type: ignore[no-untyped-def]
    return get(app, f"/api/job/{T.key_of(tape)}", service).json()


def ticks(service: Service) -> list[tuple[str, ...]]:
    return [c for c in service.runner.calls if any("sine=" in a for a in c)]  # type: ignore[attr-defined]


def flag(argv: tuple[str, ...], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_clips_are_built_only_for_flagged_boundaries_by_default(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    assert post(app, f"/api/tapes/{tape}/clips", service).status == 202
    assert not job_of(app, tape, service)["error"]

    made = ticks(service)
    assert len(made) == 2, "song 2 is in doubt, song 1 agrees with the list"
    song = T.read(service.layout, tape).songs[1]
    assert flag(made[0], "-ss") == f"{(song.start or 0.0) - 4.0:.2f}"
    assert f"sine=frequency={TICK_HZ}" in " ".join(made[0])
    assert all(c[-1].endswith(".flac") for c in made)
    assert all(T.PARTIAL in c[-1] for c in made), "a clip earns its name afterwards"

    row = {
        r["number"]: r for r in get(app, f"/api/tapes/{tape}", service).json()["ear"]
    }
    assert row[2]["clips"] == {"start": True, "end": True}
    assert row[1]["clips"] == {"start": False, "end": False}


def test_any_other_boundary_can_be_asked_for(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    asked = {"song": 1, "edge": "end"}
    assert post(app, f"/api/tapes/{tape}/clips", service, asked).status == 202
    assert len(ticks(service)) == 1
    answer = get(app, f"/api/tapes/{tape}/songs/1/clip/end", service)
    assert answer.status == 200
    assert answer.path.endswith(".flac")
    assert get(app, f"/api/tapes/{tape}/songs/1/clip/start", service).status == 404


def test_a_clip_is_only_for_a_start_or_an_end(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/clips", service, {"song": 1, "edge": "end"})
    assert get(app, f"/api/tapes/{tape}/songs/1/clip/middle", service).status == 404


def test_a_clip_already_built_is_not_built_again(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/clips", service)
    post(app, f"/api/tapes/{tape}/clips", service)
    assert len(ticks(service)) == 2


def test_a_tick_is_held_short_of_the_end_of_the_tape(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/clips", service, {"song": 2, "edge": "end"})
    duration = T.read(service.layout, tape).duration
    assert float(flag(ticks(service)[0], "-to")) <= duration


def test_a_boundary_that_moved_needs_its_own_clip(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/clips", service, {"song": 2, "edge": "end"})
    url = f"/api/tapes/{tape}/songs/2/clip/end"
    assert get(app, url, service).status == 200
    post(app, f"/api/tapes/{tape}/songs/2/edges", service, {"start": 7.0, "end": 11.0})
    assert get(app, url, service).status == 404


def test_with_nothing_flagged_a_clip_has_to_be_asked_for_by_name(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(
        app,
        tape,
        service,
        ONE_DOUBT.replace("0:30", "0:05"),
    )
    upload(app, tape, service)
    process(app, tape, service)
    assert post(app, f"/api/tapes/{tape}/clips", service).status == 409
    assert ticks(service) == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"song": 1, "edge": "middle"}, 400),
        ({"song": 9, "edge": "start"}, 404),
    ],
)
def test_a_boundary_that_is_not_there_is_refused(
    service: Service, body: dict, status: int
) -> None:
    app = build(service)
    tape = placed(app, service)
    assert post(app, f"/api/tapes/{tape}/clips", service, body).status == status


def test_clips_and_a_preview_need_the_audio(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(app, tape, service, ONE_DOUBT)
    assert post(app, f"/api/tapes/{tape}/clips", service).status == 409
    assert post(app, f"/api/tapes/{tape}/preview", service).status == 409


def test_opening_a_tape_never_builds_a_preview(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    before = len(service.runner.calls)  # type: ignore[attr-defined]
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert not view["preview"]
    assert len(service.runner.calls) == before  # type: ignore[attr-defined]
    assert get(app, f"/api/tapes/{tape}/preview", service).status == 404


def test_a_preview_is_a_job_writing_the_record_flow_s_encode(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)

    def encode(argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        if "libopus" in argv:
            Path(argv[-1]).write_bytes(b"OggS")
        return plain(argv, stdin=stdin, timeout=timeout)

    plain = service.runner.run
    service.runner.run = encode  # type: ignore[method-assign]
    assert post(app, f"/api/tapes/{tape}/preview", service).status == 202
    done = job_of(app, tape, service)
    assert done["op"] == "preview"
    assert not done["error"], done["error"]

    audio = str(T.audio_file(service.layout, tape))
    encodes = [c for c in service.runner.calls if "libopus" in c]
    assert len(encodes) == 1
    assert list(encodes[0][:-1]) == C.preview_argv(audio, "x")[:-1]
    assert get(app, f"/api/tapes/{tape}", service).json()["preview"]
    assert get(app, f"/api/tapes/{tape}/preview", service).status == 200

    post(app, f"/api/tapes/{tape}/preview", service)
    assert len([c for c in service.runner.calls if "libopus" in c]) == 1


def test_a_preview_that_produced_nothing_carries_a_message(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/preview", service)
    assert "produced nothing" in job_of(app, tape, service)["error"]
    assert not T.preview_file(service.layout, tape).exists()


# ----------------------------------------------------------------- judgements


def test_an_edge_set_over_the_wire_is_not_moved_by_a_re_run(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    url = f"/api/tapes/{tape}/songs/1/edges"
    answer = post(app, url, service, {"start": 0.5, "end": 5.25})
    assert answer.status == 200, answer.text
    process(app, tape, service)
    song = T.read(service.layout, tape).songs[0]
    assert (song.start, song.end, song.set_by) == (0.5, 5.25, TP.EAR)


def test_an_overlap_is_refused_and_changes_nothing(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/songs/2/edges", service, {"start": 7.0, "end": 11.0})
    before = T.read(service.layout, tape)
    refused = post(
        app, f"/api/tapes/{tape}/songs/1/edges", service, {"start": 0.0, "end": 8.0}
    )
    assert refused.status == 409
    assert "overlaps song 2" in refused.json()["error"]
    assert T.read(service.layout, tape) == before


@pytest.mark.parametrize(
    "body", [{}, {"start": "soon", "end": 1}, {"start": True, "end": 2}]
)
def test_an_edge_that_is_no_number_is_refused(service: Service, body: dict) -> None:
    app = build(service)
    tape = placed(app, service)
    url = f"/api/tapes/{tape}/songs/1/edges"
    assert post(app, url, service, body).status == 400


def test_an_edge_for_a_song_not_on_the_list_is_not_found(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    body = {"start": 0.0, "end": 1.0}
    assert post(app, f"/api/tapes/{tape}/songs/9/edges", service, body).status == 404


def test_heard_clears_doubt_over_the_wire_and_survives_a_re_run(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    answer = post(app, f"/api/tapes/{tape}/songs/2/heard", service)
    assert answer.status == 200, answer.text
    row = {r["number"]: r for r in answer.json()["ear"]}
    assert not row[2]["doubtful"]

    process(app, tape, service)
    song = T.read(service.layout, tape).songs[1]
    assert song.heard
    assert song.set_by == TP.FOUND


def test_heard_can_be_taken_back(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    url = f"/api/tapes/{tape}/songs/2/heard"
    post(app, url, service)
    answer = post(app, url, service, {"heard": False})
    assert {r["number"]: r for r in answer.json()["ear"]}[2]["doubtful"]


def test_a_skip_is_kept_and_named_in_the_document_until_taken_back(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    url = f"/api/tapes/{tape}/songs/2/skip"
    answer = post(app, url, service)
    assert answer.status == 200, answer.text
    view = answer.json()
    assert view["songs"][1]["skip"]
    assert view["ear"][-1]["number"] == 2, "a skipped song is last, and still there"
    process(app, tape, service)
    assert T.read(service.layout, tape).songs[1].skip
    assert not post(app, url, service, {"skip": False}).json()["songs"][1]["skip"]


def test_a_judgement_waits_for_a_running_job(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    with service.jobs.claim(T.key_of(tape), "something"):
        assert post(app, f"/api/tapes/{tape}/songs/2/heard", service).status == 409
