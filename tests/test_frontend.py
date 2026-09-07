"""The front end, checked the way the Python is.

There is no compiler here, so these stand in for one. The first is the check
that earned its place: three times in one session a string-replace edit silently
failed to match and left the JS reaching for a node that was never added, each
time surfacing as a TypeError on null in front of somebody using it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ripdoctor.web.app import App
from ripdoctor.web.auth import Sessions
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service

STATIC = Path(__file__).resolve().parent.parent / "ripdoctor" / "web" / "static"

ID = re.compile(r'id="([A-Za-z0-9_-]+)"')
REACHED = re.compile(r'\$\("#([A-Za-z0-9_-]+)')
# Two shapes: a plain string, and a template literal whose holes may contain
# quotes of their own.
QUOTED = re.compile(r"[\"'](/api/[^\"']*)")
TEMPLATE = re.compile(r"`(/api/[^`]*)`")
HOLE = re.compile(r"\$\{[^{}]*\}")


def scripts() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def markup() -> str:
    return "".join(p.read_text() for p in sorted(STATIC.glob("*.html")))


def test_there_is_a_front_end_at_all() -> None:
    """Every test below passes trivially against an empty directory."""
    assert scripts(), "no JavaScript was found"
    assert markup(), "no HTML was found"


def test_every_element_the_javascript_reaches_for_exists() -> None:
    ids = set(ID.findall(markup()))
    assert len(ids) > 20, "the markup has almost no elements; the pattern drifted"

    missing = []
    for script in scripts():
        source = script.read_text()
        for found in REACHED.finditer(source):
            if found.group(1) not in ids:
                line = source[: found.start()].count("\n") + 1
                missing.append(f'{script.name}:{line}  $("#{found.group(1)}")')
    assert not missing, "reached for and not in the HTML:\n  " + "\n  ".join(missing)


def test_every_module_the_javascript_imports_is_there() -> None:
    names = {p.name for p in scripts()}
    for script in scripts():
        for found in re.finditer(r'from "/([A-Za-z0-9_.-]+)"', script.read_text()):
            assert found.group(1) in names, f"{script.name} imports {found.group(1)}"


# A call that names a method, and the path it names it for. Matching a path
# alone let two calls through in one session: a prepare polled with GET against
# a POST-only route, and an upload posted to a path that only answers GET. Both
# read as "no such route" in front of somebody using it.
POSTING = re.compile(
    r"postJSON\(\s*[`\"'](/api/[^`\"']*)"
    r"|fetch\(\s*[`\"'](/api/[^`\"']*)[`\"'][^)]*?method:\s*[\"']POST",
    re.S,
)


def a_route_table() -> set[str]:
    service = Service(
        layout=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        thresholds=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        credentials=None,  # type: ignore[arg-type]
        sessions=Sessions(secret=b"0" * 32),
    )
    app: App = build(service)
    return {r.pattern.pattern for r in app.routes}


def posting_routes() -> set[str]:
    service = Service(
        layout=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        thresholds=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        credentials=None,  # type: ignore[arg-type]
        sessions=Sessions(secret=b"0" * 32),
    )
    app: App = build(service)
    return {r.pattern.pattern for r in app.routes if r.method == "POST"}


def matches(pattern: str, path: str) -> bool:
    return (
        re.match(
            pattern.replace("([^/]+)", "X").replace("(\\d+)", "1"),
            path.replace("{}", "X"),
        )
        is not None
    )


@pytest.mark.parametrize("script", scripts(), ids=lambda p: p.name)
def test_every_endpoint_the_page_calls_exists(script: Path) -> None:
    """A page calling a route that is not there fails in the browser, at the
    moment somebody presses the button, with a 404 and no explanation."""
    table = a_route_table()
    source = script.read_text()
    unknown = []
    for literal in sorted(set(QUOTED.findall(source) + TEMPLATE.findall(source))):
        # A hole stands for one path segment - "1", because a track index is
        # matched by a numeric pattern and a slug by any - or for nothing at
        # all, which is what a hole holding an optional query string is.
        stems = {
            HOLE.sub(fill, literal).split("?", 1)[0].rstrip("/") for fill in ("1", "")
        }
        if not any(re.match(p, stem) for p in table for stem in stems):
            unknown.append(literal)
    assert not unknown, f"{script.name} calls routes that do not exist: {unknown}"


@pytest.mark.parametrize("script", scripts(), ids=lambda p: p.name)
def test_every_post_the_page_makes_has_a_post_route(script: Path) -> None:
    """A route answering the wrong method is a 404 with a confusing message.
    Matching the path alone missed a poll sent with GET to a POST-only route,
    and an upload sent with POST to a path that only answers GET."""
    table = posting_routes()
    wrong = []
    for found in POSTING.finditer(script.read_text()):
        literal = found.group(1) or found.group(2)
        stems = {
            HOLE.sub(fill, literal).split("?", 1)[0].rstrip("/") for fill in ("1", "")
        }
        if not any(re.match(p, stem) for p in table for stem in stems):
            wrong.append(literal)
    assert not wrong, f"{script.name} posts to routes that take no POST: {wrong}"
