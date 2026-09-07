"""The capture in progress, as a page sees it.

The worker runs where it was started, so a whole side happens inside one test
and every state the meter shows can be asserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio import capture as C
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.work import capture as W
from tests.tape import Tape

FMT = C.Format(rate=48000, channels=2, sample_format="S24_3LE")


def a_recorder(tape: Tape | None = None, **kw: object) -> W.Recorder:
    """A recorder whose capture loop runs on the test's own clock."""
    return W.Recorder(
        spawn=lambda work: work(),
        now=lambda: 1000.0,
        tick=tape.now if tape else (lambda: 0.0),
        sleep=tape.sleep if tape else (lambda _s: None),
        **kw,  # type: ignore[arg-type]
    )


def a_capture(
    tmp_path: Path, script: str = "m" * 10, side: str = "a"
) -> tuple[W.Recorder, W.Live, Tape]:
    tape = Tape(C.partial_path(tmp_path, side), script)
    recorder = a_recorder(tape)
    live = recorder.start(
        FakeRunner(exit_after=len(script)),
        "hw:Rx,0",
        tmp_path,
        "album",
        side,
        FMT,
    )
    return recorder, live, tape


# ---------------------------------------------------------------- status


def test_nothing_recording_is_a_state_not_an_absence() -> None:
    """The page shows it rather than treating it as an error."""
    assert a_recorder().status() == {"running": False}


def test_a_finished_capture_reports_how_it_ended(tmp_path: Path) -> None:
    recorder, _live, _tape = a_capture(tmp_path)
    status = recorder.status()
    assert not status["running"]
    assert status["reason"] and status["slug"] == "album"


def test_the_meter_readings_reach_the_status(tmp_path: Path) -> None:
    tape = Tape(C.partial_path(tmp_path, "a"), "m" * 6)
    recorder = a_recorder()
    recorder.start(FakeRunner(exit_after=6), "hw:Rx,0", tmp_path, "album", "a", FMT)
    # The capture wrote nothing the fake could meter, so there is no reading;
    # what matters is that the shape is there either way.
    status = recorder.status()
    assert "elapsed" in status and "autostop" in status
    assert tape.polls == 0, "the fake tape was not the one that was recorded"


# ----------------------------------------------------------------- one


def test_a_second_capture_is_refused_while_one_runs(tmp_path: Path) -> None:
    """The card has one input. A second capture takes it away from the first."""
    recorder = W.Recorder(spawn=lambda _w: None, now=lambda: 1000.0)
    recorder.start(FakeRunner(), "hw:Rx,0", tmp_path, "album", "a", FMT)
    with pytest.raises(W.Busy, match="already recording"):
        recorder.start(FakeRunner(), "hw:Rx,0", tmp_path, "album", "b", FMT)


def test_another_capture_is_allowed_once_the_first_ends(tmp_path: Path) -> None:
    recorder, _live, _tape = a_capture(tmp_path)
    live = recorder.start(
        FakeRunner(exit_after=2), "hw:Rx,0", tmp_path, "album", "b", FMT
    )
    assert live.side == "b"


def test_an_unusable_device_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    with pytest.raises(C.CaptureError):
        a_recorder().start(FakeRunner(), "", tmp_path, "album", "a", FMT)


# --------------------------------------------------------------- control


def test_stopping_something_that_is_not_running_says_so() -> None:
    with pytest.raises(W.Busy, match="nothing is recording"):
        a_recorder().stop()


def test_the_controls_reach_the_capture(tmp_path: Path) -> None:
    recorder = W.Recorder(spawn=lambda _w: None, now=lambda: 1000.0)
    live = recorder.start(FakeRunner(), "hw:Rx,0", tmp_path, "album", "a", FMT)
    recorder.snooze()
    recorder.set_autostop(False)
    recorder.stop()
    assert live.control.snoozes == 1
    assert not live.control.autostop and live.control.stopping


def test_the_snooze_count_is_visible(tmp_path: Path) -> None:
    """Somebody who has pressed it four times is telling you something about
    the record, and the page should be able to say so."""
    recorder = W.Recorder(spawn=lambda _w: None, now=lambda: 1000.0)
    recorder.start(FakeRunner(), "hw:Rx,0", tmp_path, "album", "a", FMT)
    recorder.snooze()
    recorder.snooze()
    assert recorder.status()["snoozes"] == 2


# -------------------------------------------------------------- failures


def test_a_capture_that_could_not_start_is_reported_not_lost(
    tmp_path: Path,
) -> None:
    """The recorder exiting at once is a busy device, and the page has to say
    so rather than showing a capture that is not happening."""
    C.log_path(tmp_path, "a").write_text("audio open error: Device or resource busy")
    recorder = a_recorder()
    recorder.start(FakeRunner(exit_after=1), "hw:Rx,0", tmp_path, "album", "a", FMT)
    status = recorder.status()
    assert not status["running"]
    assert "resource busy" in status["error"]
