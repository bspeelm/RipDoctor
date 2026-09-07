"""Recording from a browser: the meter, the controls and salvage.

No sound card. The capture loop runs on the test's clock, so a whole side
happens inside a request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio import capture as C
from ripdoctor.audio import session as SO
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.store import files as F
from ripdoctor.web import auth as A
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.work.capture import Recorder
from tests.tape import Tape
from tests.test_store import a_plan, a_spec
from tests.test_web_routes import a_service, get, post


def a_recorder(tape: Tape | None = None) -> Recorder:
    return Recorder(
        spawn=lambda work: work(),
        now=lambda: 1000.0,
        tick=tape.now if tape else (lambda: 0.0),
        sleep=tape.sleep if tape else (lambda _s: None),
    )


def a_named_pool(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A record with a name and nothing captured under it yet."""
    service = a_service(tmp_path)
    for take in (service.layout.raw / "album").iterdir():
        take.unlink()
    F.remember(service.layout, "album", album="Second", artist="First")
    return service


def a_stalled_recorder() -> Recorder:
    """A recorder whose capture loop never runs.

    Nothing puts the outcome down, so settle() waits out its whole limit -
    which is real time unless the sleep it polls on is one of these.
    """
    return Recorder(spawn=lambda _w: None, now=lambda: 1000.0, sleep=lambda _s: None)


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
    assert body == {"running": False, "stage": "idle"}


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
    service.recorder = a_stalled_recorder()
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
    service.recorder = a_stalled_recorder()
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
    found = body["orphans"][0]
    assert (found["slug"], found["side"], found["bytes"]) == ("album", "b", 8004)
    # How long it is, which is what says whether it is most of a side or a
    # false start. A WAV that was never closed has no length in its header.
    assert found["seconds"] >= 0 and found["recording"] is False


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
    by_side = {x["side"]: x for x in body["sides"]}
    assert by_side["a"]["finished"] and by_side["a"]["bytes"] > 0
    assert not by_side["b"]["finished"], "a capture in progress is not a side"
    assert by_side["b"]["slug"] == "album"


def test_a_record_with_nothing_captured_lists_nothing(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    body = get(build(service), "/api/rip/sides/missing", service).json()
    assert body["sides"] == []


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


# ------------------------------------------------- the device is agreed on


def test_the_configured_device_is_used_when_none_is_asked_for(
    tmp_path: Path,
) -> None:
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    r = post(build(service), "/api/rip/start", service, {"slug": "album", "side": "b"})
    assert r.status == 202 and r.json()["device"] == "hw:Rx,0"


def test_a_device_that_is_not_the_configured_one_is_refused(tmp_path: Path) -> None:
    """The mistake this exists to prevent. On 2026-08-23 a rip ran for 162
    seconds against an onboard codec whose input was set to Rear Mic, and
    everything needed to catch it was already known and used only to sort a
    dropdown - which is exactly what happened again the first time somebody
    pressed Start on this page.
    """
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    r = post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "b", "device": "hw:PCH,0"},
    )
    assert r.status == 409
    assert "hw:Rx,0" in r.json()["error"] and "hw:PCH,0" in r.json()["error"]
    assert service.recorder.live is None, "it started anyway"


def test_another_device_is_allowed_when_it_is_asked_for_twice(
    tmp_path: Path,
) -> None:
    """A refusal that cannot be overridden is a refusal that gets worked around
    by editing the configuration mid-session."""
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    r = post(
        build(service),
        "/api/rip/start",
        service,
        {
            "slug": "album",
            "side": "b",
            "device": "hw:PCH,0",
            "force_device": True,
        },
    )
    assert r.status == 202 and r.json()["device"] == "hw:PCH,0"


def test_a_format_the_card_cannot_take_is_refused_before_the_needle_is_down(
    tmp_path: Path,
) -> None:
    """It otherwise fails inside arecord, seconds after somebody set the arm
    down - and the message comes back as `audio open error`."""
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    r = post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "b", "format": "S24_LE"},
    )
    assert r.status == 400 and "S24_LE" in r.json()["error"]


def test_the_rate_and_format_asked_for_are_the_ones_recorded(
    tmp_path: Path,
) -> None:
    """The form shows them, so the form has to mean something."""
    service, tape = recording_service(tmp_path)
    service.recorder = a_recorder(tape)
    post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "b", "rate": 44100, "format": "S16_LE"},
    )
    started = " ".join(service.runner.calls[0])
    assert "44100" in started and "S16_LE" in started


