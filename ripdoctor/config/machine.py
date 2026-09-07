"""What this machine is. Detected once, passed around as a value. ADR-028."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

APP = "ripdoctor"


def _xdg(var: str, home: Path, *fallback: str) -> Path:
    value = os.environ.get(var, "")
    return Path(value) if value else home.joinpath(*fallback)


@dataclass(frozen=True, slots=True)
class Machine:
    """Where things live, and what this system is.

    Detection never fails. A field it could not determine is left empty and the
    doctor is what turns that into a visible problem - so every command still
    runs on a half-configured machine, including the doctor that would explain
    it and the `config set` that would fix it.
    """

    system: str = ""
    home: Path = field(default_factory=Path)
    config_home: Path = field(default_factory=Path)
    data_home: Path = field(default_factory=Path)
    root_override: Path | None = None

    @property
    def state_dir(self) -> Path:
        """Ours, and disposable. Generated files, caches, nothing of the user's.

        Removing this directory must never lose anything that cannot be
        recomputed.
        """
        return self.root_override or self.data_home / APP

    @property
    def config_dir(self) -> Path:
        """The user's. Settings and overrides; the directory to put in git."""
        return self.config_home / APP

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"


def detect(environ: dict[str, str] | None = None) -> Machine:
    """Read the machine. Never raises, and never writes anything."""
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or os.path.expanduser("~") or ".")

    override = env.get("RIPDOCTOR_DIR", "")
    return Machine(
        system=platform.system(),
        home=home,
        # These are read from the process environment rather than derived,
        # because a caller may point them elsewhere for a test or a container.
        config_home=Path(env["XDG_CONFIG_HOME"])
        if env.get("XDG_CONFIG_HOME")
        else home / ".config",
        data_home=Path(env["XDG_DATA_HOME"])
        if env.get("XDG_DATA_HOME")
        else home / ".local" / "share",
        root_override=Path(override) if override else None,
    )
