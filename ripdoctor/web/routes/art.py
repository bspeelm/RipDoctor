"""Cover art for a record that is already in the library."""

from __future__ import annotations

from pathlib import Path

from ripdoctor.integrations import artwork as ART
from ripdoctor.integrations import tagger as T
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service

# The same ceiling the request body has. An original from the archive is
# routinely 3000x3000 and several megabytes.
MAX_IMAGE = 32 * 1024 * 1024


def _album(service: Service, slug: str) -> Path:
    where = service.layout.plan_file(slug)
    if not where.is_file():
        raise H.HttpError(404, f"no plan for {slug}")
    plan = F.read_plan(where)
    if not service.settings.library:
        raise H.HttpError(409, "no library directory is configured")
    directory = T.album_dir(service.settings.library, plan.artist, plan.album)
    if not directory.is_dir():
        raise H.HttpError(409, f"{plan.album} is not in the library yet")
    return directory


def add(app: App, service: Service) -> None:
    @app.route("GET", "/api/artwork/([^/]+)")
    def current(r: H.Request) -> H.Response:
        return H.ok(ART.current(service.runner, _album(service, slug_of(r))))

    @app.route("POST", "/api/artwork/([^/]+)/search")
    def search(r: H.Request) -> H.Response:
        """Candidates, each already proved to be an image before it is offered."""
        body = r.json()
        found = ART.candidates(
            service.fetcher,
            service.runner,
            mbid=str(body.get("mbid", "")),
            release_group=str(body.get("release_group", "")),
            page=str(body.get("page", "")),
        )
        return H.ok({"candidates": [c.as_dict() for c in found]})

    @app.route("POST", "/api/artwork/([^/]+)/install")
    def install(r: H.Request) -> H.Response:
        album = _album(service, slug_of(r))
        url = str(r.json().get("url", ""))
        if not url.startswith("https://"):
            raise H.HttpError(400, "an https image address is needed")
        try:
            data = service.fetcher.get(url, {"Accept": "image/*"})
        except (OSError, ValueError) as e:
            raise H.HttpError(502, f"could not fetch that image: {e}") from e
        return _install(service, album, data)

    @app.route("POST", "/api/artwork/([^/]+)/upload")
    def upload(r: H.Request) -> H.Response:
        """The body is the image itself. Same verification as a download."""
        album = _album(service, slug_of(r))
        if not r.body:
            raise H.HttpError(400, "no image was sent")
        return _install(service, album, r.body)


def _install(service: Service, album: Path, data: bytes) -> H.Response:
    if len(data) > MAX_IMAGE:
        raise H.HttpError(413, "that image is too large")
    try:
        done = ART.install(service.runner, album, data)
    except ValueError as e:
        # Refused before anything was touched, which is the whole point.
        raise H.HttpError(400, str(e)) from e
    return H.ok(done.as_dict())
