"""Reading a record: its sides, their envelopes, the gaps, and saving a plan."""

from __future__ import annotations

from typing import Any

from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import Envelope
from ripdoctor.core.naming import Unsafe, token
from ripdoctor.core.plan import BadPlan, OldFormat, Plan, Spec
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Busy, Job

# Long enough to be worth having, short enough that a re-rip is never served
# from it: the URL carries a stamp of the source, so a rebuilt side is a
# different address rather than the same one with stale contents.
CACHEABLE = "private, max-age=3600"


def slug_of(r: H.Request, index: int = 0) -> str:
    try:
        return token(r.params[index])
    except Unsafe as e:
        raise H.HttpError(400, str(e)) from e


def _gapset(env: Envelope, **anchor: float) -> dict[str, Any]:
    found = G.find(env, **anchor)
    return {
        "threshold": round(found.threshold, 1),
        "music": round(found.music, 1),
        "floor": round(found.floor, 1),
        "gaps": [
            {"lo": round(g.lo, 2), "hi": round(g.hi, 2), "mean": round(g.mean, 1)}
            for g in found
        ],
    }


def add(app: App, service: Service) -> None:
    layout = service.layout

    @app.route("GET", "/api/albums")
    def albums(r: H.Request) -> H.Response:
        include = r.query.get("archive") == "1"
        return H.ok({"albums": layout.albums(include_archive=include)})

    @app.route("GET", "/api/album/([^/]+)")
    def album(r: H.Request) -> H.Response:
        slug = slug_of(r)
        try:
            sides = layout.sides_on_disk(slug)
        except FileNotFoundError as e:
            raise H.HttpError(404, str(e)) from e
        if not sides:
            raise H.HttpError(404, f"no side files for {slug}")

        ready = {}
        for side in sides:
            built = C.prepared(layout, slug, side)
            if built:
                ready[side] = built.as_dict()

        out: dict[str, Any] = {
            "slug": slug,
            "sides": sides,
            "ready": ready,
            "album": "",
            "artist": "",
            "date": "",
            "tracks_by_side": {},
        }
        plan_file = layout.plan_file(slug)
        if plan_file.is_file():
            try:
                plan = F.read_plan(plan_file)
            except (OldFormat, BadPlan) as e:
                # Refusing the superseded format is deliberate, so say so with a
                # status the browser can act on rather than a 500.
                raise H.HttpError(409, str(e)) from e
            out.update(album=plan.album, artist=plan.artist, date=plan.date)
            out["tracks_by_side"] = {
                F.letter_of(side.file): [
                    {
                        "number": t.number,
                        "title": t.title,
                        "start": round(t.start, 2),
                        "end": round(t.end, 2),
                        "cat": t.cat,
                    }
                    for t in side.tracks
                ]
                for side in plan.sides
            }
        return H.ok(out)

    @app.route("POST", "/api/prepare/([^/]+)")
    def prepare(r: H.Request) -> H.Response:
        slug = slug_of(r)
        sides = layout.sides_on_disk(slug)
        if not sides:
            raise H.HttpError(404, "no side files")

        def work(job: Job) -> dict[str, Any]:
            job.total = len(sides)
            for side in sides:
                job.step(side, "starting")
                try:
                    C.build(
                        service.runner,
                        layout,
                        slug,
                        side,
                        progress=lambda d, s=side: job.step(s, d),  # type: ignore[misc]
                    )
                except C.StillRecording:
                    # Not a failure. The rest of the record still prepares, and
                    # this side is one to come back to.
                    job.skipped.append(side)
                job.finished += 1
            return {"sides": sides, "skipped": job.skipped}

        try:
            return H.ok(service.jobs.start(slug, "prepare", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("GET", "/api/prepare/([^/]+)")
    def prepare_status(r: H.Request) -> H.Response:
        job = service.jobs.status(slug_of(r))
        if job is None:
            raise H.HttpError(404, "no job")
        return H.ok(job.as_dict())

    @app.route("GET", "/api/env/([^/]+)/([^/]+)")
    def envelope(r: H.Request) -> H.Response:
        slug, side = slug_of(r), slug_of(r, 1)
        _require_prepared(layout, slug, side)
        return H.file_at(str(C.envelope_path(layout, slug, side)), cache=CACHEABLE)

    @app.route("GET", "/api/audio/([^/]+)/([^/]+)")
    def audio(r: H.Request) -> H.Response:
        slug, side = slug_of(r), slug_of(r, 1)
        _require_prepared(layout, slug, side)
        return H.file_at(str(C.preview_path(layout, slug, side)), cache=CACHEABLE)

    @app.route("GET", "/api/gaps/([^/]+)/([^/]+)")
    def gaps(r: H.Request) -> H.Response:
        """Both lanes, because on a quiet pressing they disagree.

        The disagreement is the point: full band a real gap and a quiet passage
        sit together, and in 1-3 kHz they are twenty decibels apart. Each lane
        gets its own anchor - the full one down from the music, the band one up
        from its floor - and reversing them inverts detection. ADR-030.
        """
        slug, side = slug_of(r), slug_of(r, 1)
        _require_prepared(layout, slug, side)
        lanes = C.lanes_of(layout, slug, side)
        return H.ok(
            {
                "full": _gapset(lanes.full, below=service.thresholds.gap_below),
                "band": _gapset(lanes.band, above=service.thresholds.gap_above),
            }
        )

    @app.route("POST", "/api/plan/([^/]+)")
    def save(r: H.Request) -> H.Response:
        """Write the plan and the spec together, never one without the other.

        The plan is what the cutter reads; the spec is where the same edges
        become ear overrides that win on the next fit. Writing only the plan
        discards a decision somebody made by listening.
        """
        slug = slug_of(r)
        body = r.json()
        try:
            plan = Plan.from_dict(body.get("plan") or body)
            spec = Spec.from_dict(body["spec"]) if body.get("spec") else _spec_for(plan)
        except (OldFormat, BadPlan, KeyError, TypeError, ValueError) as e:
            raise H.HttpError(400, str(e)) from e
        spec_path, plan_path = F.save(layout, slug, spec, plan)
        return H.ok({"ok": True, "spec": spec_path.name, "plan": plan_path.name})


def _spec_for(plan: Plan) -> Spec:
    """The spec this plan implies. Nothing else is sent by the editor."""
    return F.spec_of(plan)


def _require_prepared(layout: F.Layout, slug: str, side: str) -> None:
    if C.prepared(layout, slug, side) is None:
        raise H.HttpError(409, f"side {side} is not prepared")
