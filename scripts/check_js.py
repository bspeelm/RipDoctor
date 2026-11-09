#!/usr/bin/env python3
"""Parse every script the page loads, as the modules a browser loads them as.

A syntax error anywhere in one of these blanks every page, and nothing else here
would notice: the page has no build step and Python cannot read it. Node is not a
dependency of this project, so with no node on the machine this says so and steps
aside, unless told it has to run. CI tells it to.

`node --check` decides how to read a .js file by its version and what it finds there,
and older ones call `import` itself the error. A browser reads these as modules, so
each is checked under a .mjs name, which every version reads as one.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "ripdoctor" / "web" / "static"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require", action="store_true", help="fail if node is absent")
    parser.add_argument("--dir", type=Path, default=STATIC, help="scripts to check")
    args = parser.parse_args(argv)

    node = shutil.which("node")
    if node is None:
        print("js: node is not installed, so the scripts were NOT parsed")
        return 1 if args.require else 0

    scripts = sorted(args.dir.glob("*.js"))
    if not scripts:
        print(f"js: no scripts in {args.dir}, which is not the same as them parsing")
        return 1

    failed = 0
    with tempfile.TemporaryDirectory() as scratch:
        for script in scripts:
            copy = Path(scratch) / f"{script.stem}.mjs"
            shutil.copyfile(script, copy)
            done = subprocess.run(  # noqa: S603 - argv list, never shell=True
                [node, "--check", str(copy)],
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode:
                failed += 1
                print(f"js: {script.name} does not parse\n{done.stderr}")
    print(f"js: {len(scripts) - failed} of {len(scripts)} scripts parse as modules")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
