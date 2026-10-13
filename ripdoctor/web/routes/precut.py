"""An album that was bought already cut. ADR-062."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ripdoctor.integrations import importer as IMP
from ripdoctor.store import incoming as IN
from ripdoctor.store import unpack as U
from ripdoctor.store.safety import under
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Busy, Job

CHUNK = 8 * 1024 * 1024
MAX_UPLOAD = 2 * 1024 * 1024 * 1024

# The upload is the album, so there is no identifier to invent and nothing a
# restart can strand. One at a time, which is how albums are bought. ADR-062.
HELD = "precut"
WHERE = "precut"


def _dir(service: Service) -> Path:
    return under(service.layout.work, WHERE)


def _beets(service: Service) -> IMP.Beets:
    """Matching a release is beets' work. The fallback tagger writes the tags
    it is given and has no matcher, so there is nothing for it to do here."""
    importer = IMP.choose(service.runner, service.settings.importer, service.state_dir)
    if not isinstance(importer, IMP.Beets):
        raise H.HttpError(
            409, "this needs beets - the fallback tagger cannot match a release"
        )
    return importer


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

    @app.route("GET", "/api/precut")
    def state(_r: H.Request) -> H.Response:
        held = IN.state(layout, HELD)
        into = _dir(service)
        staged = sorted(p.name for p in into.glob("*")) if into.is_dir() else []
        return H.ok(
            {
                "upload": None if held is None else held.as_dict(),
                "staged": staged,
                "chunk": CHUNK,
            }
        )

    @app.route("POST", "/api/precut/begin")
    def begin(r: H.Request) -> H.Response:
        body = r.json()
        size = _size(body)
        name = str(body.get("name", ""))
        held = IN.state(layout, HELD)
        if held is not None and held.name != name and not body.get("replace"):
            raise H.HttpError(
                409,
                f"{held.name} is already part-way here ({held.have} of "
                f"{held.total} bytes) - resume it, or say replace",
            )
        if not IN.room_for(layout, size):
            raise H.HttpError(507, "not enough room in the pool for that file")
        IN.begin(layout, HELD, name=name, size=size, now=service.now())
        return H.ok({"have": 0, "total": size, "chunk": CHUNK})

    @app.route("POST", "/api/precut/chunk")
    def chunk(r: H.Request) -> H.Response:
        held = IN.state(layout, HELD)
        if held is None:
            raise H.HttpError(409, "no upload is in progress")
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
            have = IN.append(layout, HELD, at, r.body)
        except IN.Mismatch as e:
            raise H.HttpError(409, f"the upload is at {e.have}, not {at}") from e
        return H.ok({"have": have, "total": held.total})

    @app.route("POST", "/api/precut/finish")
    def finish(_r: H.Request) -> H.Response:
        """Unpack it and ask beets what it is, without letting beets move
        anything. Nothing is decided here."""
        held = IN.state(layout, HELD)
        if held is None:
            raise H.HttpError(409, "no upload is in progress")
        if held.have != held.total:
            raise H.HttpError(
                409, f"only {held.have} of {held.total} bytes arrived - send the rest"
            )

        importer = _beets(service)

        def work(job: Job) -> dict[str, Any]:
            job.total = 2
            job.step(held.name, "unpacking")
            into = _dir(service)
            shutil.rmtree(into, ignore_errors=True)
            try:
                album = U.unpack(IN.part_file(layout, HELD), into)
            except U.NotAnAlbum as e:
                raise ValueError(str(e)) from e
            job.finished = 1

            job.step(held.name, "asking beets what it is")
            mbid, output = importer.propose(service.runner, str(into))
            job.finished = 2
            return {
                "tracks": list(album.tracks),
                "art": list(album.art),
                "mbid": mbid,
                "report": output[-8000:],
            }

        try:
            return H.ok(service.jobs.start(HELD, "precut", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("POST", "/api/precut/take")
    def take(r: H.Request) -> H.Response:
        """Commit the release that was shown, and nothing else."""
        body = r.json()
        mbid = str(body.get("mbid", ""))
        if not mbid:
            raise H.HttpError(400, "the release that was shown is needed")
        root = service.settings.library
        if not root:
            raise H.HttpError(409, "no library directory is configured")
        importer = _beets(service)
        into = _dir(service)
        tracks = tuple(sorted(p.name for p in into.glob("*") if p.is_file()))
        if not any(Path(n).suffix.lower() in U.AUDIO for n in tracks):
            raise H.HttpError(409, "nothing is unpacked - upload an album first")

        held = IN.state(layout, HELD)
        name = held.name if held else "the album"

        def work(job: Job) -> dict[str, Any]:
            audio = tuple(n for n in tracks if Path(n).suffix.lower() in U.AUDIO)
            job.total = len(audio)
            job.step(name, f"importing with {importer.name}")
            done = importer.take(
                service.runner,
                str(into),
                root,
                mbid=mbid,
                tracks=audio,
                duplicates=str(body.get("duplicates", "replace")),
                file_mode=service.settings.file_mode,
                dir_mode=service.settings.dir_mode,
            )
            job.finished = done.tracks
            # Only now: nothing is cleared until the record is proved to be
            # somewhere else. ADR-062.
            if done.tracks:
                shutil.rmtree(into, ignore_errors=True)
                IN.clear(layout, HELD)
            return dict(done.as_dict())

        try:
            return H.ok(service.jobs.start(HELD, "precut-import", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("POST", "/api/precut/cancel")
    def cancel(_r: H.Request) -> H.Response:
        freed = IN.clear(layout, HELD)
        shutil.rmtree(_dir(service), ignore_errors=True)
        return H.ok({"ok": True, "freed_bytes": freed})
