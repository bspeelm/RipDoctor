"""Auto-stop: the calibration facts, as a table.

Each of these corresponds to a measurement recorded when the detector was tuned
across fifteen archived sides. They are the reason the constants are what they
are, and until now they existed only as prose in a comment. With time injected
they run in milliseconds, with no clock, no sound card and no capture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.core import autostop as A
from ripdoctor.core.envelope import decode

FIXTURES = Path(__file__).parent / "fixtures" / "sides"


def readings(
    segments: list[tuple[float, float]], every: float = 1.0
) -> list[tuple[float, float]]:
    """(band dB, elapsed) once a second, from (seconds, dB) segments."""
    out: list[tuple[float, float]] = []
    t = 0.0
    for secs, db in segments:
        for _ in range(int(secs / every)):
            out.append((db, t))
            t += every
    return out


MUSIC = -30.0


def side(
    music_secs: float, then: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    return readings([(music_secs, MUSIC), *then])


# ------------------------------------------------------- the hard cap


def test_the_hard_cap_fires_on_a_capture_that_never_goes_quiet() -> None:
    """The guard that always works: a needle that never reaches the run-out."""
    _, reason, at = A.run(side(40 * 60, []))
    assert reason and "hard cap" in reason
    assert at == pytest.approx(A.MAX_SECONDS + 1.0, abs=2.0)


def test_the_hard_cap_applies_even_with_the_silence_gate_disabled() -> None:
    """Turning off auto-stop protects quiet music, not a forgotten capture."""
    _, reason, _ = A.run(side(40 * 60, []), enabled=False)
    assert reason and "hard cap" in reason


# --------------------------------------------------- the measured gate


def test_music_175_db_down_for_two_minutes_does_not_stop_the_capture() -> None:
    """The measurement that set the gate.

    One side sits 17.5 dB below its own music level for two continuous minutes,
    a third of the way in, and is still the song. A gate of 18 dB stops that
    record mid-song; 20 dB does not.
    """
    _, reason, _ = A.run(
        side(300, [(130, MUSIC - 17.5), (300, MUSIC)]),
    )
    assert reason is None, "a gate this tight would stop a record mid-song"


@pytest.mark.parametrize(
    ("gate", "stops"),
    [
        (17.0, True),  # tighter than the dip: stops the record mid-song
        (17.5, False),  # exactly at it: the comparison is strict
        (18.0, False),
        (A.BELOW, False),  # the shipped gate, with 2.5 dB in hand
    ],
)
def test_where_the_margin_actually_is(gate: float, stops: bool) -> None:
    """The 2.5 dB of margin, made arithmetic instead of assumed.

    The tuning note says "a gate of 18 dB stops that record mid-song", which is
    loose: a signal 17.5 dB down does not trip an 18 dB gate, because the
    comparison is strict. What is true is that the margin between the quietest
    real music seen and the shipped gate is 2.5 dB, and a gate below 17.5 stops
    a record that is still playing.
    """
    _, reason, _ = A.run(side(300, [(130, MUSIC - 17.5)]), below=gate)
    assert (reason is not None) is stops


def test_dead_air_for_the_full_dwell_stops_the_capture() -> None:
    """What the detector reliably catches: a lifted needle, about 50 dB down."""
    _, reason, at = A.run(side(300, [(200, MUSIC - 50)]))
    assert reason and "run-out" in reason
    assert at == pytest.approx(300 + A.DWELL, abs=2.0)


def test_quiet_shorter_than_the_dwell_does_not_stop() -> None:
    _, reason, _ = A.run(side(300, [(A.DWELL - 20, MUSIC - 50), (120, MUSIC)]))
    assert reason is None


def test_the_dwell_restarts_when_the_music_comes_back() -> None:
    """Otherwise two separate quiet passages would add up to a stop."""
    quiet = A.DWELL - 10
    _, reason, _ = A.run(
        side(300, [(quiet, MUSIC - 50), (5, MUSIC), (quiet, MUSIC - 50), (60, MUSIC)])
    )
    assert reason is None, "two near-misses were allowed to accumulate"


# ------------------------------------------------------------- arming


def test_the_detector_does_not_arm_before_the_needle_is_down() -> None:
    """Getting a needle down takes tens of seconds of near-silence.

    Arming during that stops the capture before the record has started.
    """
    state, reason, _ = A.run(readings([(200, -77.0)]))
    assert reason is None
    assert not state.armed
    assert state.quiet_since is None


def test_a_capture_with_no_music_like_signal_says_so_after_thirty_seconds() -> None:
    """The wrong-input case, caught on any machine with no device heuristic.

    One capture ran its whole length at -77 dB and the meter reported it the
    entire time, to nobody. This is the check that replaces guessing at device
    names.
    """
    state, _, _ = A.run(readings([(120, -77.0)]))
    assert state.warning is not None
    assert "no music-like signal" in state.warning
    assert "-77" in state.warning and "needle is down" in state.warning


def test_the_warning_is_withheld_for_the_first_thirty_seconds() -> None:
    state, _, _ = A.run(readings([(25, -77.0)]))
    assert state.warning is None, "a needle being cued is not a fault"


def test_the_warning_clears_once_music_arrives() -> None:
    state, _, _ = A.run(readings([(60, -77.0), (60, MUSIC)]))
    assert state.warning is None
    assert state.armed


def test_arming_needs_enough_readings_not_just_a_loud_one() -> None:
    state, _, _ = A.run(readings([(A.ARM_READINGS - 5, MUSIC)]))
    assert not state.armed


# ---------------------------------------------------------- the switch


def test_disabling_the_gate_keeps_the_meter_live() -> None:
    """For records whose ambient stretches sit exactly on the gate.

    The trigger is held, but music level and history must keep updating or the
    meter goes dead just when it is most needed.
    """
    state, reason, _ = A.run(side(300, [(400, MUSIC - 50)]), enabled=False)
    assert reason is None
    assert state.music is not None and state.armed
    assert len(state.history) > 300


# ------------------------------------------------------- real vinyl


def test_no_archived_side_trips_the_detector_before_its_music_ends() -> None:
    """The claim the gate was tuned to satisfy, checked against real sides.

    Replaying each side's band lane as if it were a live capture, the detector
    must not fire while the record is still playing. A side that stops early
    would have been truncated in the real world.
    """
    checked = early = 0
    for path in sorted(FIXTURES.glob("*.env")):
        lanes = decode(path.read_bytes())
        if len(lanes.band) < 2000:
            continue

        # One reading a second, as the live monitor takes them.
        per_second = max(1, int(1.0 / lanes.band.window))
        seq = [
            (lanes.band.levels[i], i * lanes.band.window)
            for i in range(0, len(lanes.band), per_second)
        ]
        _, reason, at = A.run(seq)

        checked += 1
        # Firing near the end is correct - that is the run-out.
        fired_early = (
            reason is not None
            and "run-out" in reason
            and at is not None
            and at < 0.9 * lanes.band.duration
        )
        if fired_early:
            early += 1

    assert checked >= 15, "no sides were examined; the pattern has drifted"
    assert early == 0, f"{early} of {checked} sides would have been cut short"


def test_a_short_cap_is_not_reported_as_zero_minutes() -> None:
    """Found by setting a twenty-four second cap to test a capture: it stopped
    correctly and said "hard cap: 0 minutes", which reads as a bug in the thing
    that just worked."""
    _state, reason = A.step(A.State(), -30.0, 25.0, max_seconds=24.0)
    assert reason is not None and "0 minutes" not in reason
    assert "24s" in reason


def test_a_real_cap_is_still_read_in_minutes() -> None:
    _state, reason = A.step(A.State(), -30.0, 2200.0, max_seconds=2100.0)
    assert reason is not None and "35 min" in reason
