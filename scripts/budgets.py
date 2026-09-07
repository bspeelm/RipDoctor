#!/usr/bin/env python3
"""Size budgets, enforced as a failing check.

Budgets are not style. They are the mechanism that keeps this project the size
it claims to be: a small algorithm with a thin application around it. Each one
fails the build when exceeded.

Do not raise a budget to make a change fit. Retire something, or write a
decision record explaining why the number moved.

The prose budget exists for the same reason as the comment budget. Documentation
about this project grows faster than the project does, and past a point the
writing becomes the work. Every markdown file counts; only docs/history/ is
exempt, being append-only by design.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Budgets. Each is a ceiling, and each was chosen deliberately.
MAX_CORE_LINES = 2000  # the published algorithm; it should stay small
MAX_TOTAL_LINES = 8000  # everything under ripdoctor/
MAX_CORE_COMMENT_RATIO = 60  # core carries the findings; see ADR-018
MAX_OTHER_COMMENT_RATIO = 35  # plumbing does not
MAX_DOC_RATIO = 75  # markdown lines as a per cent of code lines
MAX_WHEEL_BYTES = 2 * 1024 * 1024

# Ratios need a denominator worth dividing by. Below this many lines of code a
# percentage says nothing about the project - a skeleton is almost entirely
# docstring, and would fail a cap that is correct for a finished codebase. Below
# the floor the ratios are printed but not enforced, so they stay visible and
# the point at which they start biting is predictable rather than a surprise.
# ADR-012.
RATIO_FLOOR_LINES = 500

# Two ceilings, because these are two kinds of code. core/ is the published
# algorithm and its comments are the experimental record - which threshold came
# from which measurement, which approach was tried and abandoned. Measured with
# this same counter, the predecessor modules core was ported from run 55.0 per
# cent and RipDoctor's core runs 52.6. Everything outside core is subprocess
# plumbing, routing and file handling, where that density would be noise; the
# predecessor's application as a whole runs 32.8 per cent. ADR-018.

# A layer needs this much code before its ratio means anything. Lower than the
# whole-project floor because a layer is smaller by definition.
LAYER_FLOOR_LINES = 250


def count_python(path: Path) -> tuple[int, int]:
    """Return (code lines, comment lines) for one file.

    Docstrings count as comments, not code. A module that is mostly explanation
    should read as mostly explanation in the numbers.
    """
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    comment = sum(1 for ln in lines if ln.strip().startswith("#"))
    blank = sum(1 for ln in lines if not ln.strip())

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return len(lines) - comment - blank, comment

    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        # The docstring node spans end_lineno - lineno + 1 physical lines.
        body = node.body[0]
        comment += (body.end_lineno or body.lineno) - body.lineno + 1

    return max(len(lines) - comment - blank, 0), comment


def tally(where: Path) -> tuple[int, int]:
    code = comment = 0
    for f in sorted(where.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        c, m = count_python(f)
        code += c
        comment += m
    return code, comment


# Prose that is append-only by design is exempt, because the remedy this budget
# demands - retire something - cannot be applied to it. docs/decisions.md says so
# in its own header: superseded entries stay in place with a note. Deleting an
# ADR to fit a cap destroys the record the cap exists to keep honest. ADR-017.
DOC_EXEMPT = {"decisions.md"}


def markdown_lines() -> int:
    """Living prose only: no build artifacts, no append-only records."""
    total = 0
    for f in sorted(ROOT.rglob("*.md")):
        parts = f.relative_to(ROOT).parts
        if any(p.startswith(".") for p in parts):
            continue  # .venv, .git, .pytest_cache and friends
        if parts[0] in {"venv", "node_modules"} or "history" in parts:
            continue
        if f.name in DOC_EXEMPT:
            continue
        total += len(f.read_text(encoding="utf-8").splitlines())
    return total


def check(label: str, actual: float, ceiling: float, unit: str = "") -> bool:
    ok = actual <= ceiling
    mark = "ok  " if ok else "OVER"
    print(f"  {mark}  {label:<28} {actual:>8.0f}{unit} / {ceiling:.0f}{unit}")
    return ok


def main() -> int:
    pkg = ROOT / "ripdoctor"
    core_code, _ = tally(pkg / "core")
    all_code, all_comment = tally(pkg)
    docs = markdown_lines()

    print("budgets:")
    results = [
        check("core lines", core_code, MAX_CORE_LINES),
        check("total lines", all_code, MAX_TOTAL_LINES),
    ]

    core_c, core_m = tally(pkg / "core")
    other_c, other_m = all_code - core_c, all_comment - core_m
    for label, code, comment, ceiling in (
        ("core comment ratio", core_c, core_m, MAX_CORE_COMMENT_RATIO),
        ("other comment ratio", other_c, other_m, MAX_OTHER_COMMENT_RATIO),
    ):
        if code >= LAYER_FLOOR_LINES:
            results.append(check(label, 100 * comment / code, ceiling, "%"))
        elif code:
            print(
                f"  info  {label:<28} {100 * comment / code:>7.0f}% / {ceiling}%"
                f"   (under {LAYER_FLOOR_LINES} lines, at {code})"
            )

    if all_code >= RATIO_FLOOR_LINES:
        results.append(check("doc ratio", 100 * docs / all_code, MAX_DOC_RATIO, "%"))
    elif all_code:
        print(
            f"  info  doc ratio                   {100 * docs / all_code:>7.0f}%"
            f" / {MAX_DOC_RATIO}%   (under {RATIO_FLOOR_LINES} lines)"
        )

    # web/ must not outgrow the algorithm it presents. In the predecessor a
    # single HTTP module was larger than the entire detection core.
    web_code, _ = tally(pkg / "web")
    if core_code:
        results.append(check("web vs core lines", web_code, core_code))

    # The strongest budget in the project, and the cheapest to verify.
    with (ROOT / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    results.append(check("runtime dependencies", len(deps), 0))

    if "--wheel" in sys.argv:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(ROOT / "dist")],
            cwd=ROOT,
            check=True,
        )
        wheels = sorted((ROOT / "dist").glob("*.whl"))
        if wheels:
            newest = max(wheels, key=lambda p: p.stat().st_mtime)
            results.append(
                check("wheel bytes", newest.stat().st_size, MAX_WHEEL_BYTES, "B")
            )

    if not all(results):
        print("\nA budget was exceeded. Retire something, or write an ADR.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
