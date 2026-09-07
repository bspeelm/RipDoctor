"""Session-wide guards.

A test filter that matches nothing exits successfully, so a job that was meant
to run the ffmpeg tier and quietly ran none of it reports green. `--require`
makes that impossible: name a marker, and the session fails unless tests
carrying it actually ran.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require",
        action="append",
        default=[],
        metavar="MARKER",
        help="fail the session unless tests with this marker ran and passed",
    )


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    required = config.getoption("--require")
    if not required:
        return

    passed = terminalreporter.stats.get("passed", [])
    for marker in required:
        ran = sum(1 for r in passed if marker in r.keywords)
        if ran == 0:
            raise pytest.UsageError(
                f"no tests marked {marker!r} passed - the filter matched nothing, "
                "which is not the same as the tests passing"
            )
        terminalreporter.write_line(f"{ran} test(s) marked {marker!r} passed")
