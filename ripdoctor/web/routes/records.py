"""Reading a record: its sides, their envelopes, the gaps, and saving a plan."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from ripdoctor.audio.align import fit_side
from ripdoctor.audio.ffprobe import true_duration
from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import Envelope
from ripdoctor.core.naming import Unsafe, token
from ripdoctor.core.plan import BadPlan, OldFormat, Plan
from ripdoctor.core.xcorr import AlignError
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
from ripdoctor.store.files import SIDE
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
        """Every record, with the two things that decide what can be done to it.

        Where it lives, because an archived record is finished and a raw one is
        work in progress; and whether its saved plan is the superseded format,
        because that one cannot be edited here and saying so beats a record
        that silently refuses to open.
        """
        include = r.query.get("archive") == "1"
        found = []
        for slug in layout.albums(include_archive=include):
            named = _named(layout, slug)
            found.append(
                {
                    "slug": slug,
                    "where": "raw" if (layout.raw / slug).is_dir() else "archive",
                    "old_format": named is None,
                    # What a record is called, so re-ripping one can fill the
                    # fields in from what was decided last time rather than
                    # from somebody retyping it.
                    "album": "" if named is None else named[0],
                    "artist": "" if named is None else named[1],
                    "date": "" if named is None else named[2],
                }
            )
        return H.ok({"albums": found})

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
                # The stamp changes whenever the source does, so a re-rip is a
                # different URL rather than the same one with stale contents.
                ready[side] = {**built.as_dict(), "v": _version(built.stamp)}

        live = service.recorder.live
        recording = [live.side] if live and live.running and live.slug == slug else []
        spec_file = layout.spec_file(slug)
        out: dict[str, Any] = {
            "slug": slug,
            "sides": sides,
            "ready": ready,
            "recording": recording,
            "mbid": F.read_spec(spec_file).mbid if spec_file.is_file() else "",
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

    @app.route("GET", "/api/job/([^/]+)")
    def job_status(r: H.Request) -> H.Response:
        """Whatever is running for this record, whichever operation it is.

        One endpoint rather than one per operation: there is one job per record
        at a time, so a page that polls has one thing to poll.
        """
        job = service.jobs.status(slug_of(r))
        if job is None:
            raise H.HttpError(404, "no job")
        return H.ok(job.as_dict())

    @app.route("POST", "/api/align/([^/]+)")
    def align(r: H.Request) -> H.Response:
        """Carry an archived cut onto a re-rip of the same record.

        The boundaries were right; the capture was replaced. Rather than fit
        again from the catalogue, the old side is correlated against the new one
        and every boundary is moved by the transform that maps between them.
        """
        slug = slug_of(r)
        where = layout.spec_file(slug)
        if not where.is_file():
            raise H.HttpError(409, f"no saved cut for {slug} to align from")
        spec = F.read_spec(where)

        def work(job: Job) -> dict[str, Any]:
            job.total = len(spec.sides)
            aligned, problems = [], []
            for side in spec.sides:
                job.step(side.letter, "correlating")
                try:
                    aligned.append(_align_side(service, slug, side))
                except (AlignError, FileNotFoundError) as e:
                    # One side that will not align is not a failed record: the
                    # others still carry, and this one is named.
                    problems.append({"side": side.letter, "why": str(e)})
                job.finished += 1
            if not aligned and not problems:
                raise ValueError("none of the sides have been re-ripped yet")
            return {"aligned": aligned, "problems": problems}

        try:
            return H.ok(service.jobs.start(slug, "align", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

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

        The editor sends sides by letter, which is what it has. The filename is
        this layer's business, not the page's.
        """
        slug = slug_of(r)
        body = r.json()
        try:
            plan = _plan_from(slug, body)
        except (OldFormat, BadPlan, KeyError, TypeError, ValueError) as e:
            raise H.HttpError(400, str(e)) from e
        spec = replace(F.spec_of(plan), mbid=str(body.get("mbid", "")))
        spec_path, plan_path = F.save(layout, slug, spec, plan)
        return H.ok({"ok": True, "spec": spec_path.name, "plan": plan_path.name})


def _named(layout: F.Layout, slug: str) -> tuple[str, str, str] | None:
    """What a record is called, or nothing if its plan cannot be read here.

    Refusing the superseded `cuts` format is deliberate - re-fit rather than
    convert - so a listing says which records that applies to rather than
    letting each one fail when somebody opens it.
    """
    where = layout.plan_file(slug)
    if not where.is_file():
        return "", "", ""
    try:
        plan = F.read_plan(where)
    except OldFormat:
        return None
    except (BadPlan, OSError, ValueError):
        return "", "", ""
    return plan.album, plan.artist, plan.date


def _version(stamp: str) -> str:
    return hashlib.sha1(stamp.encode(), usedforsecurity=False).hexdigest()[:10]


def _plan_from(slug: str, doc: dict[str, Any]) -> Plan:
    """A plan from the document the editor holds."""
    return Plan.from_dict(
        {
            "slug": slug,
            "album": str(doc.get("album", "")),
            "artist": str(doc.get("artist", "")),
            "date": str(doc.get("date", "")),
            "sides": [
                {
                    "file": s.get("file") or SIDE.format(letter=token(s["letter"])),
                    "tracks": s.get("tracks", []),
                }
                for s in doc.get("sides", [])
            ],
        }
    )


def _align_side(service: Service, slug: str, side: Any) -> dict[str, Any]:
    """Fit one side of a re-rip and move its boundaries onto it."""
    layout = service.layout
    old = layout.archive / slug / f"side-{side.letter}.flac"
    new = layout.raw / slug / f"side-{side.letter}.flac"
    for where, what in ((old, "archived"), (new, "new")):
        if not where.is_file():
            raise FileNotFoundError(f"no {what} capture at {where.name}")

    built = C.prepared(layout, slug, side.letter)
    duration = built.duration if built else true_duration(service.runner, str(new))
    edges = [t for t in side.tracks if t.start is not None and t.end is not None]
    if not edges:
        raise AlignError(f"side {side.letter} has no boundaries to carry")

    first = min(float(t.start) for t in edges)
    last = max(float(t.end) for t in edges)
    transform, _probes, miss = fit_side(
        service.runner, str(old), str(new), first, last, duration
    )
    tracks = []
    for t in edges:
        start, end = transform.apply(float(t.start)), transform.apply(float(t.end))
        if start < 0 or end > duration or end <= start:
            raise AlignError(
                f"track {t.number} maps to {start:.2f}-{end:.2f}, outside the "
                f"new side (0-{duration:.2f})"
            )
        tracks.append(
            {
                "number": t.number,
                "title": t.title,
                "start": round(start, 2),
                "end": round(end, 2),
                "cat": t.cat,
            }
        )
    return {
        "side": side.letter,
        "offset": round(transform.offset, 3),
        "drift_ms_per_min": round(transform.drift_ms_per_min, 1),
        "check_miss": round(miss, 3),
        "tracks": tracks,
    }


def _require_prepared(layout: F.Layout, slug: str, side: str) -> None:
    if C.prepared(layout, slug, side) is None:
        raise H.HttpError(409, f"side {side} is not prepared")
