"""Serving the front end, and refusing to serve anything else."""

from __future__ import annotations

import posixpath
from pathlib import Path

from ripdoctor.web import http as H

INDEX = "index.html"

# The front end is three files and a stylesheet, all local. It changes when the
# server does, so it is revalidated rather than cached for an hour: a stale
# app.js against a new API is a page that fails in ways nobody can reproduce.
CACHE = "no-cache"


def handler(root: str | Path):  # type: ignore[no-untyped-def]
    """A route that serves files from `root`, and only from `root`."""
    base = Path(root).resolve()

    def serve(request: H.Request) -> H.Response:
        relative = posixpath.normpath("/" + request.path).lstrip("/") or INDEX
        target = (base / relative).resolve()
        # Resolved before the check, so a symlink out of the tree is caught.
        # Every component of such a path is perfectly ordinary; only resolving
        # it shows where it goes.
        if target != base and base not in target.parents:
            raise H.HttpError(403, "forbidden")
        if not target.is_file():
            raise H.HttpError(404, "not found")
        return H.file_at(str(target), cache=CACHE)

    return serve
