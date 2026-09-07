"""Recording from a browser: the meter, the controls and salvage.

No sound card. The capture loop runs on the test's clock, so a whole side
happens inside a request.
"""

from __future__ import annotations

from pathlib import Path

from ripdoctor.audio import capture as C
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.web import auth as A
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.work.capture import Recorder
from tests.tape import Tape
from tests.test_web_routes import a_service, get, post


def a_recorder(tape: Tape | None = None) -> Recorder:
    return Recorder(
        spawn=lambda work: work(),
        now=lambda: 1000.0,
        tick=tape.now if tape else (lambda: 0.0),
        sleep=tape.sleep if tape else (lambda _s: None),
    )


def recording_service(tmp_path: Path, script: str = "m" * 8):  # type: ignore[no-untyped-def]
    service = a_service(tmp_path)
    tape = Tape(C.partial_path(service.layout.raw / "album", "b"), script)
    service.recorder = a_recorder(tape)
    service.runner = FakeRunner(exit_after=len(script))
    service.settings = __import__("dataclasses").replace(
        service.settings, capture_device="hw:Rx,0"
    )
    return service, tape


# ---------------------------------------------------------------- status


def test_nothing_recording_is_reported_as_a_state(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    body = get(build(service), "/api/rip/status", service).json()
    assert body == {"running": False}


def test_a_side_records_and_reports_how_it_ended(tmp_path: Path) -> None:
    service, tape = recording_service(tmp_path)
    r = post(build(service), "/api/rip/start", service, {"slug": "album", "side": "b"})
    assert r.status == 202
    assert tape.polls >= 4, "nothing was metered"
    assert not service.recorder.status()["running"]
    assert service.recorder.status()["reason"]


def test_starting_without_a_side_is_refused(tmp_path: Path) -> None:
    service, _tape = recording_service(tmp_path)
    r = post(build(service), "/api/rip/start", service, {"slug": "album"})
    assert r.status == 400


def test_a_slug_that_could_escape_the_pool_is_refused(tmp_path: Path) -> None:
    service, _tape = recording_service(tmp_path)
    r = post(build(service), "/api/rip/start", service, {"slug": "../etc", "side": "b"})
    assert r.status == 400


def test_an_unset_device_is_refused_before_anything_starts(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.recorder = a_recorder()
    r = post(build(service), "/api/rip/start", service, {"slug": "album", "side": "b"})
    assert r.status == 400


def test_a_second_capture_is_refused_while_one_runs(tmp_path: Path) -> None:
    """The card has one input."""
    service, _tape = recording_service(tmp_path)
    service.recorder = Recorder(spawn=lambda _w: None, now=lambda: 1000.0)
    app = build(service)
    assert (
        post(app, "/api/rip/start", service, {"slug": "album", "side": "b"}).status
        == 202
    )
    r = post(app, "/api/rip/start", service, {"slug": "album", "side": "c"})
    assert r.status == 409


# --------------------------------------------------------------- controls


def running_service(tmp_path: Path):  # type: ignore[no-untyped-def]
    service, _tape = recording_service(tmp_path)
    service.recorder = Recorder(spawn=lambda _w: None, now=lambda: 1000.0)
    app = build(service)
    post(app, "/api/rip/start", service, {"slug": "album", "side": "b"})
    return service, app


def test_stopping_reaches_the_capture(tmp_path: Path) -> None:
    service, app = running_service(tmp_path)
    assert post(app, "/api/rip/stop", service).status == 200
    assert service.recorder.live is not None and service.recorder.live.control.stopping


def test_a_snooze_is_counted_and_reported(tmp_path: Path) -> None:
    """Somebody who has pressed it four times is telling you something about
    the record."""
    service, app = running_service(tmp_path)
    post(app, "/api/rip/snooze", service)
    post(app, "/api/rip/snooze", service)
    assert get(app, "/api/rip/status", service).json()["snoozes"] == 2


def test_auto_stop_can_be_turned_off_for_a_quiet_record(tmp_path: Path) -> None:
    service, app = running_service(tmp_path)
    r = post(app, "/api/rip/autostop", service, {"on": False})
    assert r.json()["autostop"] is False


def test_abandoning_throws_the_capture_away(tmp_path: Path) -> None:
    """For a take that went wrong from the start. Distinct from stop, which
    keeps what was captured - the two are one button apart and one of them
    cannot be undone."""
    service, app = running_service(tmp_path)
    partial = C.partial_path(service.layout.raw / "album", "b")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"RIFF" + b"\x00" * 8000)
    assert post(app, "/api/rip/abandon", service).status == 200
    assert not partial.exists()
    assert service.recorder.live is not None and service.recorder.live.control.stopping


def test_abandoning_nothing_is_a_409(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.recorder = a_recorder()
    assert post(build(service), "/api/rip/abandon", service).status == 409


def test_a_control_with_nothing_running_is_a_409(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.recorder = a_recorder()
    assert post(build(service), "/api/rip/stop", service).status == 409


# --------------------------------------------------------------- devices


def test_the_devices_are_listed_with_the_configured_one(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.runner = FakeRunner().expect(
        "-l",
        stdout=b"card 1: Rx [SAVITECH], device 0: USB Audio [USB Audio]\n",
    )
    body = get(build(service), "/api/rip/devices", service).json()
    assert body["devices"][0]["id"] == "hw:Rx,0"
    assert "configured" in body


def test_a_machine_with_no_arecord_says_so_rather_than_500(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.runner = FakeRunner(installed=set())
    assert get(build(service), "/api/rip/devices", service).status == 503


# ----------------------------------------------------------------- probe


def test_the_probe_reports_what_arrived(tmp_path: Path) -> None:
    """Twenty seconds before a side rather than twenty minutes after."""
    service = a_service(tmp_path)
    service.settings = __import__("dataclasses").replace(
        service.settings, capture_device="hw:Rx,0"
    )

    class Probe(FakeRunner):
        def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
            args = [str(a) for a in argv]
            if args[0] == "arecord":
                Path(args[-1]).write_bytes(b"RIFF" + b"\x00" * 4000)
            return super().run(argv, stdin=stdin, timeout=timeout)

    service.runner = (
        Probe()
        .expect(
            lambda a: any("highpass" in x for x in a),
            stderr=b"[astats] RMS level dB: -41.0\n",
        )
        .expect(
            "astats",
            stderr=b"[astats] RMS level dB: -24.0\n[astats] Peak level dB: -6.0\n",
        )
    )
    body = post(build(service), "/api/rip/test", service).json()
    assert body["ok"] and "music" in body["summary"]
    assert body["band_rms"] == -41.0


# --------------------------------------------------------------- salvage


def a_partial(service, side: str = "b") -> Path:  # type: ignore[no-untyped-def]
    album = service.layout.raw / "album"
    album.mkdir(parents=True, exist_ok=True)
    partial = C.partial_path(album, side)
    partial.write_bytes(b"RIFF" + b"\x00" * 8000)
    return partial


def test_interrupted_captures_are_found_across_every_record(tmp_path: Path) -> None:
    """Each one is most of a side, and a side is twenty minutes of somebody's
    evening."""
    service = a_service(tmp_path)
    a_partial(service)
    body = get(build(service), "/api/rip/orphans", service).json()
    assert body["orphans"] == [{"slug": "album", "side": "b", "bytes": 8004}]


def test_salvaging_finishes_the_capture(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    partial = a_partial(service)
    service.runner = FakeRunner()
    r = post(
        build(service), "/api/rip/salvage", service, {"slug": "album", "side": "b"}
    )
    assert r.status == 200 and not partial.exists()


def test_salvaging_nothing_is_a_404(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    r = post(
        build(service), "/api/rip/salvage", service, {"slug": "album", "side": "z"}
    )
    assert r.status == 404


def test_discarding_removes_only_the_partial_capture(tmp_path: Path) -> None:
    """Never a finished side. There is one copy of those."""
    service = a_service(tmp_path)
    partial = a_partial(service)
    finished = service.layout.raw / "album" / "side-a.flac"
    r = post(
        build(service), "/api/rip/discard", service, {"slug": "album", "side": "b"}
    )
    assert r.status == 200
    assert not partial.exists() and finished.is_file()


def test_discarding_a_finished_side_is_not_possible(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    r = post(
        build(service), "/api/rip/discard", service, {"slug": "album", "side": "a"}
    )
    assert r.status == 404
    assert (service.layout.raw / "album" / "side-a.flac").is_file()


def test_the_sides_of_a_record_show_what_is_finished_and_what_is_not(
    tmp_path: Path,
) -> None:
    service = a_service(tmp_path)
    a_partial(service)
    body = get(build(service), "/api/rip/sides/album", service).json()
    assert body["sides"] == ["side-a.flac"] and body["capturing"] == ["b"]


def test_every_capture_route_needs_a_session(tmp_path: Path) -> None:
    """Recording is the one thing here that touches hardware."""
    service = a_service(tmp_path)
    app = build(service)
    for method, path in (
        ("GET", "/api/rip/status"),
        ("POST", "/api/rip/start"),
        ("POST", "/api/rip/stop"),
        ("GET", "/api/rip/orphans"),
    ):
        r = app.dispatch(H.Request.of(method, path, body=b"{}"))
        assert r.status == 401, f"{path} answered without a session"
    assert A.COOKIE  # the cookie name is what the check above turns on


def test_a_bad_side_can_be_discarded_and_recorded_again(tmp_path: Path) -> None:
    """Only from raw. A side in raw is a rip somebody can redo; a side in
    archive is the only copy there is."""
    service = a_service(tmp_path)
    side = service.layout.raw / "album" / "side-a.flac"
    r = post(
        build(service), "/api/rip/discard-side", service, {"slug": "album", "side": "a"}
    )
    assert r.status == 200 and r.json()["freed_bytes"] > 0
    assert not side.exists()


def test_a_side_in_the_archive_is_not_reachable_from_there(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    archived = service.layout.archive / "album"
    archived.mkdir(parents=True)
    (archived / "side-z.flac").write_bytes(b"fLaC" + b"\x00" * 100)
    r = post(
        build(service), "/api/rip/discard-side", service, {"slug": "album", "side": "z"}
    )
    assert r.status == 404
    assert (archived / "side-z.flac").is_file()