def test_the_listing_says_which_one_is_configured(tmp_path: Path) -> None:
    """The page picked the first in the list, which is whatever the
    motherboard calls its own audio."""
    service = a_service(tmp_path)
    service.settings = __import__("dataclasses").replace(
        service.settings, capture_device="hw:Rx,0"
    )
    service.runner = FakeRunner().expect(
        "-l",
        stdout=b"card 0: PCH [HDA Intel PCH], device 0: ALC1150 [ALC1150]\n"
        b"card 1: Rx [SAVITECH], device 0: USB Audio [USB Audio]\n",
    )
    body = get(build(service), "/api/rip/devices", service).json()
    assert [d["id"] for d in body["devices"]] == ["hw:PCH,0", "hw:Rx,0"]
    assert [d["configured"] for d in body["devices"]] == [False, True]
    assert body["rate"] and body["format"]


# ------------------------------------------------ what the page reads after


def test_a_finished_capture_reports_what_it_wrote(tmp_path: Path) -> None:
    """Stopping is not finishing. Encoding a twenty-minute side takes most of a
    minute, and the page has to be able to wait for it and then say what
    landed - not report `NaN MB` for a file that does not exist yet."""
    service, _tape = recording_service(tmp_path, "m" * 6)

    class Encoding(FakeRunner):
        """A fake ffmpeg that leaves the side it was told to write."""

        def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
            args = [str(a) for a in argv]
            if args[0] == "ffmpeg" and args[-1].endswith(".flac"):
                Path(args[-1]).write_bytes(b"fLaC" + b"\x00" * 5000)
            return super().run(argv, stdin=stdin, timeout=timeout)

    service.runner = Encoding(exit_after=6)
    app = build(service)
    post(app, "/api/rip/start", service, {"slug": "album", "side": "b"})
    st = get(app, "/api/rip/status", service).json()
    assert st["stage"] == "done", st.get("error")
    assert st["path"] and st["path"].endswith("side-b.flac")
    assert st["bytes"] > 0 and st["duration"] > 0
    assert "overruns" in st


def test_the_stage_says_when_it_is_still_encoding(tmp_path: Path) -> None:
    """What the page polls on. Without it, a stop looks finished the moment it
    is asked for, and the capture is still on disk as a WAV - which is how a
    completed rip gets offered back as wreckage to salvage."""
    from ripdoctor.audio import session as S

    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    live = service.recorder.start(
        FakeRunner(), "hw:Rx,0", tmp_path, "album", "b", C.Format()
    )
    assert live.stage == "recording"
    live.outcome = S.Outcome(path=None, reason="stopped by hand")
    assert live.stage == "encoding"
    live.outcome = S.Outcome(path=tmp_path / "side-b.flac", reason="stopped by hand")
    assert live.stage == "done"


# --------------------------------------------------- names and back-outs


def test_starting_a_capture_records_what_the_record_is_called(
    tmp_path: Path,
) -> None:
    """The earliest moment the names are known, and the one that survives a
    reload. Without it a person is left with audio, a slug and no way to start
    the second side of the record they just recorded the first side of."""
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "b", "artist": "First", "album": "Second"},
    )
    spec = F.read_spec(service.layout.spec_file("album"))
    assert (spec.artist, spec.album, spec.sides) == ("First", "Second", ())


def test_a_punch_does_not_invent_a_name(tmp_path: Path) -> None:
    """A punch is one track of a record that already has a name."""
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "1", "kind": "punch", "artist": "First"},
    )
    assert not service.layout.spec_file("album").is_file()


def test_a_re_rip_does_not_overwrite_the_saved_cut(tmp_path: Path) -> None:
    service, _tape = recording_service(tmp_path)
    service.recorder = a_stalled_recorder()
    F.save(service.layout, "album", a_spec(), a_plan())
    post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "b", "artist": "First", "album": "Second"},
    )
    assert F.read_spec(service.layout.spec_file("album")).sides == a_spec().sides


def test_abandoning_a_first_take_forgets_the_record(tmp_path: Path) -> None:
    service, app = running_service(tmp_path)
    (service.layout.raw / "album" / "side-a.flac").unlink()
    F.remember(service.layout, "album", album="Second", artist="First")
    partial = C.partial_path(service.layout.raw / "album", "b")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"RIFF" + b"\x00" * 8000)
    body = post(app, "/api/rip/abandon", service).json()
    assert not service.layout.spec_file("album").is_file()
    assert not (service.layout.raw / "album").exists()
    assert "forgotten" in body["note"]


def test_abandoning_a_later_take_keeps_the_record(tmp_path: Path) -> None:
    service, app = running_service(tmp_path)
    F.remember(service.layout, "album", album="Second", artist="First")
    album = service.layout.raw / "album"
    album.mkdir(parents=True, exist_ok=True)
    (album / "side-a.flac").write_bytes(b"fLaC")
    C.partial_path(album, "b").write_bytes(b"RIFF" + b"\x00" * 8000)
    body = post(app, "/api/rip/abandon", service).json()
    assert service.layout.spec_file("album").is_file()
    assert body["note"] == ""


