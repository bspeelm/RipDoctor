"""Turning a missing tool or an unset field into something actionable."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ripdoctor.audio.capture import SAMPLE_FORMATS as CAPTURE_FORMATS
from ripdoctor.audio.devices import enumerate_devices
from ripdoctor.audio.runner import Runner, ToolFailed, ToolMissing
from ripdoctor.config.machine import Machine
from ripdoctor.config.settings import Settings, nearest
from ripdoctor.config.thresholds import Thresholds
from ripdoctor.integrations.importer import Beets


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Result:
    """One finding. `fix` is what to do, not what went wrong."""

    check: str
    level: Level
    summary: str
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.level is Level.FAIL


# Required to do anything at all, and what each is for. Named so a report says
# why a tool is wanted rather than only that it is absent.
REQUIRED = {
    "ffmpeg": "measure levels, cut tracks and build clips",
    "ffprobe": "read a file's sample rate",
    "flac": "verify a written track",
    "beet": "tag and file a finished record",
    "metaflac": "write tags and embed cover art",
}
OPTIONAL = {
    "arecord": "record from a turntable attached to this machine",
}

# What to install to get each one, where that is not the tool's own name.
# "install arecord" is not a command anybody can run, and a fix line that
# cannot be typed is half a fix.
PACKAGE = {
    "ffprobe": "ffmpeg",
    "metaflac": "flac",
    "arecord": "alsa-utils",
    "beet": "beets - it is a dependency, so a missing one means a broken install",
}


def _install(tool: str) -> str:
    return PACKAGE.get(tool, tool)


def tools(runner: Runner) -> Iterator[Result]:
    for tool, purpose in REQUIRED.items():
        if runner.which(tool):
            yield Result(tool, Level.OK, f"{tool} found")
        else:
            yield Result(
                tool,
                Level.FAIL,
                f"{tool} is not installed - needed to {purpose}",
                fix=f"install {_install(tool)}",
            )
    for tool, purpose in OPTIONAL.items():
        if runner.which(tool):
            yield Result(tool, Level.OK, f"{tool} found")
        else:
            yield Result(
                tool,
                Level.WARN,
                f"{tool} is not installed - {purpose} is unavailable",
                fix=f"install {_install(tool)} if you want to {purpose}",
            )


def _writable(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.access(probe, os.W_OK)


def storage(settings: Settings) -> Iterator[Result]:
    for name, value in (("vinyl", settings.vinyl), ("library", settings.library)):
        if not value:
            yield Result(
                name,
                Level.FAIL,
                f"{name} directory is not set",
                fix=f"set {name} in config.toml",
            )
            continue
        path = Path(value)
        if not path.exists():
            yield Result(
                name,
                Level.WARN,
                f"{name} directory does not exist yet: {path}",
                fix="it is created on first use, or make it now",
            )
        elif not _writable(path):
            yield Result(
                name,
                Level.FAIL,
                f"{name} directory is not writable: {path}",
                fix="check the owner and mode",
            )
        else:
            yield Result(name, Level.OK, f"{name}: {path}")


IMPORTERS = ("tagger", "beets")


def importing(settings: Settings, runner: Runner) -> Iterator[Result]:
    """Which importer will actually be used, when that is not what was asked.

    `choose` falls back rather than refusing, because being told to install
    something at the last step of a twenty-minute job is not a useful answer.
    A silent fallback is exactly what this exists to say out loud.
    """
    if settings.importer not in IMPORTERS:
        yield Result(
            "importer",
            Level.FAIL,
            f"{settings.importer} is not an importer this knows",
            fix=f"set importer to one of {', '.join(sorted(IMPORTERS))}",
        )
        return
    if settings.importer == "beets" and not runner.which("beet"):
        yield Result(
            "importer",
            Level.FAIL,
            "importer is beets, but beet is not on the path - the built-in "
            "tagger will be used instead",
            fix="reinstall ripdoctor, which pulls beets in with it",
        )
        return
    yield Result("importer", Level.OK, f"importing with {settings.importer}")
    if settings.importer == "beets":
        yield from _agree(settings, runner)


def _agree(settings: Settings, runner: Runner) -> Iterator[Result]:
    """Whether beets files a record where this expects to find one.

    Two settings name the library and nothing makes them agree. A warning
    rather than a failure: only the person who set them knows which is the one
    that is wrong. ADR-042.
    """
    try:
        theirs = Beets().files_into(runner)
    except (ToolMissing, ToolFailed):
        return
    if not theirs or not settings.library:
        return
    if Path(theirs).resolve() != Path(settings.library).resolve():
        yield Result(
            "library",
            Level.WARN,
            f"beets files records into {theirs}, not {settings.library}",
            fix=f"set `directory: {settings.library}` in beets' own config "
            "(`beet config -p` says where that is), or point library here",
        )
        return
    yield Result("library", Level.OK, f"beets files records into {theirs}")


def capture(settings: Settings, runner: Runner) -> Iterator[Result]:
    if not runner.which("arecord"):
        return
    if not settings.capture_device:
        yield Result(
            "capture_device",
            Level.WARN,
            "no capture device is configured",
            fix="run `ripdoctor devices`, then set capture_device in config.toml",
        )
        return
    if settings.capture_format not in CAPTURE_FORMATS:
        yield Result(
            "capture_format",
            Level.FAIL,
            f"{settings.capture_format} is not a sample format this records in",
            fix=f"set capture_format to one of {', '.join(sorted(CAPTURE_FORMATS))}",
        )

    # The device is checked against what the box can see, not just read back.
    # A USB interface that has lost contact is still in the configuration file,
    # and a capture against whatever answered instead is twenty minutes of the
    # wrong input - which is how 162 seconds of mic-jack bleed once got recorded.
    seen = {d.id for d in enumerate_devices(runner)}
    if settings.capture_device not in seen:
        yield Result(
            "capture_device",
            Level.FAIL,
            f"{settings.capture_device} is not a device this machine can see",
            fix=(
                "if that is the turntable it has lost its connection - check the "
                "cable before recording twenty minutes of nothing"
            ),
        )
        return

    yield Result(
        "capture_device",
        Level.OK,
        f"capture: {settings.capture_device} "
        f"{settings.capture_format} {settings.capture_rate} Hz",
    )


def configuration(settings: Settings, thresholds: Thresholds) -> Iterator[Result]:
    for key in settings.unknown:
        hint = nearest(key)
        yield Result(
            "config",
            Level.WARN,
            f"unrecognised setting: {key}",
            fix=f"did you mean {hint}?" if hint else "remove it, or check the spelling",
        )
    for key in settings.unreadable:
        yield Result(
            "config",
            Level.WARN,
            f"setting could not be read: {key}",
            fix="check its type; the default is being used",
        )

    moved = thresholds.changed_from_default()
    if moved:
        listed = ", ".join(f"{k} {was}->{now}" for k, (was, now) in moved.items())
        yield Result(
            "thresholds",
            Level.WARN,
            f"{len(moved)} threshold(s) moved from the measured default: {listed}",
            fix="the first thing to check if detection behaves unexpectedly",
        )


def run_all(
    machine: Machine, settings: Settings, thresholds: Thresholds, runner: Runner
) -> list[Result]:
    out = [
        *tools(runner),
        *storage(settings),
        *capture(settings, runner),
        *importing(settings, runner),
        *configuration(settings, thresholds),
    ]
    out.append(
        Result("config_file", Level.OK, f"settings: {machine.settings_file}")
        if machine.settings_file.exists()
        else Result(
            "config_file",
            Level.OK,
            f"no settings file yet; using defaults ({machine.settings_file})",
        )
    )
    return out


def report(results: list[Result]) -> str:
    """A report a person can act on: the worst first, each with what to do."""
    order = {Level.FAIL: 0, Level.WARN: 1, Level.OK: 2}
    mark = {Level.FAIL: "FAIL", Level.WARN: "warn", Level.OK: "ok  "}
    lines = [
        f"  {mark[r.level]}  {r.summary}" + (f"\n        {r.fix}" if r.fix else "")
        for r in sorted(results, key=lambda r: order[r.level])
    ]
    fails = sum(1 for r in results if r.level is Level.FAIL)
    warns = sum(1 for r in results if r.level is Level.WARN)
    lines.append("")
    lines.append(
        "  cannot run: " + str(fails) + " problem(s) to fix"
        if fails
        else f"  ready ({warns} warning(s))"
        if warns
        else "  ready"
    )
    return "\n".join(lines)


def ok(results: list[Result]) -> bool:
    return not any(r.blocking for r in results)
