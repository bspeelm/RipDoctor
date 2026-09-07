"""Cutting a plan into tracks, and the two ways of listening to the result.

The tick clips are the point of this page. Handing somebody a track and asking
whether it sounds right tells them the track sounds right; it does not tell them
the cut landed three seconds inside a fade. A clip with a tick at the boundary
asks one question instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ripdoctor.audio.split import cut_one, plan_cuts, tick_one, verify
from ripdoctor.core.plan import BadPlan, OldFormat, PlanTrack, validate
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Busy, Job

AUDIO = ".flac"


def _listing(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.suffix.lower() == AUDIO)


def _one_of(directory: Path, index: int) -> Path:
    """The nth file of a listing this server made.

    The client sends a number, never a name, so there is no filename from a
    request anywhere near the filesystem.
    """
    names = _listing(directory)
    if not 0 <= index < len(names):
        raise H.HttpError(404, "no such track")
    return directory / names[index]


def _plan(service: Service, slug: str) -> Any:
    where = service.layout.plan_file(slug)
    if not where.is_file():
        raise H.HttpError(404, f"no plan for {slug}")
    try:
        plan = F.read_plan(where)
        validate(plan)
    except (OldFormat, BadPlan) as e:
        raise H.HttpError(409, str(e)) from e
    return plan


def add(app: App, service: Service) -> None:
    layout = service.layout

    @app.route("POST", "/api/split/([^/]+)")
    def split(r: H.Request) -> H.Response:
        """Cut, then verify. A file that will not decode is not a track."""
        slug = slug_of(r)
        plan = _plan(service, slug)
        source = layout.album_dir(slug)
        dest = layout.review_dir(slug)

        def work(job: Job) -> dict[str, Any]:
            dest.mkdir(parents=True, exist_ok=True)
            cuts = plan_cuts(plan, str(source), str(dest))
            job.total = len(cuts)
            bad = []
            for cut in cuts:
                job.step(Path(cut.dest).name, "cutting")
                cut_one(service.runner, cut)
                if not verify(service.runner, cut.dest):
                    bad.append(Path(cut.dest).name)
                job.finished += 1
            return {"tracks": len(cuts), "unreadable": bad}

        try:
            return H.ok(service.jobs.start(slug, "split", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("GET", "/api/review/([^/]+)")
    def review(r: H.Request) -> H.Response:
        names = _listing(layout.review_dir(slug_of(r)))
        return H.ok({"tracks": [{"index": i, "name": n} for i, n in enumerate(names)]})

    @app.route("GET", "/api/review/([^/]+)/(\\d+)")
    def review_audio(r: H.Request) -> H.Response:
        where = _one_of(layout.review_dir(slug_of(r)), int(r.params[1]))
        return H.file_at(str(where))

    @app.route("POST", "/api/clips/([^/]+)")
    def build_clips(r: H.Request) -> H.Response:
        """One clip per boundary, with a tick at the instant being judged."""
        slug = slug_of(r)
        plan = _plan(service, slug)
        source = layout.album_dir(slug)
        dest = layout.clips_dir(slug)

        def work(job: Job) -> dict[str, Any]:
            dest.mkdir(parents=True, exist_ok=True)
            edges = [
                (side.file, t, edge, at)
                for side in plan.sides
                for t in side.tracks
                for edge, at in (("start", t.start), ("end", t.end))
            ]
            job.total = len(edges)
            for n, (file, track, edge, at) in enumerate(edges, 1):
                job.step(f"track {track.number} {edge}", "building")
                tick_one(
                    service.runner,
                    str(source / file),
                    at,
                    str(dest / _clip_name(n, track, edge)),
                )
                job.finished += 1
            return {"clips": len(edges)}

        try:
            return H.ok(service.jobs.start(slug, "clips", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("GET", "/api/clips/([^/]+)")
    def clips(r: H.Request) -> H.Response:
        names = _listing(layout.clips_dir(slug_of(r)))
        return H.ok({"clips": [{"index": i, "name": n} for i, n in enumerate(names)]})

    @app.route("GET", "/api/clips/([^/]+)/(\\d+)")
    def clip_audio(r: H.Request) -> H.Response:
        where = _one_of(layout.clips_dir(slug_of(r)), int(r.params[1]))
        return H.file_at(str(where))


def _clip_name(n: int, track: PlanTrack, edge: str) -> str:
    return f"{n:02d} track {track.number} {edge}.flac"
