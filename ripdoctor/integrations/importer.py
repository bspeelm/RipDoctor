"""Two ways to get a cut record into a library, behind one interface.

The base one writes tags with metaflac and moves the files where the library
wants them. It exists because the archive gate is unsatisfiable without it: with
no way to finish a record, raw sides accumulate with nowhere to go, and an
install that dead-ends one step from the finish is a bad first impression.

`ripdoctor[beets]` is the other. What beets uniquely adds is fingerprint
matching, its own path formatting, album-mode ReplayGain - which matters on
vinyl, where one twelve-second fade scored +20.1 dB against +6.8 for the album -
duplicate resolution and a real library database. Those are reasons to want it.
None of them are reasons to require it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ripdoctor.audio.runner import Runner, ToolMissing
from ripdoctor.core.plan import Plan
from ripdoctor.integrations import tagger as T

FLAC = ".flac"


class ImportFailed(Exception):
    """The record did not get into the library, and here is why."""


@dataclass(frozen=True, slots=True)
class Outcome:
    tracks: int
    where: Path | None
    output: str = ""
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "tracks": self.tracks,
            "library": "" if self.where is None else str(self.where),
            "output": self.output,
            "notes": list(self.notes),
        }


class Importer(Protocol):
    @property
    def name(self) -> str:
        """What to call this in a report."""

    def available(self, runner: Runner) -> bool: ...

    def preview(
        self, runner: Runner, plan: Plan, review: str, library: str, *, mbid: str = ""
    ) -> str: ...

    def apply(
        self,
        runner: Runner,
        plan: Plan,
        review: str,
        library: str,
        *,
        mbid: str = "",
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome: ...

    def locate(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> tuple[Path | None, int]: ...


@dataclass(frozen=True, slots=True)
class Tagger:
    """metaflac and a directory. What the base install uses."""

    name: str = "tagger"

    def available(self, runner: Runner) -> bool:
        return runner.which("metaflac") is not None

    def preview(
        self, runner: Runner, plan: Plan, review: str, library: str, *, mbid: str = ""
    ) -> str:
        return "\n".join(
            f"  {p.source.name}  ->  {p.dest}"
            for p in T.placements(plan, review, library)
        )

    def apply(
        self,
        runner: Runner,
        plan: Plan,
        review: str,
        library: str,
        *,
        mbid: str = "",
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome:
        moved = T.apply(
            runner, plan, review, library, file_mode=file_mode, dir_mode=dir_mode
        )
        where = moved[0].dest.parent if moved else None
        return Outcome(tracks=len(moved), where=where)

    def locate(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> tuple[Path | None, int]:
        return T.locate(library, artist, album)


# beets colourises its output, and the escape sequences are litter in a browser.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# An ASCII unit separator, built here rather than written into the format
# string: beets passes "\t" through as a literal backslash-t, every line then
# fails to split, and the album reads as having no tracks at all.
_SEP = "\x1f"

# What is typed at beets' matcher to accept the candidate it printed.
_ACCEPT = b"A\n"

# Layered on top of whatever beets is configured with. A single `-c` adds to the
# user's own configuration rather than replacing it - two do not layer, the last
# simply wins - so this changes as little as it can. Two settings: the prompt,
# without which a preview has already moved the files, and the move, without
# which every track is written twice and a finished import cannot be told from
# a refused one. ADR-042.
OVERRIDE = "import:\n  quiet: no\n  timid: no\n  move: yes\n"
OVERRIDE_FILE = "beets-override.yaml"


@dataclass(frozen=True, slots=True)
class Beets:
    """Runs the beets already installed, with the user's own configuration.

    Where that configuration lives is beets' business, not this project's:
    `BEETSDIR` and the default path are beets' own, and a server that sets one
    passes it down like any other environment. What is added here is a single
    override, and only because a preview is otherwise impossible.
    """

    name: str = "beets"
    config: str = ""

    @classmethod
    def with_override(cls, state_dir: str | Path) -> Beets:
        """Write the override beside this project's other state, and use it."""
        where = Path(state_dir) / OVERRIDE_FILE
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(OVERRIDE)
        return cls(config=str(where))

    def available(self, runner: Runner) -> bool:
        return runner.which("beet") is not None

    def plugins(self, runner: Runner) -> tuple[str, ...]:
        """Which plugins beets loaded - not which it was told to. A plugin
        whose own dependency is missing is configured and absent."""
        result = runner.run(self._argv(["version"]), timeout=60)
        for line in (result.text + result.err).splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() == "plugins":
                return tuple(sorted(p.strip() for p in value.split(",") if p.strip()))
        return ()

    def files_into(self, runner: Runner) -> str:
        """Where beets says it will put a record, defaults included.

        Asking is the only way to know, and the two can disagree without either
        being wrong in itself. ADR-042.
        """
        result = runner.run(self._argv(["config", "-d"]), timeout=60)
        for line in (result.text + result.err).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "directory":
                return os.path.expanduser(value.strip())
        return ""

    def _argv(self, args: list[str]) -> list[str]:
        head = ["beet"]
        if self.config:
            head += ["--config", self.config]
        return head + args

    def preview(
        self, runner: Runner, plan: Plan, review: str, library: str, *, mbid: str = ""
    ) -> str:
        """The closest thing to a dry run that exists.

        beets has none. Run with stdin closed it prints the whole candidate,
        asks for an answer, and dies without touching a file; apply runs the
        identical command with the answer piped in.
        """
        result = runner.run(
            self._argv(self._import(review, mbid)), stdin=b"", timeout=1800
        )
        return _plain(result.text + result.err)

    def _import(self, review: str, mbid: str) -> list[str]:
        # The release is pinned to the one already chosen rather than guessed
        # from untagged audio: freshly cut files carry no tags at all, so even
        # the right release scores about half and a quiet import skips it
        # without a word.
        pin = ["--search-id", mbid] if mbid else ["-A"]
        return ["import", *pin, review]

    def apply(
        self,
        runner: Runner,
        plan: Plan,
        review: str,
        library: str,
        *,
        mbid: str = "",
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome:
        result = runner.run(
            self._argv(self._import(review, mbid)), stdin=_ACCEPT, timeout=3600
        )
        output = _plain(result.text + result.err)

        left = (
            [p for p in Path(review).glob(f"*{FLAC}")] if Path(review).is_dir() else []
        )
        if left:
            # beets leaves the files where they were when it declines, so what
            # is still in review is the honest measure of what did not import.
            raise ImportFailed(
                f"{len(left)} of the tracks are still in review - beets did not "
                f"take them.\n\n{output[-2000:]}"
            )

        where, count = self.locate(runner, library, plan.artist, plan.album, mbid=mbid)
        notes = []
        # beets rewrites every file as it writes tags, and the rewrite lands at
        # 0600 rather than inheriting the directory. Invisible while the player
        # runs as the owning user, and broken for anything else.
        fixed = _normalise(where, file_mode, dir_mode) if where else 0
        if fixed:
            notes.append(f"set {fixed} files' modes")
        return Outcome(count, where, output=output[-8000:], notes=tuple(notes))

    def locate(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> tuple[Path | None, int]:
        """Ask beets where it put things, rather than guessing a path.

        beets owns the naming. A gate that checked the path this project would
        have chosen would be checking the wrong directory on every install that
        configured beets differently, which is all of them.
        """
        # An empty name is not a query, it is every record in the library:
        # `beet ls album:` matches them all, and the gate then reports a record
        # that has nothing to do with the one being imported.
        if not mbid and not album:
            return None, 0
        query = [f"mb_albumid:{mbid}"] if mbid else [f"album:{album}"]
        try:
            result = runner.run(
                self._argv(["ls", "-f", _SEP.join(("$album", "$path")), *query]),
                timeout=300,
            )
        except ToolMissing:
            return None, 0
        paths = [
            Path(line.split(_SEP)[-1])
            for line in _plain(result.text).splitlines()
            if _SEP in line
        ]
        if not paths:
            return None, 0
        return paths[0].parent, len(paths)


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _normalise(where: Path, file_mode: int, dir_mode: int) -> int:
    if not where.is_dir():
        return 0
    where.chmod(dir_mode)
    changed = 0
    for p in where.iterdir():
        if p.is_file():
            p.chmod(file_mode)
            changed += 1
    return changed


def choose(runner: Runner, name: str, state_dir: str | Path = "") -> Importer:
    """The importer to use, falling back rather than refusing.

    A machine configured for beets that no longer has it should still be able
    to finish a record; being told to install something at the last step of a
    twenty-minute job is not a useful answer.
    """
    if name == "beets":
        beets = Beets.with_override(state_dir) if state_dir else Beets()
        if beets.available(runner):
            return beets
    return Tagger()
