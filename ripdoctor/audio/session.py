"""One capture, from needle down to FLAC. ADR-031, ADR-032, ADR-033.

Where the live meter meets the auto-stop reducer. The decision itself is pure
and lives in core/autostop; this is only the loop that feeds it. It runs where
the capture runs, not in a browser - a meter that stopped when somebody closed
a laptop lid would stop in the middle of every side.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ripdoctor.audio import capture as C
from ripdoctor.audio.runner import Runner
from ripdoctor.core import autostop as A
from ripdoctor.core.meter import Levels

POLL = 1.0

# arecord exits at once if the device is busy or has no clock. Without a pause
# the start looks like a success, the file never grows, and the meter reads a
# noise floor that is really an absence.
SETTLE = 0.4

BY_HAND = "stopped by hand"


@dataclass(frozen=True, slots=True)
class Reading:
    """One second of the capture, as the meter shows it."""

    elapsed: float
    levels: Levels
    music: float | None
    quiet_for: float
    warning: str | None


@dataclass
class Outcome:
    """What the capture turned out to be."""

    path: Path | None = None
    seconds: float = 0.0
    reason: str = ""
    error: str = ""
    overruns: int = 0
    overrun_ms: float = 0.0
    readings: list[Reading] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.path is not None and self.overruns == 0


def record(
    runner: Runner,
    device: str,
    album_dir: str | Path,
    letter: str,
    fmt: C.Format,
    *,
    autostop: bool = True,
    on_reading: Callable[[Reading], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll: float = POLL,
    settle: float = SETTLE,
    below: float = A.BELOW,
    dwell: float = A.DWELL,
    max_seconds: float = A.MAX_SECONDS,
) -> Outcome:
    """Record one side, watching it, and encode what was captured.

    Returns an Outcome whatever ends the capture - the reducer, the hard cap,
    the recorder exiting, or a hand on Ctrl-C. Every path out of here encodes
    what is on disk rather than discarding it. ADR-033.
    """
    proc, wav = C.start(runner, device, album_dir, letter, fmt, log=True)
    log = C.log_path(album_dir, letter)
    outcome = Outcome()

    sleep(settle)
    if proc.poll() is not None:
        detail = log.read_text(errors="replace").strip().splitlines()[-1:] or [""]
        wav.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
        raise C.CaptureError(f"the recorder exited immediately: {detail[0][:200]}")

    started = now()
    state = A.State()
    try:
        while proc.poll() is None:
            sleep(poll)
            elapsed = now() - started
            measured = C.meter(wav, fmt)
            if measured is None:
                # Not yet a block to measure. The hard cap still has to apply,
                # or a capture reading nothing would run until the disk filled.
                state, reason = A.step(
                    state,
                    A.ARM_FLOOR - 1,
                    elapsed,
                    enabled=False,
                    below=below,
                    dwell=dwell,
                    max_seconds=max_seconds,
                )
                if reason:
                    outcome.reason = reason
                    break
                continue
            levels, _ = measured
            state, reason = A.step(
                state,
                levels.band,
                elapsed,
                enabled=autostop,
                below=below,
                dwell=dwell,
                max_seconds=max_seconds,
            )
            reading = Reading(
                elapsed=round(elapsed, 1),
                levels=levels,
                music=state.music if state.armed else None,
                quiet_for=round(state.quiet_for(elapsed), 1),
                warning=state.warning,
            )
            outcome.readings.append(reading)
            if on_reading is not None:
                on_reading(reading)
            if reason:
                outcome.reason = reason
                break
    except KeyboardInterrupt:
        # Ctrl-C stops the record, not the program. Leaving here without
        # stopping the recorder would orphan it and leave the WAV headerless.
        outcome.reason = BY_HAND

    outcome.seconds = round(now() - started, 1)
    if not outcome.reason:
        outcome.reason = "the recorder stopped"
    C.stop(proc)
    outcome.overruns, outcome.overrun_ms = C.overruns(log)
    try:
        outcome.path = C.finish(runner, album_dir, letter)
    except C.CaptureError as e:
        # Not raised. A capture that ended with nothing on disk still has a
        # reason it ended, and that reason is what the human needs to see.
        outcome.error = str(e)
    log.unlink(missing_ok=True)
    return outcome
