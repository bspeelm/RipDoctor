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
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ripdoctor.audio.runner import Runner, ToolMissing
from ripdoctor.core.filing import (
    ADDING,
    BESIDE,
    JOIN,
    NEW_ALBUM,
    Row,
    Run,
    filed_copy,
    joining,
    replace_refusal,
    row_length,
    term,
)
from ripdoctor.core.plan import Plan
from ripdoctor.integrations import aside as A
from ripdoctor.integrations import tagger as T
from ripdoctor.store.safety import contains

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


@dataclass(frozen=True, slots=True)
class Stale:
    """Rows a library database holds for files that are no longer there."""

    tracks: int
    where: Path
    query: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"tracks": self.tracks, "where": str(self.where), "query": self.query}

    def note(self) -> str:
        s = "" if self.tracks == 1 else "s"
        return (
            f"beets still has {self.tracks} row{s} registered at {self.where}, but "
            "none of those files exist any more - they were deleted outside beets, "
            "or the library is off its mount. It will treat this import as a duplicate "
            "and stop part-way. Clear the stale rows first; that removes database rows "
            "only, never files."
        )


@dataclass(frozen=True, slots=True)
class Filed:
    """What the library holds of one record, which is what replacing it would remove,
    and why it would refuse. `where` is the first of `folders` folders."""

    tracks: int
    where: Path | None
    folders: int = 0
    refused: str = ""
    stale: Stale | None = None


