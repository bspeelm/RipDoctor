"""A tape over the wire: list, audio, a job, and a document. ADR-063."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner, Result
from ripdoctor.core import tape as TP
from ripdoctor.store import incoming as IN
from ripdoctor.store import tapes as T
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.routes import ingest as INGEST
from ripdoctor.web.routes import precut as PRECUT
from ripdoctor.web.routes.tapes import CHUNK, MAX_UPLOAD
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Jobs
from tests.test_web_routes import a_service, get, post, signed_in

CSV = (
    "playlist,owned,position,title,artist,album,duration\r\n"
    'Mix,yes,2,"Two, Part ""B""",A Band,A Record,0:05\r\n'
    "Mix,no,1,One,A Band,A Record,0:05\r\n"
)
BY_HAND = [
    {"title": "One", "artist": "A Band", "album": "A Record", "length": "0:05"},
    {"title": 'Two, Part "B"', "artist": "A Band", "album": "A Record", "length": 5},
]
SECONDS = 12.0


def lane_frames() -> bytes:
    """5 s of music, 2 s of digital silence, 5 s of music, as astats reports it."""
    rows = []
    for i in range(int(SECONDS / 0.05)):
        silent = 5.0 <= i * 0.05 < 7.0
        rms, peak = ("-inf", "-inf") if silent else ("-20.0", "-6.0")
        rows.append(f"frame:{i} pts:{i} pts_time:{i * 0.05}")
        rows.append(f"lavfi.astats.Overall.RMS_level={rms}")
        rows.append(f"lavfi.astats.Overall.Peak_level={peak}")
    return "\n".join(rows).encode()


class Normalising(FakeRunner):
    """An ffmpeg that writes the file it was told to."""

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if args[0] == "ffmpeg" and args[-1].endswith(".flac"):
            Path(args[-1]).write_bytes(b"fLaC-normalised")
        return super().run(argv, stdin=stdin, timeout=timeout)


def a_tape_runner() -> FakeRunner:
    streams = {
        "streams": [{"codec_name": "wav", "channels": 2, "sample_rate": "48000"}]
    }
    return (
        Normalising()
        .expect(
            "stream=codec_name,channels,sample_rate",
            stdout=json.dumps(streams).encode(),
        )
        .expect("stream=sample_rate", stdout=b"48000\n")
        .expect("astats", stdout=lane_frames())
        .expect("-progress", stdout=f"out_time_us={int(SECONDS * 1e6)}\n".encode())
    )


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return a_service(tmp_path, runner=a_tape_runner())


def seeing(
    service: Service, tape: str, number: int, start: object, end: object
) -> dict:
    """What the page sends: the new edges, and the ones it last saw."""
    song = T.read(service.layout, tape).songs[number - 1]
    return {"start": start, "end": end, "seen_start": song.start, "seen_end": song.end}


def a_new_tape(app, service: Service, name: str = "Mix: Vol. 1") -> str:  # type: ignore[no-untyped-def]
    made = post(app, "/api/tapes", service, {"name": name})
    assert made.status == 201
    return made.json()["id"]


def save_list(app, tape: str, service: Service, rows: object, **more: object):  # type: ignore[no-untyped-def]
    """A hand list, sent as a page does: saying which list it was made from."""
    seen = get(app, f"/api/tapes/{tape}", service).json()["list_seen"]
    body = {"songs": rows, "seen": seen, **more}
    return post(app, f"/api/tapes/{tape}/songs", service, body)


def send_csv(  # type: ignore[no-untyped-def]
    app, tape: str, service: Service, text: str = CSV, *, confirm: bool = False
) -> H.Response:
    return app.dispatch(
        H.Request.of(
            "POST",
            f"/api/tapes/{tape}/list{'?confirm=1' if confirm else ''}",
            headers=signed_in(service),
            body=text.encode(),
        )
    )


def send_chunk(app, tape: str, service: Service, at: int, data: bytes) -> H.Response:  # type: ignore[no-untyped-def]
    return app.dispatch(
        H.Request.of(
            "POST",
            f"/api/tapes/{tape}/upload/chunk?at={at}",
            headers=signed_in(service),
            body=data,
        )
    )


def upload(app, tape: str, service: Service, data: bytes = b"RIFFdata") -> None:  # type: ignore[no-untyped-def]
    begun = post(
        app,
        f"/api/tapes/{tape}/upload/begin",
        service,
        {"name": "mix.wav", "size": len(data)},
    )
    assert begun.status == 200, begun.text
    assert send_chunk(app, tape, service, 0, data).status == 200


