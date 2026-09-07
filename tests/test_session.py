"""One capture, end to end, with no sound card.

The recorder is faked, the clock is injected, and the WAV grows a block at a
time under the loop that meters it - so the auto-stop behaviour that otherwise
takes twenty minutes of vinyl to observe is a table test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio import capture as C
from ripdoctor.audio import session as S
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core import autostop as A
from tests.tape import Tape

FMT = C.Format(rate=48000, channels=2, sample_format="S24_3LE")

# Long enough to arm the detector, per core/autostop.
ARMED = "m" * (A.ARM_READINGS + 2)


def run(
    tmp_path: Path,
    script: str,
    *,
    runner: FakeRunner | None = None,
    interrupt_after: int | None = None,
    autostop: bool = True,
    control: S.Control | None = None,
    **limits: float,
) -> tuple[S.Outcome, Tape, FakeRunner]:
    fake = runner or FakeRunner()
    tape = Tape(C.partial_path(tmp_path, "a"), script, interrupt_after=interrupt_after)
    outcome = S.record(
        fake,
        "hw:Rx,0",
        tmp_path,
        "a",
        FMT,
        autostop=autostop,
        control=control,
        now=tape.now,
        sleep=tape.sleep,
        **limits,
    )
    return outcome, tape, fake


# ------------------------------------------------------------ the loop


@pytest.fixture(scope="module")
def side(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[S.Outcome, Tape, FakeRunner]:
    """One recorded side, shared by everything that asks a question about it.

    Metering a side means an FFT per reading, so recording it once and making
    several assertions is the difference between a fast suite and a slow one.
    """
    return run(tmp_path_factory.mktemp("side"), ARMED + "q", dwell=20.0)


def test_the_side_stops_at_the_run_out(
    side: tuple[S.Outcome, Tape, FakeRunner],
) -> None:
    """The behaviour the whole reducer exists for, observed through the meter.

    Music, then a run-out that holds for the dwell, and the capture ends by
    itself with nobody watching.
    """
    outcome, tape, _ = side
    assert "run-out" in outcome.reason
    assert outcome.path is not None and outcome.path.name == "side-a.flac"
    assert tape.polls > A.ARM_READINGS


def test_the_meter_saw_the_music_before_it_saw_the_silence(
    side: tuple[S.Outcome, Tape, FakeRunner],
) -> None:
    outcome, _, _ = side
    music = [r for r in outcome.readings if r.music is not None]
    assert music, "the detector never armed on a side that is mostly music"
    assert music[-1].music is not None and music[-1].music > A.ARM_FLOOR
    assert outcome.readings[-1].quiet_for >= 20.0


def test_a_quiet_passage_shorter_than_the_dwell_does_not_stop(
    tmp_path: Path,
) -> None:
    """Records with genuinely quiet passages sit right on this gate."""
    fake = FakeRunner(exit_after=60)
    outcome, _, _ = run(tmp_path, ARMED + "q" * 20 + "m" * 10, runner=fake, dwell=120.0)
    assert "run-out" not in outcome.reason
    assert outcome.reason == "the recorder stopped"
    assert outcome.path is not None, "a side that ran its length was not encoded"


def test_auto_stop_off_still_leaves_the_hard_cap(tmp_path: Path) -> None:
    """Disarming the detector is for a record that is quiet all the way
    through. It is not permission to fill the disk."""
    outcome, _, _ = run(tmp_path, ARMED + "q", autostop=False, max_seconds=40.0)
    assert "hard cap" in outcome.reason


def test_a_capture_that_records_nothing_still_stops(tmp_path: Path) -> None:
    """No file at all: a device that opened and then produced silence.

    The meter has nothing to measure, so the reducer never arms - and the hard
    cap is the guard that is left.
    """
    fake = FakeRunner()
    outcome = S.record(
        fake,
        "hw:Rx,0",
        tmp_path,
        "a",
        FMT,
        now=lambda: 0.0,
        sleep=lambda _s: None,
        max_seconds=-1.0,
    )
    assert "hard cap" in outcome.reason
    assert outcome.path is None
    assert "nothing was captured" in outcome.error


# ------------------------------------------------------------ stopping


def test_ctrl_c_stops_the_record_not_the_program(tmp_path: Path) -> None:
    """And the side is still encoded. Anything else throws away the evening."""
    outcome, _, fake = run(tmp_path, "m" * 5, interrupt_after=4)
    assert outcome.reason == S.BY_HAND
    assert outcome.path is not None, "an interrupted side was discarded"
    assert fake.started[0].interrupted, "the recorder was left running"


def test_the_recorder_is_interrupted_rather_than_killed(
    side: tuple[S.Outcome, Tape, FakeRunner],
) -> None:
    """arecord backfills the WAV header - which is where the length lives - on
    an interrupt, and does not when it is killed."""
    _outcome, _tape, fake = side
    proc = fake.started[0]
    assert proc.interrupted and not proc.killed


def test_a_recorder_that_exits_at_once_is_reported_not_metered(
    tmp_path: Path,
) -> None:
    """A busy device exits immediately. Without this the start looks like a
    success and the meter reads an absence as a noise floor."""
    C.log_path(tmp_path, "a").write_text(
        "arecord: main:830: audio open error: Device or resource busy\n"
    )
    fake = FakeRunner(exit_after=1)
    with pytest.raises(C.CaptureError, match="Device or resource busy"):
        S.record(
            fake, "hw:Rx,0", tmp_path, "a", FMT, sleep=lambda _s: None, now=lambda: 0.0
        )
    assert not C.partial_path(tmp_path, "a").exists()


def test_the_encode_runs_once_the_capture_has_stopped(
    side: tuple[S.Outcome, Tape, FakeRunner],
) -> None:
    """Not before. An encoder reading a file still being written produces a
    file shorter than the record."""
    _outcome, _tape, fake = side
    encode = [i for i, c in enumerate(fake.calls) if c[0] == "ffmpeg"]
    assert encode == [1], "the encode did not run exactly once, after the capture"


# ------------------------------------------------------------- overruns


def test_overruns_are_counted_and_the_lost_time_reported(tmp_path: Path) -> None:
    """Every one of these is a slice of the record that is not in the file."""
    C.log_path(tmp_path, "a").write_text(
        "overrun!!! (at least 8.235 ms long)\noverrun!!! (at least 12.100 ms long)\n"
    )
    outcome, _, _ = run(tmp_path, "m" * 3, dwell=20.0, runner=FakeRunner(exit_after=4))
    assert outcome.overruns == 2
    assert outcome.overrun_ms == pytest.approx(20.3)
    assert not outcome.clean


def test_a_capture_with_no_complaints_is_clean(
    side: tuple[S.Outcome, Tape, FakeRunner],
) -> None:
    outcome, _, _ = side
    assert outcome.clean and outcome.overruns == 0


# ------------------------------------------------------------- control


def test_a_hand_on_stop_ends_the_side_and_keeps_it(tmp_path: Path) -> None:
    hand = S.Control()
    tape = Tape(C.partial_path(tmp_path, "a"), "m" * 40)
    fake = FakeRunner()

    def sleep(seconds: float) -> None:
        tape.sleep(seconds)
        if tape.polls >= 5:
            hand.stop()

    outcome = S.record(
        fake, "hw:Rx,0", tmp_path, "a", FMT, control=hand, now=tape.now, sleep=sleep
    )
    assert outcome.reason == S.BY_HAND
    assert outcome.path is not None and fake.started[0].interrupted


def test_a_snooze_restarts_the_dwell_rather_than_disarming(tmp_path: Path) -> None:
    """The human says "this is still the song". The side keeps recording with
    the safety net intact, which is not what turning auto-stop off does."""
    hand = S.Control()
    tape = Tape(C.partial_path(tmp_path, "a"), ARMED + "q")

    def sleep(seconds: float) -> None:
        tape.sleep(seconds)
        # Press it once, just before the dwell would have expired.
        if tape.polls == A.ARM_READINGS + 18 and not hand.snoozes:
            hand.snooze()

    outcome = S.record(
        FakeRunner(exit_after=A.ARM_READINGS + 30),
        "hw:Rx,0",
        tmp_path,
        "a",
        FMT,
        control=hand,
        dwell=20.0,
        now=tape.now,
        sleep=sleep,
    )
    assert hand.snoozes == 1
    assert "run-out" not in outcome.reason, "the snooze did not restart the clock"


def test_auto_stop_can_be_turned_off_through_the_control(tmp_path: Path) -> None:
    hand = S.Control(autostop=False)
    outcome, _tape, _fake = run(
        tmp_path, ARMED + "q", dwell=20.0, max_seconds=60.0, control=hand
    )
    assert "hard cap" in outcome.reason
