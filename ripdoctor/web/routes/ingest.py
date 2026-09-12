"""Taking in a file that was recorded somewhere else. ADR-053."""

from __future__ import annotations

from typing import Any

from ripdoctor.audio import ffprobe, split
from ripdoctor.audio import ingest as G
from ripdoctor.store import files as F
from ripdoctor.store import incoming as IN
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Busy, Job

# What one request carries. Well under the adapter's ceiling, which is the size
# of an object it materialises rather than a policy - see ADR-053. Served to
# the client when an upload begins, so nothing has to agree on it in advance.
CHUNK = 8 * 1024 * 1024

# A ceiling on one upload. A side is a few hundred megabytes; this is room for
# a long one and a bound on a client that has lost its place.
MAX_UPLOAD = 2 * 1024 * 1024 * 1024

SIDE = "a"


def _size(body: dict[str, Any]) -> int:
    try:
        size = int(body["size"])
    except (KeyError, TypeError, ValueError) as e:
        raise H.HttpError(400, "the file's size is needed") from e
    if size <= 0:
        raise H.HttpError(400, "that file is empty")
    if size > MAX_UPLOAD:
        raise H.HttpError(413, f"that file is larger than {MAX_UPLOAD // 2**30} GB")
    return size


def add(app: App, service: Service) -> None:
    layout = service.layout

    @app.route("GET", "/api/ingest/([^/]+)")
    def state(r: H.Request) -> H.Response:
        """What has arrived. The whole of recovering from a closed tab."""
        held = IN.state(layout, slug_of(r))
        return H.ok({"exists": False} if held is None else held.as_dict())

    @app.route("POST", "/api/ingest/([^/]+)/begin")
    def begin(r: H.Request) -> H.Response:
        slug = slug_of(r)
        body = r.json()
        size = _size(body)
        name = str(body.get("name", ""))

        if (layout.raw / slug / f"side-{SIDE}.flac").is_file() and not body.get(
            "replace"
        ):
            raise H.HttpError(409, f"{slug} already has side {SIDE}")

        held = IN.state(layout, slug)
        if held is not None and held.name != name and not body.get("replace"):
            # Two different files under one record. Answering with what is
            # there lets the page offer resume-or-restart instead of guessing.
            raise H.HttpError(
                409,
                f"{held.name} is already part-way here ({held.have} of "
                f"{held.total} bytes) - resume it, or say replace",
            )

        if not IN.room_for(layout, size):
            raise H.HttpError(507, "not enough room in the pool for that file")

        # Named here rather than after the upload: it is the earliest moment
        # the record's name is known, and a reload would otherwise throw it
        # away. The same call a rip makes when it starts.
        if body.get("artist") or body.get("album"):
            F.remember(
                layout,
                slug,
                album=str(body.get("album", "")),
                artist=str(body.get("artist", "")),
                date=str(body.get("date", "")),
            )

        IN.sweep(layout, keep=slug, now=service.now())
        IN.begin(layout, slug, name=name, size=size, now=service.now())
        return H.ok({"slug": slug, "have": 0, "total": size, "chunk": CHUNK})

    @app.route("POST", "/api/ingest/([^/]+)/chunk")
    def chunk(r: H.Request) -> H.Response:
        """The body is the piece itself, like the artwork upload."""
        slug = slug_of(r)
        held = IN.state(layout, slug)
        if held is None:
            raise H.HttpError(409, "no upload is in progress for this record")
        if not r.body:
            raise H.HttpError(400, "no data was sent")
        if len(r.body) > CHUNK:
            raise H.HttpError(413, f"a piece may be at most {CHUNK} bytes")
        try:
            at = int(r.query.get("at", ""))
        except ValueError as e:
            raise H.HttpError(400, "an offset is needed") from e
        if at + len(r.body) > held.total:
            raise H.HttpError(409, "that piece runs past the end of the file")
        try:
            have = IN.append(layout, slug, at, r.body)
        except IN.Mismatch as e:
            raise H.HttpError(409, f"the upload is at {e.have}, not {at}") from e
        return H.ok({"have": have, "total": held.total})

    @app.route("POST", "/api/ingest/([^/]+)/finish")
    def finish(r: H.Request) -> H.Response:
        """Verify, normalise, and put it under a side name in one instant."""
        slug = slug_of(r)
        held = IN.state(layout, slug)
        if held is None:
            raise H.HttpError(409, "no upload is in progress for this record")
        if held.have != held.total:
            raise H.HttpError(
                409, f"only {held.have} of {held.total} bytes arrived - send the rest"
            )
        letter = str(r.json().get("side", SIDE))

        def work(job: Job) -> dict[str, Any]:
            job.total = 3
            part, done = IN.part_file(layout, slug), IN.done_file(layout, slug)

            job.step(held.name, "reading what arrived")
            try:
                found = G.probe(service.runner, str(part))
            except G.NotAudio as e:
                raise ValueError(str(e)) from e
            seconds = ffprobe.true_duration(service.runner, str(part))
            if seconds <= 0:
                raise ValueError("that file has no playable audio in it")
            job.finished = 1

            job.step(held.name, "normalising")
            G.normalise(service.runner, str(part), str(done))
            job.finished = 2

            job.step(held.name, "checking it decodes")
            if not split.verify(service.runner, str(done)):
                done.unlink(missing_ok=True)
                raise ValueError("the encoded side would not decode - nothing placed")
            where = IN.place(layout, slug, letter)
            IN.clear(layout, slug)
            job.finished = 3
            return {
                "slug": slug,
                "side": letter,
                "seconds": round(seconds, 2),
                "bytes": where.stat().st_size,
                "codec": found.codec,
                "rate": found.rate,
            }

        try:
            return H.ok(service.jobs.start(slug, "ingest", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("POST", "/api/ingest/([^/]+)/cancel")
    def cancel(r: H.Request) -> H.Response:
        """Drop it, and the name with it if it turned out to belong to nothing."""
        slug = slug_of(r)
        freed = IN.clear(layout, slug)
        return H.ok(
            {"ok": True, "freed_bytes": freed, "forgot": F.forget(layout, slug)}
        )
