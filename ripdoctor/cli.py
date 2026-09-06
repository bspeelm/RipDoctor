"""Command-line entry point.

Subcommands are added as each layer lands; see docs/decisions.md. This module is
the only one permitted to call sys.exit.
"""

from __future__ import annotations

import argparse

from ripdoctor import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ripdoctor",
        description="Capture a vinyl side, find the track boundaries, "
        "cut them where you want them.",
    )
    p.add_argument("--version", action="version", version=f"ripdoctor {__version__}")
    p.set_defaults(run=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run is None:
        build_parser().print_help()
        return 0
    exit_code: int = args.run(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