def test_abandoning_says_what_it_threw_away(tmp_path: Path) -> None:
    """It used to say `side undefined after NaN`, because the page read four
    fields the answer did not carry."""
    service, app = running_service(tmp_path)
    partial = C.partial_path(service.layout.raw / "album", "b")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"RIFF" + b"\x00" * 8000)
    body = post(app, "/api/rip/abandon", service).json()
    assert body["side"] == "b" and body["kind"] == "side"
    assert body["freed_bytes"] == 8004 and body["seconds"] == 0.0


def test_abandoning_does_not_leave_the_side_behind(tmp_path: Path) -> None:
    """The auto-stop and Abandon race for the same file, and the auto-stop
    wins by encoding a side somebody had just said to throw away."""
    service, _tape = recording_service(tmp_path)
    album = service.layout.raw / "album"
    landed = album / "side-b.flac"
    recorder = a_stalled_recorder()

    def lands(_s: float) -> None:
        live = recorder.live
        if live is not None and live.outcome is None:
            landed.write_bytes(b"fLaC" + b"\x00" * 400)
            live.outcome = SO.Outcome(reason="quiet", seconds=12.0, path=landed)

    recorder.sleep = lands
    service.recorder = recorder
    app = build(service)
    post(app, "/api/rip/start", service, {"slug": "album", "side": "b"})
    body = post(app, "/api/rip/abandon", service).json()
    assert not landed.exists() and "encode" in body["note"]
    assert body["seconds"] == 12.0


def test_discarding_the_last_side_forgets_the_record(tmp_path: Path) -> None:
    service = a_named_pool(tmp_path)
    (service.layout.raw / "album" / "side-b.flac").write_bytes(b"fLaC")
    body = post(
        build(service), "/api/rip/discard-side", service, {"slug": "album", "side": "b"}
    ).json()
    assert not service.layout.spec_file("album").is_file()
    assert "the name went too" in body["notes"][-1]


def test_discarding_one_of_two_sides_keeps_the_record(tmp_path: Path) -> None:
    service = a_named_pool(tmp_path)
    album = service.layout.raw / "album"
    for letter in ("a", "b"):
        (album / f"side-{letter}.flac").write_bytes(b"fLaC")
    post(
        build(service), "/api/rip/discard-side", service, {"slug": "album", "side": "b"}
    )
    assert service.layout.spec_file("album").is_file()


def test_discarding_a_partial_forgets_a_record_with_nothing_else(
    tmp_path: Path,
) -> None:
    service = a_named_pool(tmp_path)
    partial = C.partial_path(service.layout.raw / "album", "b")
    partial.write_bytes(b"RIFF" + b"\x00" * 8000)
    body = post(
        build(service), "/api/rip/discard", service, {"slug": "album", "side": "b"}
    ).json()
    assert not service.layout.spec_file("album").is_file()
    assert body["freed_bytes"] == 8004 and "forgotten" in body["note"]


def test_a_punch_orphan_can_be_thrown_away(tmp_path: Path) -> None:
    """It was listed and neither button under it worked: both looked for a
    side by that number, and a punch is not a side."""
    service, _tape = recording_service(tmp_path)
    album = service.layout.raw / "album"
    album.mkdir(parents=True, exist_ok=True)
    C.partial_path(album, "7", "punch").write_bytes(b"RIFF" + b"\x00" * 8000)
    app = build(service)
    found = get(app, "/api/rip/orphans", service).json()["orphans"][0]
    assert found["kind"] == "punch" and found["side"] == "7"
    gone = {"slug": "album", "side": "7", "kind": "punch"}
    r = post(app, "/api/rip/discard", service, gone)
    assert r.status == 200 and not C.partial_path(album, "7", "punch").exists()


@pytest.mark.parametrize("what", ["salvage", "discard"])
def test_the_capture_being_written_is_not_one_to_act_on(
    tmp_path: Path, what: str
) -> None:
    """Both take the file out from under arecord: salvage encodes what has
    arrived so far and unlinks it, and discard simply unlinks it."""
    service, app = running_service(tmp_path)
    partial = C.partial_path(service.layout.raw / "album", "b")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"RIFF" + b"\x00" * 8000)
    r = post(app, f"/api/rip/{what}", service, {"slug": "album", "side": "b"})
    assert r.status == 409 and "recording now" in r.json()["error"]
    assert partial.exists()


def test_salvaging_says_which_side_it_wrote(tmp_path: Path) -> None:
    service, _tape = recording_service(tmp_path)
    album = service.layout.raw / "album"
    album.mkdir(parents=True, exist_ok=True)
    C.partial_path(album, "b").write_bytes(b"RIFF" + b"\x00" * 2_000_000)
    body = post(
        build(service), "/api/rip/salvage", service, {"slug": "album", "side": "b"}
    ).json()
    assert body["side"] == "b" and body["duration"] > 0
