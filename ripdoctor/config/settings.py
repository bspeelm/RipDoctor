"""Settings: defaults, overridden by a file that is never allowed to refuse.

A half-finished configuration still loads. Refusing here would make every
command fail with the same message - including the `doctor` that would explain
it and the edit that would complete it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from ripdoctor.config.machine import Machine
from ripdoctor.config.thresholds import Thresholds

SCHEMA = 1

# Keys that were renamed. Mapped forward rather than dropped, so a config
# written by an older version keeps working and says so.
RETIRED: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything a person may change.

    Machine-specific fields default to empty and are filled by detection, so the
    shipped defaults describe no particular computer.
    """

    schema: int = SCHEMA

    # Where the audio lives. The one path that has to be chosen.
    library: str = ""
    vinyl: str = ""

    # Capture. The device is named, never guessed: a heuristic on the card name
    # makes the application unusable on any machine but the one it was written
    # on. ADR-009.
    capture_device: str = ""
    capture_rate: int = 48000
    capture_format: str = "S24_3LE"
    capture_channels: int = 2

    # Serving.
    port: int = 8080
    bind: str = "127.0.0.1"

    # Cutting.
    lead: float = 1.3
    tail: float = 1.5
    embed_art: bool = True
    file_mode: int = 0o664
    dir_mode: int = 0o775

    # Read but not settable, and reported when they are not understood.
    unknown: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()

    @property
    def raw_dir(self) -> str:
        return f"{self.vinyl}/raw" if self.vinyl else ""

    @property
    def work_dir(self) -> str:
        return f"{self.vinyl}/work" if self.vinyl else ""

    @property
    def review_dir(self) -> str:
        return f"{self.vinyl}/review" if self.vinyl else ""

    @property
    def archive_dir(self) -> str:
        return f"{self.vinyl}/archive" if self.vinyl else ""


SETTABLE = tuple(
    f.name for f in fields(Settings) if f.name not in ("unknown", "unreadable")
)


def defaults(machine: Machine) -> Settings:
    """The shipped configuration, with detection filling the machine's part."""
    return Settings(
        vinyl=str(machine.state_dir / "vinyl"),
        library=str(machine.home / "Music"),
    )


def _coerce(name: str, value: Any, current: Any) -> Any:
    """Bring a TOML value to the field's type, or raise for the caller to note."""
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise TypeError(name)
        return value
    for kind in (int, float, str):
        if isinstance(current, kind):
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                raise TypeError(name)
            return kind(value)
    raise TypeError(name)


def merge(base: Settings, data: dict[str, Any]) -> Settings:
    """Apply a parsed file over the defaults.

    An unrecognised key is a warning, never a refusal, and a key whose value is
    the wrong type is dropped and named. Both are reported on the returned
    value so the doctor can say so; neither stops the program.
    """
    changes: dict[str, Any] = {}
    unknown: list[str] = []
    unreadable: list[str] = []

    for key, value in data.items():
        name = RETIRED.get(key, key)
        if name not in SETTABLE:
            unknown.append(key)
            continue
        try:
            changes[name] = _coerce(name, value, getattr(base, name))
        except (TypeError, ValueError):
            unreadable.append(key)

    return replace(
        base, **changes, unknown=tuple(unknown), unreadable=tuple(unreadable)
    )


def load(machine: Machine, path: Path | None = None) -> Settings:
    """Read the settings. A missing file means every default, which is what a
    fresh install is. An unreadable one means the same, and says so."""
    base = defaults(machine)
    where = path or machine.settings_file
    try:
        raw = where.read_bytes()
    except OSError:
        return base
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return replace(base, unreadable=(str(where),))
    return merge(base, data)


def nearest(key: str, known: tuple[str, ...] = SETTABLE) -> str | None:
    """The closest real key to a typo, or nothing.

    Suggesting `port` for `librari` is worse than suggesting nothing, so the
    distance allowed scales with the length of what was typed.
    """
    limit = min(len(key) // 3, 3)
    if limit < 1:
        return None
    best, best_d = None, limit + 1
    for candidate in known:
        d = _distance(key, candidate)
        if d < best_d:
            best, best_d = candidate, d
    return best


def _distance(a: str, b: str) -> int:
    """Edit distance counting an adjacent swap as one change, not two.

    Typing `prot` for `port` is a single slip. Plain Levenshtein scores it 2 and
    a limit tight enough to be useful then rejects it, which is the wrong answer
    for the most common typo there is.
    """
    if a == b:
        return 0
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            rows[i][j] = min(
                rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def describe(settings: Settings, thresholds: Thresholds) -> dict[str, Any]:
    """Every resolved value, for the doctor and for `--json`.

    Reported rather than left implicit: a wrong path is far easier to see than
    to deduce from behaviour.
    """
    out: dict[str, Any] = {name: getattr(settings, name) for name in SETTABLE}
    out["thresholds"] = thresholds.as_dict()
    return out
