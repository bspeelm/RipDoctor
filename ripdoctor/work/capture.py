"""The capture in progress, and the handle a page has on it.

One at a time: the sound card has one input, and a second capture would either
fail to open it or take it away from the first.

The loop runs here rather than in the browser. A meter that stopped when
somebody closed a laptop lid would stop in the middle of every side, and the
auto-stop with it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ripdoctor.audio import capture as C
from ripdoctor.audio import session as S
from ripdoctor.audio.runner import Runner
from ripdoctor.core import autostop as A


class Busy(Exception):
    """Something is already recording."""


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


@dataclass
class Live:
    """One capture, from the outside."""

    slug: str
    side: str
    device: str
    started: float
    control: S.Control
    stem: str = "side"
    reading: S.Reading | None = None
    outcome: S.Outcome | None = None
    error: str = ""

    @property
    def running(self) -> bool:
        return self.outcome is None and not self.error

    @property
    def stage(self) -> str:
        if self.error:
            return "failed"
        if self.outcome is None:
            return "recording"
        return "done" if self.outcome.path else "encoding"

    def as_dict(self, now: float, dwell: float, below: float) -> dict[str, Any]:
        out: dict[str, Any] = {
            "running": self.running,
            "stage": self.stage,
            "slug": self.slug,
            "side": self.side,
            "kind": self.stem,
            "device": self.device,
            "elapsed": round(now - self.started, 1),
            "autostop": self.control.autostop,
            "snoozes": self.control.snoozes,
            "dwell": dwell,
            # How long a quiet stretch has to run before the page says
            # something. Well short of the dwell, so there is time to press
            # snooze rather than watch it fire.
            "warn": round(dwell * 0.25),
            "below": below,
            "error": self.error,
        }
        if self.reading:
            out["levels"] = {
                "full": self.reading.levels.full,
                "band": self.reading.levels.band,
                "peak": self.reading.levels.peak,
            }
            out["music"] = self.reading.music
            out["quiet_for"] = self.reading.quiet_for
            out["warning"] = self.reading.warning
        if self.outcome:
            out["reason"] = self.outcome.reason
            out["seconds"] = self.outcome.seconds
            out["overruns"] = self.outcome.overruns
            out["overrun_ms"] = self.outcome.overrun_ms
            out["path"] = None if not self.outcome.path else str(self.outcome.path)
            out["bytes"] = self.outcome.bytes
            out["duration"] = self.outcome.seconds
            out["error"] = self.outcome.error or self.error
        return out


@dataclass
class Recorder:
    """Whatever is recording right now, or the last thing that did."""

    spawn: Callable[[Callable[[], None]], None] = _thread
    now: Callable[[], float] = time.time
    # The capture loop's own clock, separate from the wall clock above: it
    # measures elapsed time and must not move when the system clock does.
    tick: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    dwell: float = A.DWELL
    below: float = A.BELOW
    live: Live | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start(
        self,
        runner: Runner,
        device: str,
        album_dir: str | Path,
        slug: str,
        side: str,
        fmt: C.Format,
        *,
        autostop: bool = True,
        stem: str = "side",
        below: float = A.BELOW,
        max_seconds: float = A.MAX_SECONDS,
    ) -> Live:
        C.check_device(device)
        with self.lock:
            if self.live and self.live.running:
                raise Busy(f"already recording {self.live.slug} side {self.live.side}")
            live = Live(
                slug=slug,
                stem=stem,
                side=side,
                device=device,
                started=self.now(),
                control=S.Control(autostop=autostop),
            )
            self.live = live

        def work() -> None:
            try:
                live.outcome = S.record(
                    runner,
                    device,
                    album_dir,
                    side,
                    fmt,
                    stem=stem,
                    control=live.control,
                    on_reading=lambda r: setattr(live, "reading", r),
                    dwell=self.dwell,
                    below=below,
                    max_seconds=max_seconds,
                    now=self.tick,
                    sleep=self.sleep,
                )
            except Exception as e:  # carried, so the page can say what happened
                live.error = f"{type(e).__name__}: {e}"

        self.spawn(work)
        return live

    def status(self) -> dict[str, Any]:
        live = self.live
        if live is None:
            # Not recording is a state, not an absence: the page shows it.
            return {"running": False}
        return live.as_dict(self.now(), self.dwell, self.below)

    def _running(self) -> Live:
        if self.live is None or not self.live.running:
            raise Busy("nothing is recording")
        return self.live

    def stop(self) -> None:
        self._running().control.stop()

    def snooze(self) -> None:
        self._running().control.snooze()

    def set_autostop(self, on: bool) -> None:
        self._running().control.autostop = on
