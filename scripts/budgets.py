#!/usr/bin/env python3
"""Size budgets, enforced as a failing check.

Four numbers, one of each kind, matching the scheme this project's process was
taken from. No per-layer ceilings, no floor below which they stop applying, and
no file exempted for being inconvenient: a cap on one directory is one the prose
walks out of, and moving a section from the README into the decision log would
otherwise read as an improvement while nothing was retired.

**The comment ratio is hard.** Never exceeded, never raised. If prose has
outgrown it, move findings into docs/method.md - that is what the file is for.
Over budget means retiring, not raising.

**The other three are soft**, and soft does not mean ignore. It means the choice
between raising a ceiling and writing worse code belongs to the author. Never
quietly trim a function, drop a guard or skip a case to fit a number: stop and
say the ceiling is in the way.

Changing any number requires a decision record saying why it moved.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAX_CODE_LINES = 4000
MAX_COMMENT_RATIO = 25
MAX_DOC_RATIO = 75
MAX_WHEEL_BYTES = 2 * 1024 * 1024

# Append-only records are the only prose exempt, because the remedy this budget
# asks for - retire something - cannot be applied to them.
DOC_EXEMPT_DIRS = {"history", "review"}


def count_python(path: Path) -> tuple[int, int]:
    """Return (code lines, comment lines). Docstrings count as comments."""
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
        if ast.get_docstring(node, clean=False) is None:
            continue
        body = node.body[0]
        comment += (body.end_lineno or body.lineno) - body.lineno + 1

    return max(len(lines) - comment - blank, 0), comment


def sources() -> list[Path]:
    """Shipping code: the package, excluding tests and caches."""
    return [
        f
        for f in sorted((ROOT / "ripdoctor").rglob("*.py"))
        if "__pycache__" not in f.parts
    ]


def live_docs() -> list[Path]:
    out = []
    for f in sorted(ROOT.rglob("*.md")):
        parts = f.relative_to(ROOT).parts
        if any(p.startswith(".") for p in parts):
            continue
        if parts[0] in {"venv", "node_modules"} or DOC_EXEMPT_DIRS & set(parts):
            continue
        out.append(f)
    return out


def main() -> int:
    code = comment = 0
    for f in sources():
        c, m = count_python(f)
        code += c
        comment += m
    docs = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in live_docs())

    if code == 0:
        print("no code yet")
        return 0

    ratio = comment * 100 // code
    dratio = docs * 100 // code

    print(f"code:     {code} lines (budget {MAX_CODE_LINES})")
    print(f"comments: {comment} lines, {ratio}% of code (budget {MAX_COMMENT_RATIO}%)")
    print(f"prose:    {docs} lines, {dratio}% of code (budget {MAX_DOC_RATIO}%)")

    failed = False
    if ratio > MAX_COMMENT_RATIO:
        print(
            "\nover the comment budget - this ceiling is hard."
            "\nMove findings into docs/method.md. Do not raise it."
        )
        failed = True
    if code > MAX_CODE_LINES:
        print("\nover the code budget - the author decides whether it moves.")
        failed = True
    if dratio > MAX_DOC_RATIO:
        print("\nover the prose budget - retire a document, or the author decides.")
        failed = True

    if "--wheel" in sys.argv:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(ROOT / "dist")],
            cwd=ROOT,
            check=True,
        )
        wheels = sorted((ROOT / "dist").glob("*.whl"))
        if wheels:
            size = max(wheels, key=lambda p: p.stat().st_mtime).stat().st_size
            print(f"wheel:    {size} bytes (budget {MAX_WHEEL_BYTES})")
            if size > MAX_WHEEL_BYTES:
                print("\nover the wheel budget.")
                failed = True

    with (ROOT / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    print(f"deps:     {len(deps)} runtime dependencies (budget 0)")
    if deps:
        print("\nthe base install must pull in nothing.")
        failed = True

    if failed:
        print("\nDo not argue with a budget; write a decision record to change one.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
