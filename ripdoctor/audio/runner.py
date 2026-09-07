"""The one place external programs are run. ADR-026.

Everything above this layer receives a Runner and never reaches for a process
itself, so the whole application can be exercised with no ffmpeg installed. The
architecture test holds that boundary.
"""

from __future__ import annotations

import shutil
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


class Runner(Protocol):
    """Runs one external program and returns what it said."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = ...,
        timeout: float | None = ...,
    ) -> Result: ...

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

    def which(self, tool: str) -> str | None:
        return shutil.which(tool)


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
    installed: set[str] = field(default_factory=set)

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
        if self.installed and args[0] not in self.installed:
            raise ToolMissing(args[0])
        for match, reply in self.replies:
            if match(args):
                return Result(args, reply.returncode, reply.stdout, reply.stderr)
        return Result(args, 0, b"", b"")

    def which(self, tool: str) -> str | None:
        if not self.installed:
            return f"/usr/bin/{tool}"
        return f"/usr/bin/{tool}" if tool in self.installed else None

    def argv_for(self, needle: str) -> tuple[str, ...]:
        """The one recorded call containing `needle`. Raises if not exactly one."""
        hits = [c for c in self.calls if any(needle in a for a in c)]
        if len(hits) != 1:
            raise AssertionError(f"{len(hits)} calls matched {needle!r}, wanted 1")
        return hits[0]
