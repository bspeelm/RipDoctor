"""Deciding when a side has ended, without a clock.

A capture otherwise runs until someone presses stop, and a forgotten one fills
the disk at about 11.5 MB a minute. Two guards: a silence gate that fires early
when it safely can, and a hard cap that always works. The hard cap is the
reliable one.

The gate is relative to the side's own music level, never absolute - run-out
measured -41 to -85 dB across fifteen sides, and on one it was louder than the
quietest music on the same record. docs/method.md has the measurements and
tests/test_autostop.py checks each of them.

The whole decision is a fold over (band level, elapsed seconds). Nothing here
sleeps, reads a clock or touches a process.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

BELOW = 20.0  # dB under this side's music level that counts as silence
DWELL = 120.0  # how long that must hold before stopping
MAX_SECONDS = 35 * 60.0  # hard cap, for a needle that never reaches run-out
ARM_READINGS = 30  # readings needed before the music estimate means anything
ARM_FLOOR = -60.0  # ...and it must look like music, not an empty room
FLOOR_WARN_AFTER = 30.0  # seconds of nothing-but-floor before saying so
MUSIC_PCT = 0.85
HISTORY_MAX = 5400  # about 90 minutes of one-second readings


@dataclass(frozen=True, slots=True)
class State:
    """What the detector has seen so far.

    `quiet_since` holds an elapsed time, not a wall clock, so a capture can be
    replayed or fast-forwarded and reach the same decision.
    """

    history: tuple[float, ...] = ()
    quiet_since: float | None = None
    music: float | None = None
    warning: str | None = None

    @property
    def armed(self) -> bool:
        """Is the music estimate usable yet?

        Not armed means the detector holds its fire. Getting a needle down takes
        tens of seconds, and arming during that would stop the capture before the
        record started.
        """
        return (
            len(self.history) >= ARM_READINGS
            and self.music is not None
            and self.music >= ARM_FLOOR
        )

    def quiet_for(self, elapsed: float) -> float:
        return 0.0 if self.quiet_since is None else max(0.0, elapsed - self.quiet_since)


def step(
    state: State,
    band_db: float,
    elapsed: float,
    *,
    enabled: bool = True,
    below: float = BELOW,
    dwell: float = DWELL,
    max_seconds: float = MAX_SECONDS,
) -> tuple[State, str | None]:
    """Fold one reading in. Returns the new state and a stop reason, or None.

    `enabled` turns off only the silence gate. Some records have ambient
    stretches that run 20 dB under the loud parts and are still the song, sitting
    exactly on the gate; for those the meter must keep working while the trigger
    is held. The hard cap still applies - that one guards against a forgotten
    capture, not against quiet music.
    """
    if elapsed > max_seconds:
        return state, f"hard cap: {max_seconds / 60:.0f} minutes"

    history = (*state.history, band_db)[-HISTORY_MAX:]
    ordered = sorted(history)
    music = ordered[min(len(ordered) - 1, int(len(ordered) * MUSIC_PCT))]
    state = replace(state, history=history, music=music, warning=None)

    if len(history) < ARM_READINGS:
        return replace(state, quiet_since=None), None

    if music < ARM_FLOOR:
        # Nothing that looks like music. Never arm here, or the minutes spent
        # getting the needle down would trip it immediately.
        warning = None
        if elapsed > FLOOR_WARN_AFTER:
            # One bad capture sat at -77 dB for its whole length and the meter
            # said so the entire time, to nobody. Past thirty seconds this is a
            # wrong input, a dead cable, or an arm still in its rest.
            warning = (
                f"no music-like signal after {elapsed:.0f}s - 1-3 kHz sits at "
                f"{music:.1f} dB, floor is {ARM_FLOOR:.0f}. Check the input and "
                "that the needle is down."
            )
        return replace(state, quiet_since=None, warning=warning), None

    if not enabled:
        return replace(state, quiet_since=None), None

    if band_db < music - below:
        since = elapsed if state.quiet_since is None else state.quiet_since
        state = replace(state, quiet_since=since)
        if elapsed - since >= dwell:
            return state, (
                f"{dwell / 60:.0f} min of run-out ({below:.0f} dB below the music)"
            )
        return state, None

    return replace(state, quiet_since=None), None


def run(
    readings: list[tuple[float, float]], **kw: object
) -> tuple[State, str | None, float | None]:
    """Fold a whole capture. Returns (final state, stop reason, elapsed at stop).

    A convenience for replaying a side, which is what lets the detector be
    checked against a recorded envelope rather than only a constructed one.
    """
    state = State()
    for band_db, elapsed in readings:
        state, reason = step(state, band_db, elapsed, **kw)  # type: ignore[arg-type]
        if reason:
            return state, reason, elapsed
    return state, None, None
