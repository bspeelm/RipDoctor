"""Cover art for a record that is already in the library."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from ripdoctor.integrations import artwork as ART
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.integrations import tagger as T
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import named, slug_of
from ripdoctor.web.service import Service

# The same ceiling the request body has. An original from the archive is
# routinely 3000x3000 and several megabytes.
MAX_IMAGE = 32 * 1024 * 1024


def _album(service: Service, slug: str) -> Path:
    """Where the tracks this art belongs to are: review, then the library."""
    where = service.layout.plan_file(slug)
    if not where.is_file():
        raise H.HttpError(404, f"no plan for {slug}")
    # The decided names, not one document's. A library path built from whichever
    # of the two happened to be read points at Unknown Artist the moment that
    # one is blank, and the record is then reported as not being in a library it
    # is plainly in.
    # Review first, where the cuts are until an import moves them: beets takes
    # a cover from the folder it imports, so art put there travels in. ADR-042.
    cut = service.layout.review_dir(slug)
    if cut.is_dir() and any(cut.glob("*.flac")):
        return cut
    album, artist, _date, _old = named(service.layout, slug)
    if service.settings.library:
        filed = T.album_dir(service.settings.library, artist, album)
        if filed.is_dir():
            return filed
    raise H.HttpError(409, f"{album or slug} has no cut tracks and is not filed yet")


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

    @app.route("POST", "/api/artwork/([^/]+)/fetch")
    def fetch(r: H.Request) -> H.Response:
        """Find the best cover for this record and install it, in one step.

        The release this was fitted against is in the spec, which is the only
        reason the catalogue can be asked again later. Candidates below the
        size floor are not installed: a small cover is worse than the one a
        player already shows for a record with none.
        """
        slug = slug_of(r)
        album = _album(service, slug)
        spec_file = service.layout.spec_file(slug)
        mbid = F.read_spec(spec_file).mbid if spec_file.is_file() else ""
        page = str(r.json().get("page", ""))
        if not mbid and not page:
            raise H.HttpError(
                409,
                "no release is recorded for this record - run a first pass, or "
                "give a page to take the cover from",
            )
        # A pressing often has no cover of its own while the album plainly does:
        # this is a vinyl tool, and a twelve-inch is exactly the release the
        # archive is least likely to hold a scan for. The group is where the
        # rest of the editions keep theirs, so it is asked for as well.
        group = ""
        if mbid:
            with suppress(MB.LookupFailed, OSError, ValueError):
                group = MB.release_group(service.fetcher, mbid)
        found = ART.candidates(
            service.fetcher,
            service.runner,
            mbid=mbid,
            release_group=group,
            page=page,
        )
        usable = [
            c for c in found if c.ok and c.image is not None and c.image.big_enough
        ]
        if not usable:
            # Naming the sizes matters: "too small" for a cover that missed the
            # floor by twenty pixels reads as a fault rather than as a judgement
            # somebody can overrule with a better scan of their own.
            why = "; ".join(
                f"{c.source}: {c.why or f'{c.image} is under {ART.MIN_EDGE}px'}"
                for c in found
            )
            raise H.HttpError(
                404, f"nothing usable was found - {why}. Upload an image instead."
            )
        best = max(usable, key=lambda c: c.image.edge if c.image else 0)
        data = service.fetcher.get(best.url, {"Accept": "image/*"})
        installed = _install(service, album, data)
        return H.ok({**installed.json(), "source": best.source})

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
