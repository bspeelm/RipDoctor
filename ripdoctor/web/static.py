"""Serving the front end, and refusing to serve anything else."""

from __future__ import annotations

import posixpath
from pathlib import Path

from ripdoctor.web import http as H

INDEX = "index.html"

# The front end is three files and a stylesheet, all local. It changes when the
# server does, so it is revalidated rather than cached for an hour: a stale
# app.js against a new API is a page that fails in ways nobody can reproduce.
#
# `no-cache` says revalidate, and revalidating needs something to revalidate
# against. Without a validator a browser cannot ask "has this changed?", so it
# decides for itself - and one that decided to keep app.js served a page from
# before a deployment against an API from after it, which is the failure this
# header was chosen to prevent.
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
        # The same size-and-mtime stamp a measurement is keyed to, for the same
        # reason: it changes when the file does and costs one stat to read.
        stat = target.stat()
        tag = f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
        if _asked_with(request, "If-None-Match") == tag:
            return H.Response(304, cache=CACHE, headers=(("ETag", tag),))
        return H.file_at(str(target), cache=CACHE, etag=tag)

    return serve


def _asked_with(request: H.Request, name: str) -> str:
    """One request header, whatever the client capitalised it as."""
    wanted = name.lower()
    return next(
        (v for k, v in request.headers.items() if k.lower() == wanted),
        "",
    )
