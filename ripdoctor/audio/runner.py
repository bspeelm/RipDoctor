"""The one place external programs are run. ADR-026.

Everything above this layer receives a Runner and never reaches for a process
itself, so the whole application can be exercised with no ffmpeg installed. The
architecture test holds that boundary.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol


class ToolMissing(Exception):
    """A program this needs is not installed."""

    def __init__(self, tool: str, purpose: str = "") -> None:
        self.tool = tool
        detail = f" - needed to {purpose}" if purpose else ""
        super().__init__(f"{tool} is not installed{detail}")


class ToolFailed(Exception):
    """A program ran and reported failure."""

    def __init__(self, argv: Sequence[str], code: int, stderr: str) -> None:
        self.argv = list(argv)
        self.code = code
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-1:] or [""]
        super().__init__(f"{argv[0]} exited {code}: {tail[0]}")


@dataclass(frozen=True, slots=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def require(self) -> Result:
        if not self.ok:
            raise ToolFailed(self.argv, self.returncode, self.err)
        return self


class Process(Protocol):
    """A program still running. A capture outlives a single call."""

    def poll(self) -> int | None: ...

    def interrupt(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = ...) -> int: ...


class Runner(Protocol):
    """Runs one external program and returns what it said."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = ...,
        timeout: float | None = ...,
    ) -> Result: ...

    def start(
        self, argv: Sequence[str], *, stderr_path: str | None = ...
    ) -> Process: ...

    def which(self, tool: str) -> str | None: ...


class RealRunner:
    """Runs programs. Never through a shell, and always from an argv list.

    An argv list is the whole defence against a record whose title contains a
    quote: there is no string for it to break out of.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> Result:
        args = [str(a) for a in argv]
        if not args:
            raise ValueError("empty argv")
        if self.which(args[0]) is None:
            raise ToolMissing(args[0])
        try:
            p = subprocess.run(  # noqa: S603 - argv list, never shell=True
                args,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:  # lost between which() and here
            raise ToolMissing(args[0]) from e
        return Result(tuple(args), p.returncode, p.stdout or b"", p.stderr or b"")

    def start(self, argv: Sequence[str], *, stderr_path: str | None = None) -> Process:
        """Begin a program and return while it runs.

        Used only for capture, which lasts a side. Everything else finishes
        inside one call and goes through run(). Errors go to a file rather than
        a pipe, which over twenty minutes would fill and block the writer.
        ADR-032.
        """
        args = [str(a) for a in argv]
        if not args:
            raise ValueError("empty argv")
        if self.which(args[0]) is None:
            raise ToolMissing(args[0])
        errors = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL  # noqa: SIM115
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list, never shell=True
                args, stdout=subprocess.DEVNULL, stderr=errors
            )
        except FileNotFoundError as e:
            raise ToolMissing(args[0]) from e
        finally:
            if errors is not subprocess.DEVNULL:
                errors.close()  # type: ignore[union-attr]
        return Started(proc)

    def which(self, tool: str) -> str | None:
        return shutil.which(tool)


@dataclass
class Started:
    """A running program, and the one signal a capture needs.

    arecord backfills the WAV header - which is where the length lives - when
    it is interrupted, and does not when it is killed. A killed capture leaves
    a file reporting no duration, which every tool that reads it then has to
    work around. So a capture is asked to stop, and only killed if it will not.
    """

    proc: subprocess.Popen[bytes]

    def poll(self) -> int | None:
        return self.proc.poll()

    def interrupt(self) -> None:
        self.proc.send_signal(signal.SIGINT)

    def kill(self) -> None:
        self.proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout)


Match = Callable[[Sequence[str]], bool]


@dataclass
class FakeRunner:
    """A Runner that answers from a script instead of running anything.

    Replies are matched in order, so a specific case can be registered before a
    general one. Every call is recorded, which is what makes it possible to
    assert the exact argv a layer constructs rather than only its return value -
    for cutting, the argv *is* the behaviour.
    """

    replies: list[tuple[Match, Result]] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    # None means every tool exists. An empty set means none do - which is a
    # thing a test needs to say, and cannot if the two are the same value.
    installed: set[str] | None = None
    # How many poll() calls a started process runs for. None means it keeps
    # going until something stops it, which is what a capture does.
    exit_after: int | None = None
    started: list[FakeProcess] = field(default_factory=list)

    def expect(
        self,
        match: Match | str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> FakeRunner:
        """Register a reply. A string matches any argv containing it."""
        match_fn: Match
        if isinstance(match, str):
            needle = match
            match_fn = lambda argv: any(needle in a for a in argv)  # noqa: E731
        else:
            match_fn = match
        self.replies.append((match_fn, Result((), returncode, stdout, stderr)))
        return self

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> Result:
        args = tuple(str(a) for a in argv)
        if not args:
            raise ValueError("empty argv")
        self.calls.append(args)
        if self.installed is not None and args[0] not in self.installed:
            raise ToolMissing(args[0])
        for match, reply in self.replies:
            if match(args):
                return Result(args, reply.returncode, reply.stdout, reply.stderr)
        return Result(args, 0, b"", b"")

    def start(self, argv: Sequence[str], *, stderr_path: str | None = None) -> Process:
        args = tuple(str(a) for a in argv)
        if not args:
            raise ValueError("empty argv")
        self.calls.append(args)
        if self.installed is not None and args[0] not in self.installed:
            raise ToolMissing(args[0])
        proc = FakeProcess(self.exit_after)
        self.started.append(proc)
        return proc

    def which(self, tool: str) -> str | None:
        if self.installed is None or tool in self.installed:
            return f"/usr/bin/{tool}"
        return None

    def argv_for(self, needle: str) -> tuple[str, ...]:
        """The one recorded call containing `needle`. Raises if not exactly one."""
        hits = [c for c in self.calls if any(needle in a for a in c)]
        if len(hits) != 1:
            raise AssertionError(f"{len(hits)} calls matched {needle!r}, wanted 1")
        return hits[0]


@dataclass
class FakeProcess:
    """A started program that never existed."""

    exit_after: int | None = None
    polls: int = 0
    returncode: int | None = None
    interrupted: bool = False
    killed: bool = False

    def poll(self) -> int | None:
        self.polls += 1
        if (
            self.returncode is None
            and self.exit_after is not None
            and self.polls >= self.exit_after
        ):
            self.returncode = 0
        return self.returncode

    def interrupt(self) -> None:
        self.interrupted = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode
