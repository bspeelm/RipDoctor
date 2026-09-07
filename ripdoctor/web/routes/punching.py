"""Re-recording one track, and putting it in place of the old one."""

from __future__ import annotations

from typing import Any

from ripdoctor.core.xcorr import AlignError
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service
from ripdoctor.work import punch as P
from ripdoctor.work.jobs import Busy, Job


def _number(body: dict[str, Any]) -> int:
    try:
        return int(body["number"])
    except (KeyError, TypeError, ValueError) as e:
        raise H.HttpError(400, "a track number is needed") from e


def _plan(service: Service, slug: str):  # type: ignore[no-untyped-def]
    where = service.layout.plan_file(slug)
    if not where.is_file():
        raise H.HttpError(404, f"no plan for {slug}")
    return F.read_plan(where)


def add(app: App, service: Service) -> None:
    layout = service.layout

    @app.route("GET", "/api/punch/([^/]+)")
    def state(r: H.Request) -> H.Response:
        """Every track, or an empty list and the reason there are none.

        Nothing can be punched before a record has been cut, and a record that
        has not been cut is the ordinary state rather than a missing one.
        """
        slug = slug_of(r)
        where = layout.plan_file(slug)
        if not where.is_file():
            return H.ok(
                {
                    "slug": slug,
                    "album": "",
                    "artist": "",
                    "tracks": [],
                    "why": "no saved cut yet - nothing to punch into",
                }
            )
        return H.ok(
            P.state(
                service.runner,
                layout,
                _plan(service, slug),
                service.settings.library,
                slug,
            )
        )

    @app.route("POST", "/api/punch/([^/]+)/locate")
    def locate(r: H.Request) -> H.Response:
        """Fit the archived track's boundaries onto the punch capture.

        Correlating a punch against a side is seconds of work, so it runs as a
        job rather than holding the request open.
        """
        slug = slug_of(r)
        number = _number(r.json())

        def work(_job: Job) -> dict[str, Any]:
            return P.locate(service.runner, layout, slug, number).as_dict()

        try:
            return H.ok(service.jobs.start(slug, "locate", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("GET", "/api/punch/([^/]+)/audio")
    def audio(r: H.Request) -> H.Response:
        slug = slug_of(r)
        try:
            number = int(r.query.get("number", ""))
        except ValueError as e:
            raise H.HttpError(400, "a track number is needed") from e
        where = P.recorded(layout, slug, number)
        if where is None:
            raise H.HttpError(404, f"no punch recorded for track {number}")
        return H.file_at(str(where))

    @app.route("POST", "/api/punch/([^/]+)/apply")
    def apply(r: H.Request) -> H.Response:
        slug = slug_of(r)
        body = r.json()
        number = _number(body)
        try:
            start, end = float(body["start"]), float(body["end"])
        except (KeyError, TypeError, ValueError) as e:
            raise H.HttpError(400, "a start and an end are needed") from e
        if not service.settings.library:
            raise H.HttpError(409, "no library directory is configured")
        try:
            done = P.apply(
                service.runner,
                layout,
                _plan(service, slug),
                service.settings.library,
                slug,
                number,
                start,
                end,
                now=service.now,
            )
        except (P.PunchError, AlignError) as e:
            # Refused before the library was touched, which is the point.
            raise H.HttpError(409, str(e)) from e
        return H.ok(done.as_dict())

    @app.route("POST", "/api/punch/([^/]+)/discard")
    def discard(r: H.Request) -> H.Response:
        slug = slug_of(r)
        number = _number(r.json())
        try:
            size = P.discard(layout, slug, number)
        except P.PunchError as e:
            raise H.HttpError(404, str(e)) from e
        return H.ok({"ok": True, "bytes": size})