def a_ready_tape(app, service: Service) -> str:  # type: ignore[no-untyped-def]
    tape = a_new_tape(app, service)
    assert send_csv(app, tape, service).status == 200
    upload(app, tape, service)
    return tape


def process(app, tape: str, service: Service) -> dict:  # type: ignore[no-untyped-def]
    started = post(app, f"/api/tapes/{tape}/process", service)
    assert started.status == 202, started.text
    done = get(app, f"/api/job/{T.key_of(tape)}", service).json()
    assert done["done"]
    assert not done["error"], done["error"]
    return done["result"]


# ------------------------------------------------------------------- the list


def test_a_csv_and_a_list_by_hand_write_the_same_document(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    document = T.dir_for(service.layout, tape) / T.DOCUMENT

    assert send_csv(app, tape, service).status == 200
    from_file = document.read_text()
    answer = save_list(app, tape, service, BY_HAND)
    assert answer.status == 200, answer.text
    assert document.read_text() == from_file


def test_a_list_from_a_csv_stays_editable(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    loaded = send_csv(app, tape, service).json()["songs"]

    edited = [
        {"title": "Three", "artist": "A Band", "album": "", "length": "1:00"},
        loaded[1],
        loaded[0],
    ]
    answer = save_list(app, tape, service, edited)
    assert [(s["number"], s["title"]) for s in answer.json()["songs"]] == [
        (1, "Three"),
        (2, 'Two, Part "B"'),
        (3, "One"),
    ]


def test_a_bad_list_is_refused_with_its_row_and_changes_nothing(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(app, tape, service)
    refused = send_csv(app, tape, service, CSV.replace("0:05", "soon", 1))
    assert refused.status == 400
    assert "row 2" in refused.json()["error"]
    assert len(get(app, f"/api/tapes/{tape}", service).json()["songs"]) == 2


def test_a_list_that_is_not_utf8_says_so(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    refused = app.dispatch(
        H.Request.of(
            "POST",
            f"/api/tapes/{tape}/list",
            headers=signed_in(service),
            body=b"caf\xe9",
        )
    )
    assert refused.status == 400
    assert "UTF-8" in refused.json()["error"]


def test_a_stale_page_reverted_a_row_and_is_refused_with_nothing_changed(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(app, tape, service)
    seen = get(app, f"/api/tapes/{tape}", service).json()["list_seen"]
    newer = [{**BY_HAND[0], "title": "Newer"}, BY_HAND[1]]
    assert save_list(app, tape, service, newer).status == 200
    held = T.read(service.layout, tape)
    for said in ({"seen": seen}, {}):
        old = post(app, f"/api/tapes/{tape}/songs", service, {"songs": BY_HAND, **said})
        assert old.status == 409
        assert old.json()["stale"] is True
        assert "has changed since this page loaded it" in old.json()["error"]
    assert T.read(service.layout, tape) == held


def test_a_hand_list_of_the_wrong_shape_is_a_refusal(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    assert save_list(app, tape, service, "x").status == 400


def test_a_new_list_is_refused_over_an_ear_set_boundary(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(app, tape, service)
    saved = T.read(service.layout, tape)
    ear = replace(saved.songs[0], start=0.0, end=5.0, set_by=TP.EAR)
    T.save(service.layout, replace(saved, songs=(ear, saved.songs[1])))
    more = [*BY_HAND, {"title": "More", "artist": "A", "length": "3:00"}]
    refused = save_list(app, tape, service, more)
    assert refused.status == 409
    assert "which audio belongs to which song" in refused.json()["error"]
    assert send_csv(app, tape, service, confirm=True).status == 409
    assert T.read(service.layout, tape).songs[0].set_by == TP.EAR


# ------------------------------------------------------------------ the upload


def test_a_tape_may_be_larger_than_a_record_may(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    monkeypatch.setattr(IN, "room_for", lambda _l, _s: True)
    three_gb = 3 * 2**30
    assert (
        post(
            app,
            f"/api/tapes/{tape}/upload/begin",
            service,
            {"name": "mix.wav", "size": three_gb},
        ).status
        == 200
    )
    assert (
        post(
            app,
            "/api/ingest/a-record/begin",
            service,
            {"name": "side.wav", "size": three_gb},
        ).status
        == 413
    )
    assert INGEST.MAX_UPLOAD == PRECUT.MAX_UPLOAD == 2 * 2**30


def test_an_upload_over_the_limit_is_refused(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    refused = post(
        app,
        f"/api/tapes/{tape}/upload/begin",
        service,
        {"name": "mix.wav", "size": MAX_UPLOAD + 1},
    )
    assert refused.status == 413
    assert "16 GB" in refused.json()["error"]


def test_an_infinite_size_was_an_uncaught_overflow_and_is_now_a_400(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    refused = app.dispatch(
        H.Request.of(
            "POST",
            f"/api/tapes/{tape}/upload/begin",
            headers=signed_in(service),
            body=b'{"name": "mix.wav", "size": 1e999}',
        )
    )
    assert refused.status == 400


def test_a_length_too_large_to_hold_is_a_400(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    song = {"title": "O", "artist": "A", "album": "R", "length": 10**400}
    refused = save_list(app, tape, service, [song])
    assert refused.status == 400
    assert "length" in refused.json()["error"]


def test_an_upload_the_pool_has_no_room_for_is_refused(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    monkeypatch.setattr(IN, "room_for", lambda _l, _s: False)
    refused = post(
        app, f"/api/tapes/{tape}/upload/begin", service, {"name": "m.wav", "size": 4}
    )
    assert refused.status == 507


def test_beginning_the_same_file_again_resumes_where_it_stopped(
    service: Service,
) -> None:
    """A 16 GB upload that restarted from nothing would never finish."""
    app = build(service)
    tape = a_new_tape(app, service)
    body = {"name": "mix.wav", "size": 8}
    post(app, f"/api/tapes/{tape}/upload/begin", service, body)
    send_chunk(app, tape, service, 0, b"RIFF")

    again = post(app, f"/api/tapes/{tape}/upload/begin", service, body).json()
    assert again["have"] == 4
    assert send_chunk(app, tape, service, 4, b"data").json()["have"] == 8


def test_a_different_file_does_not_overwrite_one_part_way_here(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    post(app, f"/api/tapes/{tape}/upload/begin", service, {"name": "a.wav", "size": 8})
    send_chunk(app, tape, service, 0, b"RIFF")
    other = {"name": "b.wav", "size": 8}
    refused = post(app, f"/api/tapes/{tape}/upload/begin", service, other)
    assert refused.status == 409
    assert "a.wav" in refused.json()["error"]
    replaced = post(
        app, f"/api/tapes/{tape}/upload/begin", service, {**other, "replace": True}
    )
    assert replaced.json()["have"] == 0


def test_a_piece_out_of_place_is_told_where_the_upload_is(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    post(app, f"/api/tapes/{tape}/upload/begin", service, {"name": "a.wav", "size": 8})
    refused = send_chunk(app, tape, service, 3, b"RI")
    assert refused.status == 409
    assert "is at 0" in refused.json()["error"]
    assert send_chunk(app, tape, service, 0, b"x" * 9).status == 409
    assert send_chunk(app, tape, service, 0, b"x" * (CHUNK + 1)).status == 413


def test_a_tape_that_has_its_audio_takes_no_other(service: Service) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    process(app, tape, service)
    refused = post(
        app, f"/api/tapes/{tape}/upload/begin", service, {"name": "b.wav", "size": 4}
    )
    assert refused.status == 409
    assert "remove the tape" in refused.json()["error"]


# --------------------------------------------------------------------- the job


def test_processing_normalises_measures_places_and_saves(service: Service) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    result = process(app, tape, service)

    calls = service.runner.calls  # type: ignore[attr-defined]
    part = str(IN.part_file(service.layout, T.key_of(tape)))
    done = str(IN.done_file(service.layout, T.key_of(tape)))
    normalise = next(c for c in calls if "-map_metadata" in c)
    assert normalise[normalise.index("-i") + 1] == part
    assert normalise[-1] == done
    assert ("flac", "-t", "-s", done) in calls
    assert sum(any("astats" in a for a in c) for c in calls) == 2

    assert result["source"] == "mix.wav"
    assert result["duration"] == SECONDS
    assert result["gaps"] == [[5.0, 6.95]]
    assert [s["verdict"] for s in result["songs"]] == [TP.AGREES, TP.AGREES]
    assert result["songs"][0]["end"] == 5.0
    assert not result["mismatch"]

    saved = T.read(service.layout, tape)
    assert saved.to_dict() == result
    assert T.audio_file(service.layout, tape).read_bytes() == b"fLaC-normalised"
    assert IN.state(service.layout, T.key_of(tape)) is None


def test_a_list_that_does_not_fit_is_saved_as_a_mismatch(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    songs = [{"title": "Long", "artist": "A", "album": "R", "length": "9:00"}]
    save_list(app, tape, service, songs)
    upload(app, tape, service)
    result = process(app, tape, service)
    assert result["mismatch"]
    assert result["songs"][0]["verdict"] == TP.DOUBT


def test_a_song_spanning_a_silence_carries_its_reason_in_the_document(
    service: Service,
) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    songs = [{"title": "Whole", "artist": "A", "album": "R", "length": "0:10"}]
    save_list(app, tape, service, songs)
    upload(app, tape, service)
    result = process(app, tape, service)
    assert result["songs"][0]["verdict"] == TP.DOUBT
    assert "0:05 to 0:07" in result["songs"][0]["reason"]
    assert get(app, f"/api/tapes/{tape}", service).json()["songs"] == result["songs"]


def test_a_second_job_is_refused_while_one_runs(tmp_path: Path) -> None:
    service = a_service(tmp_path, runner=a_tape_runner())
    service.jobs = Jobs(spawn=lambda work: None, now=service.now)
    app = build(service)
    tape = a_ready_tape(app, service)
    assert post(app, f"/api/tapes/{tape}/process", service).status == 202
    refused = post(app, f"/api/tapes/{tape}/process", service)
    assert refused.status == 409
    assert "already running" in refused.json()["error"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("songs", {"songs": BY_HAND}),
        ("upload/begin", {"name": "b.wav", "size": 4}),
        ("upload/cancel", {}),
        ("remove", {}),
    ],
)
def test_nothing_changes_under_a_running_job(
    tmp_path: Path, path: str, body: dict
) -> None:
    service = a_service(tmp_path, runner=a_tape_runner())
    service.jobs = Jobs(spawn=lambda work: None, now=service.now)
    app = build(service)
    tape = a_ready_tape(app, service)
    post(app, f"/api/tapes/{tape}/process", service)
    assert post(app, f"/api/tapes/{tape}/{path}", service, body).status == 409
    assert T.read(service.layout, tape).songs


def test_a_change_under_way_refuses_a_job_and_another_change(service: Service) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    with service.jobs.claim(T.key_of(tape), "a change"):
        assert post(app, f"/api/tapes/{tape}/process", service).status == 409
        changed = save_list(app, tape, service, BY_HAND)
        assert changed.status == 409
    process(app, tape, service)


def test_a_list_changed_just_before_a_job_starts_was_lost_and_is_now_used(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    start = service.jobs.start

    def after_a_change(*args):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(service.jobs, "start", start)
        new = [{**BY_HAND[0], "title": "Newer"}, BY_HAND[1]]
        assert save_list(app, tape, service, new).status == 200
        return start(*args)

    monkeypatch.setattr(service.jobs, "start", after_a_change)
    assert process(app, tape, service)["songs"][0]["title"] == "Newer"


def test_processing_needs_a_list_and_a_whole_upload(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    assert "song list" in post(app, f"/api/tapes/{tape}/process", service).text
    send_csv(app, tape, service)
    assert "no audio" in post(app, f"/api/tapes/{tape}/process", service).text
    post(app, f"/api/tapes/{tape}/upload/begin", service, {"name": "a.wav", "size": 8})
    send_chunk(app, tape, service, 0, b"RIFF")
    assert "send the rest" in post(app, f"/api/tapes/{tape}/process", service).text


def test_a_file_with_no_audio_keeps_its_upload_and_places_nothing(
    tmp_path: Path,
) -> None:
    service = a_service(tmp_path, runner=FakeRunner().expect("stream=", stdout=b"{}"))
    app = build(service)
    tape = a_ready_tape(app, service)
    post(app, f"/api/tapes/{tape}/process", service)
    job = get(app, f"/api/job/{T.key_of(tape)}", service).json()
    assert "no audio" in job["error"]
    assert not T.audio_file(service.layout, tape).exists()
    assert IN.state(service.layout, T.key_of(tape)) is not None


def test_a_failed_decode_keeps_the_upload_and_leaves_no_encode(
    tmp_path: Path,
) -> None:
    runner = a_tape_runner()
    runner.replies.insert(
        0, (lambda a: a[0] == "flac", Result((), 1, b"", b"bad frame"))
    )
    service = a_service(tmp_path, runner=runner)
    app = build(service)
    tape = a_ready_tape(app, service)
    post(app, f"/api/tapes/{tape}/process", service)
    job = get(app, f"/api/job/{T.key_of(tape)}", service).json()
    assert "would not decode" in job["error"]
    assert not T.audio_file(service.layout, tape).exists()
    assert not IN.done_file(service.layout, T.key_of(tape)).exists()
    assert IN.part_file(service.layout, T.key_of(tape)).read_bytes() == b"RIFFdata"


def test_a_restart_finds_the_tape_and_can_process_it_again(
    service: Service, tmp_path: Path
) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    process(app, tape, service)

    after = a_service(tmp_path, runner=a_tape_runner(), layout=service.layout)
    app = build(after)
    found = get(app, f"/api/tapes/{tape}", after).json()
    assert found["audio"]
    assert found["job"] is None
    assert found["songs"][0]["verdict"] == TP.AGREES

    edited = [{**BY_HAND[0], "length": "0:06"}, BY_HAND[1]]
    save_list(app, tape, after, edited)
    again = process(app, tape, after)
    assert again["songs"][0]["delta"] == -1.0
    assert not any(c[0] == "flac" for c in after.runner.calls)  # type: ignore[attr-defined]
    assert not any(any("astats" in a for a in c) for c in after.runner.calls)  # type: ignore[attr-defined]


def test_a_restart_between_placing_the_audio_and_clearing_the_upload_recovers(
    service: Service,
) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    T.audio_file(service.layout, tape).write_bytes(b"fLaC")
    result = process(app, tape, service)
    assert IN.state(service.layout, T.key_of(tape)) is None
    assert T.audio_file(service.layout, tape).read_bytes() == b"fLaC"
    assert result["songs"][0]["verdict"] == TP.AGREES


def test_an_ear_set_boundary_survives_processing_again(service: Service) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    process(app, tape, service)
    saved = T.read(service.layout, tape)
    ear = replace(saved.songs[1], start=6.5, end=11.5, set_by=TP.EAR)
    T.save(service.layout, replace(saved, songs=(saved.songs[0], ear)))

    again = process(app, tape, service)
    assert (again["songs"][1]["start"], again["songs"][1]["end"]) == (6.5, 11.5)
    assert again["songs"][1]["set_by"] == TP.EAR


# -------------------------------------------------------------------- the rest


def test_a_tape_is_absent_from_the_record_list(service: Service) -> None:
    app = build(service)
    views = ("/api/albums", "/api/albums?archive=1", "/api/rip/orphans")
    before = [get(app, view, service).json() for view in views]
    tape = a_ready_tape(app, service)
    process(app, tape, service)
    assert get(app, f"/api/tapes/{tape}", service).status == 200
    assert [get(app, view, service).json() for view in views] == before


def test_the_list_of_tapes_names_each_one(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    send_csv(app, tape, service)
    bad = a_new_tape(app, service, "Other")
    (T.dir_for(service.layout, bad) / T.DOCUMENT).write_text("{")
    found = get(app, "/api/tapes", service).json()
    assert found["tapes"] == [
        {"id": tape, "name": "Mix: Vol. 1", "songs": 2, "mismatch": False}
    ]
    assert found["unreadable"] == [bad]


def test_a_tape_needs_a_name(service: Service) -> None:
    assert post(build(service), "/api/tapes", service, {"name": "  "}).status == 400
    found = T.listed(service.layout)
    assert (found.tapes, found.unreadable) == ((), ())
    assert not (service.layout.work / T.WHERE).exists()


def test_a_tape_that_is_not_there_is_a_404(service: Service) -> None:
    app = build(service)
    assert get(app, "/api/tapes/nothing", service).status == 404
    assert post(app, "/api/tapes/nothing/remove", service).status == 404


def test_a_tape_id_with_a_bad_component_is_refused(service: Service) -> None:
    assert get(build(service), "/api/tapes/..", service).status == 400


def test_a_tape_is_removed_only_when_asked(service: Service) -> None:
    app = build(service)
    tape = a_ready_tape(app, service)
    process(app, tape, service)
    assert T.dir_for(service.layout, tape).is_dir()
    removed = post(app, f"/api/tapes/{tape}/remove", service)
    assert removed.status == 200
    assert not T.dir_for(service.layout, tape).exists()
    moved = service.layout.root / removed.json()["moved_to"]
    assert (moved / T.AUDIO).read_bytes() == b"fLaC-normalised"
    assert get(app, "/api/tapes", service).json()["tapes"] == []


def test_cancelling_an_upload_frees_it(service: Service) -> None:
    app = build(service)
    tape = a_new_tape(app, service)
    upload(app, tape, service)
    assert post(app, f"/api/tapes/{tape}/upload/cancel", service).status == 200
    assert IN.state(service.layout, T.key_of(tape)) is None


def test_tapes_need_a_session(service: Service) -> None:
    app = build(service)
    assert app.dispatch(H.Request.of("GET", "/api/tapes")).status == 401


def test_beginning_an_upload_clears_one_whose_tape_is_gone(service: Service) -> None:
    app = build(service)
    orphan = T.key_of("gone")
    IN.begin(service.layout, orphan, name="old.wav", size=4, now=1.0)
    tape = a_new_tape(app, service)
    upload(app, tape, service)
    assert IN.state(service.layout, orphan) is None
    assert IN.state(service.layout, T.key_of(tape)) is not None