@dataclass(frozen=True, slots=True)
class TapeRun:
    """What one album run of a tape's songs said. Never proof of anything: only
    the library's rows and the files left behind say what happened."""

    returncode: int
    output: str
    root: str = ""

    def why(self) -> str:
        """The one thing to tell a person, in place of a traceback in a log."""
        lines = [line.strip() for line in self.output.splitlines() if line.strip()]
        said = lines[-1] if lines else f"beets exited {self.returncode}, saying nothing"
        missing = _MISSING.search(self.output)
        if missing and _relative_row(self.root, missing[2]):
            return (
                "beets crashed with FileNotFoundError, which merging into an album "
                "does when a library row holds a relative path. Nothing was lost. "
                "Back up the library database, rewrite those rows' paths as absolute "
                f"ones, then file again. beets said: {said}"
            )
        if missing:
            return (
                f"beets crashed looking for {missing[2]}, which is not there - put "
                "it back, or clear the row that names it with beet remove, which "
                "leaves files alone, then file again"
            )
        no_release = _NO_RELEASE.search(self.output)
        if no_release:
            return f"{no_release[0]} - check the release chosen, then file again"
        if _STARVED in self.output:
            return f"beets asked a question this did not answer: {said}"
        return said


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
        duplicates: str = "replace",
        replacing: bool = False,
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome: ...

    def locate(
        self,
        runner: Runner,
        library: str,
        artist: str,
        album: str,
        *,
        mbid: str = "",
        unreleased: bool = False,
    ) -> tuple[Path | None, int]: ...

    def filed(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Filed: ...

    def stale(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Stale | None: ...

    def clear_stale(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Stale: ...


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
        duplicates: str = "replace",
        replacing: bool = False,
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome:
        # It writes into a directory rather than a database, so a same-named
        # track is overwritten and there is no second question to answer.
        moved = T.apply(
            runner, plan, review, library, file_mode=file_mode, dir_mode=dir_mode
        )
        where = moved[0].dest.parent if moved else None
        return Outcome(tracks=len(moved), where=where)

    def locate(
        self,
        runner: Runner,
        library: str,
        artist: str,
        album: str,
        *,
        mbid: str = "",
        unreleased: bool = False,
    ) -> tuple[Path | None, int]:
        return T.locate(library, artist, album)

    def filed(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Filed:
        where, count = T.locate(library, artist, album)
        return Filed(count, where, 1 if where else 0)

    def stale(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Stale | None:
        return None  # it keeps no database, so nothing can outlive the files

    def clear_stale(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Stale:
        raise ImportFailed("the tagger keeps no database - there is nothing to clear")


# beets colourises its output, and the escape sequences are litter in a browser.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# An ASCII unit separator, built here rather than written into the format
# string: beets passes "\t" through as a literal backslash-t, every line then
# fails to split, and the album reads as having no tracks at all.
_SEP = "\x1f"
_PATH_FIELD = re.compile(r"\$\{?(\w+)")
_ROW = _SEP.join(
    ("$title", "$artist", "$album", "$length", "$path", "$mb_trackid", "$track")
)
_HELD = _SEP.join((_ROW, "$mb_albumid"))
_COPY = _SEP.join((_HELD, "$albumartist", "$id", "$album_id"))

# What is typed at beets' matcher to accept the candidate it printed, and then
# at the question it asks when the album is already in the library. One answer
# leaves that second question at end-of-input, which beets reports as a stdin
# error three screens below the thing that actually went wrong. ADR-054.
_APPLY = "A"
#
# Replace is answered with keep, because beets' own `R` deletes the audio of every album
# of that artist and name, another edition's included, measured on 2.1.0. What a replace
# removes is chosen and removed here first, so what beets still asks about is not it.
DUPLICATES = {"replace": "K", "keep": "K", "skip": "S", "merge": "M"}

# beets says this when it asked and nobody answered. Worth recognising: the
# failure it causes looks like "the tracks are still in review", which is true
# and is not the cause.
_STARVED = "stdin stream ended"
_MISSING = re.compile(r"FileNotFoundError:[^\n]*?: b?(['\"])([^\n]+)\1\s*$", re.M)
_NO_RELEASE = re.compile(r"^No matching release found.*$", re.M)

# What a tape's runs answer. Replace is not among these and never may be: it deletes
# the filed audio. ADR-060.
#
# With no release there is no candidate to accept, so the first question is the
# duplicate one, and merging would rematch what is already filed. Keeping both is what
# the song's outcome says happened.
FROM_LIST_ANSWERS = b"K\n"
# Apply the release; merge; apply again, because merging matches afresh. A duplicate
# question nobody expected meets end of input, which stops the run safely. Merging into
# another edition rewrites every owned track, so that goes beside it. ADR-063.
PINNED_ANSWERS = {
    NEW_ALBUM: b"A\n",
    JOIN: b"A\nM\nA\n",
    BESIDE: b"A\nK\n",
    ADDING: b"A\nK\n",
}


# With lookup off beets has no step that acts on a merge, and goes on to keep both.
NO_RELEASE_TO_MERGE = (
    "merging needs a release: with none, beets has nothing to match the album against "
    "and files another album entry. Choose Keep both for that, or pin a release"
)
SKIP_ANY_OTHER = b"A\nS\n"
REMATCHED = (
    "merged into the album already filed: beets matched its tracks to this release "
    "again, which rewrites their tags and album gain"
)


def answers(duplicates: str, *, matching: bool) -> bytes:
    """Every answer beets may ask for, in the order it asks.

    Only a run that consults the catalogue is offered a candidate to accept.
    With lookup off there is no such question, and sending one anyway puts an
    answer the duplicate prompt does not recognise - which survives only
    because beets asks again. ADR-054.
    """
    said = [_APPLY] if matching else []
    said.append(DUPLICATES.get(duplicates, "K"))
    return ("\n".join(said) + "\n").encode()


# Layered on top of whatever beets is configured with. A single `-c` adds to the
# user's own configuration rather than replacing it - two do not layer, the last
# simply wins - so this changes as little as it can. Three settings: the prompt,
# without which a preview has already moved the files; the move, without which
# every track is written twice and a finished import cannot be told from a refused
# one; and the duplicate question, which a configured `remove` answers by deleting
# the filed audio, before any answer sent here is read. ADR-042, ADR-063.
#
# `incremental` and `resume` because a retry must not be skipped as already seen, or
# asked whether to resume a run a kill interrupted.
OVERRIDE = (
    "import:\n  quiet: no\n  timid: no\n  move: yes\n  duplicate_action: ask\n"
    "  incremental: no\n  resume: no\n"
)

# For a song going into a row that has its art: the row it arrives on is about to go,
# and a cover fetched for that would stay behind as a file nothing names.
NO_ART = OVERRIDE + "fetchart:\n  auto: no\n"
NO_ART_FILE = "beets-override-no-art.yaml"

# Only for the weak matches beets does ask about: it lists the candidates and
# this declines them, which is the right outcome, because a look that had to
# choose between several would be choosing. A strong match is never asked
# about at all - see PREVIEW. ADR-062.
LOOK_ONLY = b"S\n"

# A match beets rates strongly is applied without prompting, so no answer on
# stdin makes `beet import` non-committal - a look that only closed or fed the
# input filed the record. A look is therefore an import into a library that is
# thrown away, with nothing moved, copied or written. ADR-062.
PREVIEW = (
    "library: {db}\n"
    "directory: {files}\n"
    "import:\n"
    "  quiet: no\n"
    "  timid: no\n"
    "  move: no\n"
    "  copy: no\n"
    "  write: no\n"
    "  resume: no\n"
    "  incremental: no\n"
)
PREVIEW_FILE = "beets-preview.yaml"
PREVIEW_DB = "beets-preview.db"
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
    preview_config: str = ""
    preview_db: str = ""
    no_art: str = ""

    @classmethod
    def with_override(cls, state_dir: str | Path) -> Beets:
        """Write the override beside this project's other state, and use it."""
        where = Path(state_dir) / OVERRIDE_FILE
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(OVERRIDE)
        db = where.parent / PREVIEW_DB
        look = where.parent / PREVIEW_FILE
        look.write_text(
            PREVIEW.format(db=db, files=where.parent / "beets-preview-files")
        )
        no_art = where.parent / NO_ART_FILE
        no_art.write_text(NO_ART)
        return cls(
            config=str(where),
            preview_config=str(look),
            preview_db=str(db),
            no_art=str(no_art),
        )

    def available(self, runner: Runner) -> bool:
        return runner.which("beet") is not None

    def config_file(self, runner: Runner) -> str:
        """The file beets reads, whether or not anything has written one."""
        result = runner.run(self._argv(["config", "-p"]), timeout=60)
        lines = (result.text + result.err).strip().splitlines()
        return lines[-1].strip() if lines else ""

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

    def folders_apart(self, runner: Runner) -> bool | None:
        """Whether beets' album path gives each album row its own folder, which is what
        `%aunique` does, with the defaults counted. None if it would not say, or if what
        it says cannot be foretold from an album's names. `comp` is only for a release
        by Various Artists, which the note's wording allows for, and `singleton` is for
        no album; any other path is chosen by a query this does not run."""
        result = runner.run(self._argv(["config", "-d"]), timeout=60)
        if not result.ok:
            return None
        inside, found = False, ""
        for line in _plain(result.text).splitlines():
            if line and not line[0].isspace():
                inside = line.startswith("paths:")
            elif inside and line.strip():
                key, _, value = line.strip().partition(":")
                if key == "default":
                    found = value
                elif key not in ("comp", "singleton"):
                    return None
        return _apart_by(found)

    def _argv(self, args: list[str], config: str = "") -> list[str]:
        head = ["beet"]
        chosen = config or self.config
        if chosen:
            head += ["--config", chosen]
        return head + args

    def _looking(self, args: list[str]) -> list[str]:
        """The same command against a library that is about to be deleted.

        Fresh every time: rows left by the last look would make the next one
        report the record as a duplicate of itself.
        """
        if not self.preview_config:
            # Falling back on the real configuration here would file the record
            # instead of showing it, which is the one thing a look must not do.
            raise ImportFailed(
                "a look needs a library to throw away and none is configured"
            )
        Path(self.preview_db).unlink(missing_ok=True)
        return self._argv(args, self.preview_config)

    def preview(
        self, runner: Runner, plan: Plan, review: str, library: str, *, mbid: str = ""
    ) -> str:
        """The closest thing to a dry run that exists.

        beets has none, and a strong match is taken without asking, so this
        runs against a throwaway library and moves, copies and writes nothing.
        """
        result = runner.run(
            self._looking(self._import(review, mbid)), stdin=LOOK_ONLY, timeout=1800
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
        duplicates: str = "replace",
        replacing: bool = False,
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome:
        self._refuse_off_its_mount(runner, library)
        self._refuse_held(runner, library)
        if duplicates == "merge" and not mbid:
            raise ImportFailed(NO_RELEASE_TO_MERGE)
        how = self._merging(runner, library, mbid, plan.album, duplicates)
        expected = sum(len(s.tracks) for s in plan.sides)
        replacing = replacing and duplicates == "replace"
        cut = len(list(Path(review).glob(f"*{FLAC}")))
        if replacing and (short := partial_cut(cut, expected)):
            raise ImportFailed(short)
        # Only with the acknowledgement: "replace" alone is a default, not a
        # licence to delete. ADR-060.
        held = (
            self._hold_filed(runner, library, plan.artist, plan.album, mbid, review)
            if replacing
            else None
        )
        try:
            if not mbid:
                # Nothing to match against, and a cut file carries no tags at
                # all. The plan is what is known, so it is written first. ADR-049.
                T.write_plan_tags(runner, plan, review)
            result = runner.run(
                self._argv(self._import(review, mbid), self._config_for(how)),
                stdin=_merge_answers(how) or answers(duplicates, matching=bool(mbid)),
                timeout=3600,
            )
            output = _plain(result.text + result.err)
            left = (
                [p for p in Path(review).glob(f"*{FLAC}")]
                if Path(review).is_dir()
                else []
            )
            if left:
                # beets leaves the files where they were when it declines, so what
                # is still in review is the honest measure of what did not import.
                # A held copy's rows are not stale: its files are about to return.
                why = (
                    None
                    if held
                    else self.stale(runner, library, plan.artist, plan.album, mbid=mbid)
                )
                said = f"\n\n{why.note()}" if why else ""
                if _STARVED in output:
                    said = (
                        "\n\nbeets asked a question this did not answer, and gave up "
                        "waiting. That is the cause; what is left in review is the "
                        "symptom."
                    ) + said
                raise ImportFailed(
                    f"{len(left)} of the tracks are still in review - beets did not "
                    f"take them.{said}\n\n{output[-2000:]}"
                )
            new = self._new_copy(runner, library, held, expected) if held else ()
        except Exception as e:
            if held:
                self._undo_replace(runner, library, held, e)
            raise
        removed, covers, folders = (
            self._finish_replace(runner, library, held, new) if held else ([], 0, 0)
        )

        where, count = self.locate(runner, library, plan.artist, plan.album, mbid=mbid)
        if count < expected:
            raise ImportFailed(fewer_than_cut(count, expected))
        notes = []
        # beets rewrites every file as it writes tags, and the rewrite lands at
        # 0600 rather than inheriting the directory. Invisible while the player
        # runs as the owning user, and broken for anything else.
        fixed = T.set_modes(where, file_mode, dir_mode) if where else 0
        if fixed:
            notes.append(f"set {fixed} files' modes")
        if removed:
            notes.append(f"removed {len(removed)} files of the copy already filed")
        if covers:
            notes.append(f"removed {covers} covers of the copy already filed")
        if folders:
            notes.append(f"removed {folders} empty folders it left")
        notes.extend(self._merged(runner, library, mbid, how))
        if duplicates != "merge":
            notes.extend(too_many(count, sum(len(s.tracks) for s in plan.sides)))
        spare = self.unregistered(runner, library, plan.artist, plan.album, mbid=mbid)
        if spare:
            notes.append(
                f"{len(spare)} files in that directory are not in the library - "
                f"starting {spare[0].name}. Whatever serves it will show them."
            )
        return Outcome(count, where, output=output[-8000:], notes=tuple(notes))

    def _merging(
        self, runner: Runner, root: str, release: str, album: str, duplicates: str
    ) -> str:
        """How Merge can honestly be done, or nothing when it was not asked for. Beets
        merges every row of an album's name onto the release, so that happens only when
        all of them are it. Beside another edition this joins its row by hand. Nothing
        held is nothing to add to, and another edition held is refused, since merging
        renames, renumbers and retags it. ADR-054."""
        if duplicates != "merge":
            return ""
        own = filed_copy(self._copies(runner, root, []), "", "", release)
        if own:
            title = own[0].album
            run = Run(release, "", title, ())
            return joining(run, self.album_rows(runner, root, title))
        if album and self.album_rows(runner, root, album):
            raise ImportFailed(
                f"the library holds another edition of {album}, and merging would "
                "rename, renumber and retag it. Choose Keep both, which leaves it "
                "as it is"
            )
        return NEW_ALBUM

    def _config_for(self, how: str) -> str:
        return self.no_art if how == ADDING else ""

    def _merged(self, runner: Runner, root: str, release: str, how: str) -> list[str]:
        """What a merge did, said. One beets did itself has rewritten its album's files
        and worked out its gain; one by hand still has to."""
        if how == JOIN:
            return [REMATCHED]
        if how != ADDING:
            return []
        try:
            album, _, apart = self.join_release(runner, root, release)
        except (ImportFailed, ToolMissing) as e:
            return [f"these stayed a separate album entry beside that row: {e}"]
        if apart:
            return ["these stayed a separate album entry, in a folder of their own"]
        why = (
            self.album_gain(runner, album)
            if "replaygain" in self.plugins(runner)
            else ""
        )
        said = "joined the row already there, and no other edition's tracks changed"
        return [said, *([f"its album gain was not refreshed: {why}"] if why else [])]

    def propose(self, runner: Runner, directory: str) -> tuple[str, str]:
        """What beets would make of an album that arrived already tagged.

        The same mechanism as `preview`: a throwaway library, and nothing
        moved, copied or written. ADR-062.
        """
        result = runner.run(
            self._looking(["import", directory]), stdin=LOOK_ONLY, timeout=1800
        )
        output = _plain(result.text + result.err)
        return proposed(output), output

    def take(
        self,
        runner: Runner,
        directory: str,
        library: str,
        *,
        mbid: str,
        tracks: tuple[str, ...],
        duplicates: str = "keep",
        replacing: bool = False,
        file_mode: int = 0o664,
        dir_mode: int = 0o775,
    ) -> Outcome:
        """Import an album that arrived already cut, pinned to the agreed
        release so the run that commits cannot choose a different one."""
        self._refuse_off_its_mount(runner, library)
        self._refuse_held(runner, library)
        how = self._merging(runner, library, mbid, "", duplicates)
        # Nothing asked first here, so finding none of this release is the usual case.
        held = (
            self._hold_filed(
                runner,
                library,
                "",
                "",
                mbid,
                directory,
                none_is_fine=True,
                at_least=len(tracks),
            )
            if replacing and duplicates == "replace"
            else None
        )
        try:
            result = runner.run(
                self._argv(
                    ["import", "--search-id", mbid, directory], self._config_for(how)
                ),
                stdin=_merge_answers(how) or answers(duplicates, matching=True),
                timeout=3600,
            )
            output = _plain(result.text + result.err)
            left = [n for n in tracks if (Path(directory) / n).exists()]
            if left:
                said = (
                    "\n\nbeets asked a question this did not answer, and gave up "
                    "waiting."
                    if _STARVED in output
                    else ""
                )
                raise ImportFailed(
                    f"{len(left)} of the tracks are still waiting - beets did not "
                    f"take them.{said}\n\n{output[-2000:]}"
                )
            new = self._new_copy(runner, library, held, len(tracks)) if held else ()
        except Exception as e:
            if held:
                self._undo_replace(runner, library, held, e)
            raise
        removed, covers, folders = (
            self._finish_replace(runner, library, held, new) if held else ([], 0, 0)
        )

        where, count = self.locate(runner, library, "", "", mbid=mbid)
        if count < len(tracks):
            raise ImportFailed(fewer_than_cut(count, len(tracks)))
        notes = []
        fixed = T.set_modes(where, file_mode, dir_mode) if where else 0
        if fixed:
            notes.append(f"set {fixed} files' modes")
        if removed:
            notes.append(f"removed {len(removed)} files of this release already filed")
        if covers:
            notes.append(f"removed {covers} covers of this release already filed")
        if folders:
            notes.append(f"removed {folders} empty folders it left")
        notes.extend(self._merged(runner, library, mbid, how))
        if duplicates != "merge":
            notes.extend(too_many(count, len(tracks)))
        return Outcome(count, where, output=output[-8000:], notes=tuple(notes))

    def file_tape_run(
        self,
        runner: Runner,
        directory: str,
        release: str = "",
        root: str = "",
        *,
        how: str = BESIDE,
    ) -> TapeRun:
        """One directory, one album of a tape: pinned, or with no release its tags
        are taken as they stand. Reports and judges nothing, because a tape's album
        is usually partial and every count `apply` trusts would misread it. A pinned
        run is told `how` it meets the library, and left untold it merges nothing."""
        config = self.no_art if release and how == ADDING else ""
        result = runner.run(
            self._argv(self._import(directory, release), config),
            stdin=PINNED_ANSWERS[how] if release else FROM_LIST_ANSWERS,
            timeout=3600,
        )
        return TapeRun(result.returncode, _plain(result.text + result.err), root)

    def library_rows(
        self, runner: Runner, root: str, query: list[str]
    ) -> tuple[Row, ...]:
        """The rows a query finds. A question beets could not answer is raised, not
        read as a miss, or a song it cannot see is filed a second time."""
        result = runner.run(self._argv(["ls", "-f", _ROW, *query]), timeout=300)
        if not result.ok:
            raise ImportFailed(
                f"beets could not look in the library: {TapeRun(1, result.err).why()}"
            )
        return tuple(
            _row(root, *fields)
            for fields in _fields(result.text)
            if len(fields) == _ROW.count(_SEP) + 1
        )

    def owned(
        self, runner: Runner, root: str, recording: str, artist: str
    ) -> tuple[tuple[Row, ...], tuple[Row, ...]]:
        """The rows holding this recording, and everything by this artist: not every
        row has an id, and a title is compared once folded, which beets cannot do."""
        by_id = (
            self.library_rows(runner, root, [f"mb_trackid:{recording}"])
            if recording
            else ()
        )
        return by_id, self.library_rows(runner, root, [term("artist", artist)])

    def artist_copies(self, runner: Runner, root: str, artist: str) -> tuple[Row, ...]:
        """Everything by this artist, with the album it is on, for a lookup to judge."""
        return self._copies(runner, root, [term("artist", artist)])

    def recording_copies(
        self, runner: Runner, root: str, recording: str
    ) -> tuple[Row, ...]:
        return self._copies(runner, root, [f"mb_trackid:{recording}"])

    def _copies(self, runner: Runner, root: str, query: list[str]) -> tuple[Row, ...]:
        result = runner.run(self._argv(["ls", "-f", _COPY, *query]), timeout=300)
        if not result.ok:
            raise ImportFailed(TapeRun(result.returncode, result.err).why())
        return tuple(
            _row(root, *fields)
            for fields in _fields(result.text)
            if len(fields) == _COPY.count(_SEP) + 1
        )

    def holds_path(self, runner: Runner, root: str, path: str) -> bool:
        """Whether a row names this file. A question it could not answer is raised."""
        return bool(self.library_rows(runner, root, [_one_file(path)]))

    def forget_row(self, runner: Runner, root: str, path: str) -> None:
        """Drop one row by its exact path. No `-d`: the file stays. ADR-050."""
        query = [_one_file(path)]
        result = runner.run(self._argv(["remove", "-f", *query]), timeout=600)
        if not result.ok or self.library_rows(runner, root, query):
            why = TapeRun(result.returncode, result.err).why()
            raise ImportFailed(f"beets could not clear its row for {path}: {why}")

    def off_its_mount(self, runner: Runner, root: str) -> str:
        """Why filing now would land on a bare mount point, or nothing. A root with
        nothing in it is either new or gone, and only beets' rows can tell which, so
        only then is every row asked for. A library this cannot ask about raises. A
        Tagger install keeps no rows and so cannot be told this. ADR-063."""
        if not holds_nothing(root):
            return ""
        rows = [
            r for r in self._held(runner, root, [], 300.0) if contains(root, r.path)
        ]
        if not rows:
            return ""
        s = "" if len(rows) == 1 else "s"
        return (
            f"the library at {root} has nothing in it and beets holds {len(rows)} "
            f"row{s} there, so it may be off its mount. Nothing was filed - put that "
            "right and file again"
        )

    def _refuse_off_its_mount(self, runner: Runner, root: str) -> None:
        why = self.off_its_mount(runner, root)
        if why:
            raise ImportFailed(why)

    def _complaint(self, runner: Runner, args: list[str], timeout: float) -> str:
        """What beets said, when it did not exit clean."""
        result = runner.run(self._argv(args), timeout=timeout)
        return "" if result.ok else TapeRun(result.returncode, result.err).why()

    def art_of(self, runner: Runner, root: str, path: str) -> str:
        """The art beets holds for the album of this file, or nothing."""
        result = runner.run(
            self._argv(["ls", "-f", "$artpath", _one_file(path)]), timeout=300
        )
        if not result.ok:
            why = TapeRun(result.returncode, result.err).why()
            raise ImportFailed(f"beets could not say what art this has: {why}")
        said = _plain(result.text).strip()
        return "" if said in ("", "None") else str(_rooted(root, said))

    def album_files(
        self, runner: Runner, root: str, path: str
    ) -> tuple[str, tuple[str, ...]]:
        """The album row this file is on, and every file on it."""
        result = runner.run(
            self._argv(["ls", "-f", "$album_id", _one_file(path)]), timeout=300
        )
        album = _plain(result.text).strip()
        if not result.ok or not album.isdigit():
            raise ImportFailed(f"beets has no album for {path}")
        return album, self.files_on(runner, root, album)

    def files_on(self, runner: Runner, root: str, album: str) -> tuple[str, ...]:
        rows = runner.run(
            self._argv(["ls", "-f", "$path", f"album_id:{album}"]), timeout=300
        )
        if not rows.ok:
            why = TapeRun(rows.returncode, rows.err).why()
            raise ImportFailed(f"beets could not list that album: {why}")
        files = [line for line in _plain(rows.text).split("\n") if line.strip()]
        return tuple(str(_rooted(root, line)) for line in files)

    def join_release(
        self, runner: Runner, root: str, release: str
    ) -> tuple[str, int, int]:
        """This release's album rows into its oldest, so a later song of an edition held
        beside another is one album. Only rows move: other editions are never named, no
        file is touched, and a row is removed only with nothing left on it, since that
        forgets what is. Says the row kept, how many joined it and how many sit in
        another folder and stay. Running it again finishes what a kill interrupted."""
        result = runner.run(
            self._argv(["ls", "-a", "-f", "$id", f"mb_albumid:{release}"]), timeout=300
        )
        if not result.ok:
            raise ImportFailed(TapeRun(result.returncode, result.err).why())
        rows = _plain(result.text).split()
        files = {r: self.files_on(runner, root, r) for r in rows} if rows[1:] else {}
        full = [r for r in rows if files.get(r)]
        kept = full[0] if full else "".join(rows[:1])
        joined = apart = 0
        for row in rows:
            if row == kept:
                continue
            if files[row] and any(not _beside(files[kept], f) for f in files[row]):
                apart += 1
                continue
            if files[row]:
                self._moved_rows(runner, row, kept)
                if self.files_on(runner, root, row):
                    raise ImportFailed(f"album row {row} still holds files")
                joined += 1
            self._dropped_row(runner, row)
        return kept, joined, apart

    def _moved_rows(self, runner: Runner, row: str, kept: str) -> None:
        said = self._complaint(
            runner,
            ["modify", "-y", "-M", "-W", f"album_id:{row}", f"album_id={kept}"],
            120,
        )
        if said:
            raise ImportFailed(said)

    def _dropped_row(self, runner: Runner, row: str) -> None:
        said = self._complaint(runner, ["remove", "-a", "-f", f"id:{row}"], 120)
        if said:
            raise ImportFailed(said)

    def album_gain(self, runner: Runner, album: str) -> str:
        """Every file of this album row together, and no other row's. `-f` because beets
        otherwise sees a gain in the row, and a song arriving has changed what it is."""
        return self._complaint(runner, ["replaygain", "-a", "-f", f"id:{album}"], 3600)

    def set_art(self, runner: Runner, album: str, art: str) -> str:
        """The album row's own art, so a song joining it later is given the same.
        Nothing is moved or rewritten by this, which is what `-M -W` say. `modify` reads
        a value as a template, so what a path means as one is escaped."""
        value = art.replace("$", "$$").replace("%", "$%")
        return self._complaint(
            runner,
            ["modify", "-a", "-y", "-M", "-W", f"id:{album}", f"artpath={value}"],
            120,
        )

    def embed_art(self, runner: Runner, path: str, art: str) -> str:
        """Into this one file: `-f` takes items, where the form without it rewrites
        every track of a whole album."""
        return self._complaint(
            runner, ["embedart", "-y", "-f", art, _one_file(path)], 600
        )

    def replaygain(self, runner: Runner, path: str) -> str:
        """This one file's gain. `-f` because a lookup-off import leaves a gain in the
        row and not in the file, and without it beets sees that row and skips."""
        return self._complaint(runner, ["replaygain", "-f", _one_file(path)], 3600)

    def locate(
        self,
        runner: Runner,
        library: str,
        artist: str,
        album: str,
        *,
        mbid: str = "",
        unreleased: bool = False,
    ) -> tuple[Path | None, int]:
        """Ask beets where it put things, rather than guessing a path.

        beets owns the naming. A gate that checked the path this project would
        have chosen would be checking the wrong directory on every install that
        configured beets differently, which is all of them.
        """
        rows = self._quietly_filed(runner, library, artist, album, mbid)
        if unreleased:
            rows = tuple(r for r in rows if not r.release)
        return (Path(rows[0].path).parent, len(rows)) if rows else (None, 0)

    def filed(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Filed:
        """The one selection the dialog shows and a replace removes."""
        rows = self._quietly_filed(runner, library, artist, album, mbid)
        if not rows:
            return Filed(0, None)
        folders = {Path(r.path).parent for r in rows}
        refused = replace_refusal(rows, artist, album, mbid)
        where = Path(rows[0].path).parent
        stale = _stale_of(rows, artist, album)
        return Filed(len(rows), where, len(folders), refused, stale)

    def _filed_rows(
        self, runner: Runner, library: str, artist: str, album: str, mbid: str
    ) -> tuple[Row, ...]:
        """Every row, judged here. A beets query cannot do this: `album:` is a substring
        over every artist, and any id that is part of another finds that one too."""
        if not mbid and not album.strip():
            return ()
        return filed_copy(self._copies(runner, library, []), artist, album, mbid)

    def _quietly_filed(
        self, runner: Runner, library: str, artist: str, album: str, mbid: str
    ) -> tuple[Row, ...]:
        """For a question only reading, where no answer has always read as not there."""
        try:
            return self._filed_rows(runner, library, artist, album, mbid)
        except (ImportFailed, ToolMissing):
            return ()

    def _said_paths(self, runner: Runner, query: list[str]) -> list[str]:
        """Every file beets registered, as it says them, in its own order."""
        try:
            result = runner.run(
                self._argv(["ls", "-f", _SEP.join(("$album", "$path")), *query]),
                timeout=300,
            )
        except ToolMissing:
            return []
        return [
            line.split(_SEP)[-1]
            for line in _plain(result.text).splitlines()
            if _SEP in line
        ]

    def _paths(self, runner: Runner, query: list[str], root: str = "") -> list[Path]:
        """Every file beets registered, absolute. ADR-059."""
        return [_rooted(root, said) for said in self._said_paths(runner, query)]

    def stale(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Stale | None:
        """Rows whose files are all gone, or nothing. ADR-050."""
        rows = self._quietly_filed(runner, library, artist, album, mbid)
        return _stale_of(rows, artist, album)

    def _hold_filed(
        self,
        runner: Runner,
        library: str,
        artist: str,
        album: str,
        mbid: str,
        directory: str,
        *,
        none_is_fine: bool = False,
        at_least: int | None = None,
    ) -> A.Held | None:
        """Exactly `filed`'s files moved aside, or a plain refusal with nothing moved.
        Every row is proved first, since these paths come from another program. Their
        rows stay: only when the new copy is proved are they forgotten, so a failure has
        only to rename files back."""
        rows = self._filed_rows(runner, library, artist, album, mbid)
        if not rows and none_is_fine:
            return None
        refused = replace_refusal(rows, artist, album, mbid)
        if refused:
            raise ImportFailed(refused)
        # An album that arrived cut has no plan to count against. Fewer than are filed
        # would lose the rest; more completes a copy that was short, and is allowed.
        if at_least is not None and at_least < len(rows):
            raise ImportFailed(partial_cut(at_least, len(rows)))
        root = Path(library).resolve()
        for r in rows:
            if not contains(root, r.path):
                raise ImportFailed(f"refusing to delete {r.path}: outside {root}")
        try:
            return A.hold(
                library,
                A.Held(
                    Path(),
                    tuple(r.row for r in rows),
                    tuple(r.path for r in rows),
                    (),
                    directory,
                    artist,
                    album,
                    mbid,
                ),
            )
        except OSError as e:
            raise ImportFailed(f"nothing was replaced: {e}") from e

    def _new_copy(
        self, runner: Runner, library: str, held: A.Held, expected: int
    ) -> tuple[Row, ...]:
        """The rows the import added, once they are all that was asked for. Until this
        has answered, what it replaces is only moved aside."""
        rows = self._filed_rows(runner, library, held.artist, held.album, held.mbid)
        new = tuple(r for r in rows if r.row not in held.rows)
        there = sum(1 for r in new if r.present)
        if there != expected or len(new) != expected:
            raise ImportFailed(
                f"the library holds {there} of the {expected} new tracks, so what was "
                "filed before has been put back"
            )
        return new

    def _finish_replace(
        self, runner: Runner, library: str, held: A.Held, new: tuple[Row, ...]
    ) -> tuple[list[Path], int, int]:
        """The new copy is proved, so the old goes: rows, then what was held, then the
        folders that leaves. A kill anywhere in here is finished by `recover`. Once the
        old rows are gone, `%aunique` no longer has reason to keep the new copy in a
        folder of its own, and `move` says where it belongs. It is filed either way."""
        settled = A.proved(held)
        self._forget_ids(runner, settled.rows)
        A.drop(settled)
        removed = [Path(p) for p in settled.files]
        folders = _clear_emptied(library, [Path(p) for p in settled.everything])
        for album in sorted({r.album_row for r in new if r.album_row}):
            self._complaint(runner, ["move", "-a", f"id:{album}"], 3600)
        return removed, len(settled.covers), folders

    def _undo_replace(
        self, runner: Runner, library: str, held: A.Held, why: Exception
    ) -> None:
        """Back as it was: anything the import did add returns to where it came from,
        and the held files to their paths. The rows never left. Anything that will not
        go back stays held, and is said with the failure that brought this here."""
        trouble = ""
        added: list[Row] = []
        try:
            rows = self._filed_rows(runner, library, held.artist, held.album, held.mbid)
            added = [r for r in rows if r.row not in held.rows]
            if added:
                self._forget_ids(runner, tuple(r.row for r in added))
                for r in added:
                    _return_file(Path(r.path), Path(held.directory))
        except (ImportFailed, ToolMissing, OSError) as e:
            trouble = f" Rows it added may still be there: {e}."
        stuck = A.put_back(held)
        _clear_emptied(library, [Path(r.path) for r in added])
        if stuck:
            trouble += (
                f" {len(stuck)} files of the earlier copy could not go back, because "
                f"something is at their paths, and are held in {held.where}."
            )
        if trouble:
            why.args = (f"{why}\n\n{trouble.strip()}",)

    def recover(self, runner: Runner, library: str) -> list[str]:
        """Whatever a kill left held in this library: finished if its new copy had been
        proved, otherwise put back. Says what it did, and what it could not."""
        said = []
        for held in A.found(library):
            if held.rows == ("?",):
                said.append(f"{held.where} cannot be read and was left where it is")
            elif held.proved:
                self._forget_ids(runner, held.rows)
                A.drop(held)
                said.append(f"finished replacing {held.album or held.mbid}")
            else:
                failure = ImportFailed("a replace was interrupted")
                self._undo_replace(runner, library, held, failure)
                said.append(f"{failure} and {held.album or held.mbid} was put back")
        return said

    def _refuse_held(self, runner: Runner, library: str) -> None:
        """Nothing is filed past a copy that could not be put back."""
        self.recover(runner, library)
        left = A.found(library)
        if left:
            raise ImportFailed(
                f"an earlier replace left files held in {left[0].where} that could not "
                "go back. Move them or what is in their way, then file again"
            )

    def _forget_ids(self, runner: Runner, rows: Collection[str]) -> None:
        """By row id and not path: the new copy may be at the very paths of the old."""
        if not rows:  # with no query at all, beets would take the whole library
            return
        terms = [part for r in rows for part in (",", f"id:{r}")][1:]
        result = runner.run(self._argv(["remove", "-f", *terms]), timeout=600)
        if not result.ok:
            why = TapeRun(result.returncode, result.err).why()
            raise ImportFailed(f"beets could not clear those rows: {why}")

    def _forget_rows(self, runner: Runner, rows: Collection[Row]) -> None:
        """By each exact path, in one command, and with no `-d`, so no file goes."""
        if not rows:  # with no query at all, beets would take the whole library
            return
        terms = [part for r in rows for part in (",", _one_file(r.path))][1:]
        result = runner.run(self._argv(["remove", "-f", *terms]), timeout=600)
        if not result.ok:
            why = TapeRun(result.returncode, result.err).why()
            raise ImportFailed(f"beets could not clear those rows: {why}")

    def unregistered(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> list[Path]:
        """Audio in the album's directory the library does not account for.

        What serves a library reads the directory, not the database. ADR-060.
        """
        where, _count = self.locate(runner, library, artist, album, mbid=mbid)
        if where is None or not where.is_dir():
            return []
        # By the folder and not the record: another edition can share it, and that is
        # in the library. A path ending in a comma cannot be asked about at all.
        if str(where).endswith(","):
            return []
        known = set(self._paths(runner, [f"path:{where}"], library))
        return sorted(p for p in where.glob("*.flac") if p not in known)

    def all_stale(self, runner: Runner, library: str = "") -> tuple[int, int]:
        """How many registered files are missing, of how many registered."""
        paths = self._paths(runner, [], library)
        return sum(1 for p in paths if not p.exists()), len(paths)

    def relative_rows(self, runner: Runner, library: str = "") -> tuple[int, int]:
        """How many rows hold a relative path, of how many registered."""
        said = self._said_paths(runner, [])
        return sum(1 for path in said if _relative_row(library, path)), len(said)

    def clear_stale(
        self, runner: Runner, library: str, artist: str, album: str, *, mbid: str = ""
    ) -> Stale:
        """Drop the rows, never the files, and only once every path is gone.

        `beet remove` without `-d` leaves files alone, which is right here
        because there are none left to leave. A library off its mount has lost
        none of them, so nothing is cleared there. ADR-050.
        """
        self._refuse_off_its_mount(runner, library)
        rows = self._filed_rows(runner, library, artist, album, mbid)
        if not rows or any(Path(r.path).exists() for r in rows):
            raise ImportFailed(
                f"nothing stale for {album or mbid} - either beets has no rows for "
                "it, or the files it names are still there"
            )
        found = Stale(len(rows), Path(rows[0].path).parent, f"{artist} - {album}")
        self._forget_rows(runner, rows)
        left = self._filed_rows(runner, library, artist, album, mbid)
        if left:
            raise ImportFailed(f"{len(left)} row(s) are still registered")
        return found

    def album_rows(self, runner: Runner, root: str, album: str) -> tuple[Row, ...]:
        """The rows on an album of this name, with the release each is of."""
        return self._held(runner, root, [term("album", album)], 300.0)

    def held_rows(
        self,
        runner: Runner,
        root: str,
        terms: tuple[str, ...],
        timeout: float = 300.0,
    ) -> tuple[Row, ...]:
        """One query for many songs: a lone comma is beets' OR. No terms asks nothing,
        where beets would read it as the whole library. A failure to answer raises."""
        if not terms:
            return ()
        query = [part for term in terms for part in (",", term)][1:]
        return self._held(runner, root, query, timeout)

    def rows_at(
        self, runner: Runner, root: str, paths: Collection[str], timeout: float
    ) -> set[str]:
        """Which of these files beets has a row for, in one query. A path ending in a
        comma cannot be asked about alone or in a list, so it is taken to be there."""
        unaskable = {p for p in paths if p.endswith(",")}
        terms = tuple(f"path:{p}" for p in sorted(set(paths) - unaskable))
        return unaskable | {
            r.path for r in self.held_rows(runner, root, terms, timeout)
        }

    def _held(
        self, runner: Runner, root: str, query: list[str], timeout: float
    ) -> tuple[Row, ...]:
        result = runner.run(self._argv(["ls", "-f", _HELD, *query]), timeout=timeout)
        if not result.ok:
            raise ImportFailed(TapeRun(result.returncode, result.err).why())
        return tuple(
            _row(root, *fields)
            for fields in _fields(result.text)
            if len(fields) == _HELD.count(_SEP) + 1
        )


def _stale_of(rows: Sequence[Row], artist: str, album: str) -> Stale | None:
    if not rows or any(Path(r.path).exists() for r in rows):
        return None
    return Stale(len(rows), Path(rows[0].path).parent, f"{artist} - {album}")


def _merge_answers(how: str) -> bytes:
    """Nothing when no merge was asked for. A question a merge with nothing to add to
    did not foresee is Skip, which leaves every file where it is."""
    if not how:
        return b""
    return SKIP_ANY_OTHER if how == NEW_ALBUM else PINNED_ANSWERS[how]


def _return_file(path: Path, directory: Path) -> None:
    """A file the import took, back to where it took it from, overwriting nothing."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    n = 0
    while target.exists():
        n += 1
        target = directory / f"{path.stem} {n}{path.suffix}"
    if path.is_file():
        path.rename(target)


def _clear_emptied(library: str, removed: list[Path]) -> int:
    """The folders these files were in, and those above them, up to and not including
    the root, as far as each is now empty. `rmdir` is the check: it refuses a folder
    with anything in it, so nothing else can go, nor one this did not empty."""
    root = Path(library).resolve()
    cleared = 0
    for folder in sorted({p.parent for p in removed}, key=lambda p: -len(p.parts)):
        while folder.resolve() != root and contains(root, folder):
            try:
                folder.rmdir()
            except FileNotFoundError:  # went with a deeper one on this same walk
                pass
            except OSError:  # it holds something, so everything above it does too
                break
            else:
                cleared += 1
            folder = folder.parent
    return cleared


def holds_nothing(library: str) -> bool:
    try:
        return not any(Path(library).iterdir())
    except OSError:  # not there at all, or not to be read: neither holds a song
        return True


def _apart_by(album_path: str) -> bool | None:
    """Its folders, without the file's own name. Only the album artist and album make a
    folder two rows are known to share: a year or a disc is different between editions,
    and a function could say anything."""
    folders = album_path.strip().strip("'\"").rpartition("/")[0]
    if "%aunique" in folders:
        return True
    fields = set(_PATH_FIELD.findall(folders))
    return None if "%" in folders or fields != {"album", "albumartist"} else False


def _beside(files: tuple[str, ...], path: str) -> bool:
    return bool(files) and Path(files[0]).parent == Path(path).parent


def _one_file(path: str) -> str:
    """Beets takes this as that exact path, whatever the path holds, except for a
    trailing comma: that is an OR with everything, and a `remove` would forget the
    whole library. There is no escape for it, so it is never sent."""
    if path.endswith(","):
        raise ImportFailed(f"beets cannot be asked about {path} alone: it ends in ,")
    return f"path:{path}"


def _fields(said: str) -> list[list[str]]:
    """By `\\n` alone: beets ends a row with that, and `splitlines` would also break
    one at any of the line separators a title may hold."""
    return [line.split(_SEP) for line in _plain(said).split("\n")]


def _row(
    root: str,
    title: str,
    artist: str,
    album: str,
    length: str,
    said: str,
    recording: str,
    track: str,
    release: str = "",
    album_artist: str = "",
    row: str = "",
    album_row: str = "",
) -> Row:
    where = _rooted(root, said)
    return Row(
        title,
        artist,
        album,
        row_length(length),
        str(where),
        where.is_file(),
        recording=recording,
        track=int(track) if track.isdigit() else 0,
        release=release,
        album_artist=album_artist,
        row=row,
        album_row=album_row,
    )


def _rooted(root: str, said: str) -> Path:
    """What beets said, as something that can be stat'd. ADR-059."""
    where = Path(said)
    return where if where.is_absolute() or not root else Path(root) / where


def _relative_row(root: str, said: str) -> bool:
    """beets may resolve a relative row against where it runs, so the path it names
    can be absolute and missing: the row was relative if that tail is under the root."""
    where = Path(said)
    if not where.is_absolute():
        return True
    here = Path.cwd()
    return (
        bool(root)
        and where.is_relative_to(here)
        and not where.exists()
        and (Path(root) / where.relative_to(here)).is_file()
    )


# beets prints the release it settled on as a URL. More than one means it is
# offering a choice rather than making one, and that is not a proposal. ADR-062.
_RELEASE = re.compile(r"musicbrainz\.org/release/([0-9a-fA-F-]{36})")


def proposed(output: str) -> str:
    """The one release beets settled on, or nothing if it did not settle."""
    found = list(dict.fromkeys(_RELEASE.findall(output)))
    return found[0] if len(found) == 1 else ""


def partial_cut(cut: int, expected: int) -> str:
    """Why these cuts may not replace a whole record, or nothing."""
    if cut == expected:
        return ""
    return (
        f"{cut} cut tracks are waiting and this record has {expected}, so replacing "
        "would lose the rest. Nothing was changed. Cut it again, then replace"
    )


def fewer_than_cut(count: int, expected: int) -> str:
    return (
        f"the import ran, and the library holds {count} of {expected} tracks of this "
        "record. Look in the library before filing it again"
    )


def too_many(count: int, expected: int) -> list[str]:
    """Say so when the library holds more of a record than was cut. ADR-057."""
    if count <= expected:
        return []
    return [
        f"the library holds {count} tracks and this record has {expected} - "
        "an earlier copy was not replaced, and both are there"
    ]


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


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
