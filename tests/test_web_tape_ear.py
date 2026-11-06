"""A person's judgement of a tape's boundaries over the wire, no ffmpeg. ADR-063."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.core import tape as TP
from ripdoctor.core.envelope import HEADER_BYTES, decode
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


# ------------------------------------------------------------------- the waveform


def test_a_tape_s_waveform_is_what_processing_measured_and_opening_measures_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    kept = T.envelope_file(service.layout, tape)
    lanes = decode(kept.read_bytes())
    assert len(kept.read_bytes()) == HEADER_BYTES + 3 * len(lanes.full)
    before = len(service.runner.calls)  # type: ignore[attr-defined]
    assert get(app, f"/api/tapes/{tape}", service).json()["waveform"]
    answer = get(app, f"/api/tapes/{tape}/envelope", service)
    assert answer.status == 200
    assert answer.path == str(kept)
    assert len(service.runner.calls) == before, "no measure, no encode, no probe"  # type: ignore[attr-defined]


def test_a_tape_with_no_measurement_says_so_rather_than_drawing_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    assert not get(app, f"/api/tapes/{tape}", service).json()["waveform"]
    assert get(app, f"/api/tapes/{tape}/envelope", service).status == 404


def test_a_tape_s_audio_is_its_own_file_served_for_seeking(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    answer = get(app, f"/api/tapes/{tape}/audio", service)
    assert answer.status == 200
    assert answer.path == str(T.audio_file(service.layout, tape))
    assert answer.content_type == "audio/flac"


def test_a_tape_with_no_audio_serves_none(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    assert get(app, f"/api/tapes/{tape}/audio", service).status == 409
    assert get(app, "/api/tapes/nobody/audio", service).status == 404


# ----------------------------------------------------------------- judgements


def test_a_drag_is_saved_as_the_person_s_and_a_re_run_leaves_it(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    url = "songs/2/edges"
    body = {"start": 6.5, "end": 11.0}
    answer = post(app, f"/api/tapes/{tape}/{url}", service, body)
    assert answer.status == 200, answer.text
    process(app, tape, service)
    song = T.read(service.layout, tape).songs[1]
    assert (song.start, song.end, song.set_by) == (6.5, 11.0, TP.EAR)


@pytest.mark.parametrize(
    ("start", "end", "said"),
    [
        (1.0, 11.0, "overlaps song 1"),
        (6.5, 9999.0, "does not fit"),
        (12.0, 11.0, "does not fit"),
    ],
)
def test_an_impossible_drag_is_refused_with_its_reason_and_changes_nothing(
    service: Service, start: float, end: float, said: str
) -> None:
    app = build(service)
    tape = placed(app, service)
    before = T.read(service.layout, tape)
    url = "songs/2/edges"
    body = {"start": start, "end": end}
    refused = post(app, f"/api/tapes/{tape}/{url}", service, body)
    assert refused.status == 409
    assert said in refused.json()["error"]
    assert T.read(service.layout, tape) == before


def test_moving_a_boundary_settles_that_song_s_doubt(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    url = "songs/2/edges"
    body = {"start": 6.5, "end": 11.0}
    view = post(app, f"/api/tapes/{tape}/{url}", service, body).json()
    assert not {r["number"]: r for r in view["ear"]}[2]["doubtful"]


def test_accepting_settles_a_doubt_and_the_cut_waits_until_it_is_settled(
    service: Service,
) -> None:
    app = build(service)
    tape = placed(app, service)
    for song in (1, 2):
        post(app, f"/api/tapes/{tape}/songs/{song}/from-list", service)
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert "song 2 (Two): still in doubt" in view["cut_refusal"]
    refused = post(app, f"/api/tapes/{tape}/cut", service)
    assert refused.status == 409
    assert "song 2 (Two)" in refused.json()["error"]

    accepted = post(app, f"/api/tapes/{tape}/songs/2/heard", service).json()
    assert accepted["cut_refusal"] == ""
    assert accepted["songs"][1]["set_by"] == TP.FOUND
    taken_back = post(
        app, f"/api/tapes/{tape}/songs/2/heard", service, {"heard": False}
    )
    assert "song 2 (Two)" in taken_back.json()["cut_refusal"]


def test_a_document_written_with_clips_and_a_preview_beside_it_still_opens(
    service: Service,
) -> None:
    """Those files stay on disk, unread, until the tape is removed."""
    app = build(service)
    tape = placed(app, service)
    post(app, f"/api/tapes/{tape}/songs/2/heard", service)
    where = T.dir_for(service.layout, tape)
    (where / "clips").mkdir()
    (where / "clips" / "0000005000.flac").write_bytes(b"fLaC")
    (where / "tape.opus").write_bytes(b"OggS")
    view = get(app, f"/api/tapes/{tape}", service).json()
    assert view["songs"][1]["heard"]
    assert not {r["number"]: r for r in view["ear"]}[2]["doubtful"]
    assert set(view["ear"][0]) == {"number", "doubtful", "cut"}
    assert "preview" not in view
    assert (where / "clips" / "0000005000.flac").is_file()


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
    "body",
    [
        {},
        {"start": 0.0},
        {"start": "", "end": 1},
        {"start": None, "end": 1},
        {"start": "soon", "end": 1},
        {"start": True, "end": 2},
    ],
)
def test_an_edge_that_is_no_number_is_refused(service: Service, body: dict) -> None:
    app = build(service)
    tape = placed(app, service)
    url = f"/api/tapes/{tape}/songs/1/edges"
    assert post(app, url, service, body).status == 400


def test_a_real_zero_is_a_good_edge(service: Service) -> None:
    app = build(service)
    tape = placed(app, service)
    url = f"/api/tapes/{tape}/songs/1/edges"
    assert post(app, url, service, {"start": 0.0, "end": 1.0}).status == 200
    assert T.read(service.layout, tape).songs[0].start == 0.0


def test_the_ear_check_refuses_a_blank_edge_before_it_can_become_zero() -> None:
    source = (Path(__file__).parent.parent / "ripdoctor/web/static/app.js").read_text()
    guard = source.index("a blank one is not 0")
    assert source.index('if (!edge("start") || !edge("end"))') < guard
    assert guard < source.index('Number(edge("start"))')


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


@pytest.mark.parametrize(
    ("judgement", "body"),
    [("heard", {}), ("skip", {}), ("edges", {"start": 6.0, "end": 11.0})],
)
def test_a_judgement_waits_for_a_running_job_and_changes_nothing(
    service: Service, judgement: str, body: dict
) -> None:
    app = build(service)
    tape = placed(app, service)
    before = T.read(service.layout, tape)
    url = f"/api/tapes/{tape}/songs/2/{judgement}"
    with service.jobs.claim(T.key_of(tape), "something"):
        assert post(app, url, service, body).status == 409
    assert T.read(service.layout, tape) == before
