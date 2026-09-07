"""One background job per record, and the table of what is running.

Split, import and archive each take minutes. Run inside the request they pin a
connection for their whole duration and the browser waits on it hoping neither
end gives up first, so they run on a worker and the browser polls.

There is one runner. The predecessor had two, each with its own table, its own
prune and its own thirty-minute constant, and a job started through one was
invisible to the other.

Two things this deliberately does not do. It does not queue: a second job for a
record is refused while the caller is still there to be told, rather than being
accepted and silently stalled. And it does not translate failures into status
codes, because once the work is off the request thread there is no response left
to put a code on - the message is carried instead, which is the part anyone
reads.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# How long a finished job stays pollable: long enough that a browser which
# reloaded mid-operation can still collect the result, short enough that the
# table does not grow for the life of the process.
KEEP = 30 * 60.0


class Busy(Exception):
    """Something is already running for this record."""


@dataclass
class Job:
    """One unit of work, and everything a progress bar needs."""

    slug: str
    op: str
    started: float
    total: int = 0
    finished: int = 0
    current: str = ""
    detail: str = ""
    result: Any = None
    error: str = ""
    ended: float | None = None
    done: bool = False
    skipped: list[str] = field(default_factory=list)

    def step(self, current: str = "", detail: str = "") -> None:
        self.current = current
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "op": self.op,
            "done": self.done,
            "error": self.error,
            "result": self.result,
            "total": self.total,
            "finished": self.finished,
            "current": self.current,
            "detail": self.detail,
            "skipped": list(self.skipped),
            "started": round(self.started, 3),
            "ended": None if self.ended is None else round(self.ended, 3),
        }


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


@dataclass
class Jobs:
    """The running and recently finished jobs, one per record.

    A job that is not done is the claim on that record; there is no second
    table of locks to fall out of step with this one.
    """

    keep: float = KEEP
    now: Callable[[], float] = time.time
    spawn: Callable[[Callable[[], None]], None] = _thread
    running: dict[str, Job] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def status(self, slug: str) -> Job | None:
        with self.lock:
            return self.running.get(slug)

    def start(self, slug: str, op: str, work: Callable[[Job], Any]) -> Job:
        """Claim the record and run `work` on a worker thread.

        The claim is taken here, on the caller's thread, so a conflict is
        refused while there is still a request to refuse it to.
        """
        self.prune()
        with self.lock:
            current = self.running.get(slug)
            if current and not current.done:
                raise Busy(f"{current.op} is already running for {slug}")
            job = Job(slug=slug, op=op, started=self.now())
            self.running[slug] = job

        def run() -> None:
            try:
                job.result = work(job)
            except Exception as e:  # carried into the job, not swallowed
                job.error = f"{type(e).__name__}: {e}"
            finally:
                # Order matters. `done` is set last, so a client that polls,
                # sees it finished and immediately starts the next operation
                # cannot arrive while this one still holds the record.
                job.current = ""
                job.ended = self.now()
                job.done = True

        self.spawn(run)
        return job

    def prune(self) -> int:
        """Forget jobs nobody is coming back for."""
        cutoff = self.now() - self.keep
        with self.lock:
            stale = [
                slug
                for slug, job in self.running.items()
                if job.done and job.ended is not None and job.ended < cutoff
            ]
            for slug in stale:
                del self.running[slug]
        return len(stale)
