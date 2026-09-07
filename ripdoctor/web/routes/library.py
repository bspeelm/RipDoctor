"""The catalogue, the first pass, and getting a record into the library."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ripdoctor.core.fit import fit_plan, report
from ripdoctor.core.plan import BadPlan, Plan, Spec, SpecSide, SpecTrack, validate
from ripdoctor.core.sides import assign_sides, music_span
from ripdoctor.integrations import importer as IMP
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import archive as AR
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Busy, Job

VINYL = ("vinyl", '12" vinyl', '10" vinyl', '7" vinyl')


def _release(r: MB.Release) -> dict[str, Any]:
    return {
        "id": r.mbid,
        "title": r.title,
        "artist": r.artist,
        "date": r.date,
        "country": r.country,
        "disambiguation": r.disambiguation,
        "formats": [r.format] if r.format else [],
        "tracks": len(r.tracks),
        # How many of them have a length, which is what decides whether this
        # entry can be fitted at all.
        "durations": sum(1 for t in r.tracks if t.length),
        "total": round(r.total_seconds),
        "vinyl": r.format.lower() in VINYL,
        "has_durations": r.has_durations,
        "describe": r.describe(),
    }


def spec_from(slug: str, release: MB.Release, sides: list[tuple[str, float, float]]):  # type: ignore[no-untyped-def]
    """Lay a tracklist across the sides that were actually recorded.

    The split is exhaustive rather than greedy: putting one track on the wrong
    side displaces every side after it, so the arrangement is chosen whole.
    """
    lengths = [t.length or 0.0 for t in release.tracks]
    runs = assign_sides([hi - lo for _letter, lo, hi in sides], lengths)
    out = []
    for (letter, lo, hi), (first, last) in zip(sides, runs, strict=True):
        out.append(
            SpecSide(
                letter=letter,
                start=lo,
                end=hi,
                tracks=tuple(
                    SpecTrack(number=t.number, title=t.title, cat=t.length or 0.0)
                    for t in release.tracks[first:last]
                ),
            )
        )
    return Spec(
        slug=slug,
        album=release.title,
        artist=release.artist,
        date=release.date,
        mbid=release.mbid,
        sides=tuple(out),
    )


def add(app: App, service: Service) -> None:
    layout = service.layout

    @app.route("GET", "/api/mb/search")
    def search(r: H.Request) -> H.Response:
        artist, album = r.query.get("artist", ""), r.query.get("album", "")
        if not artist or not album:
            raise H.HttpError(400, "an artist and an album are needed")
        try:
            found = MB.search(service.fetcher, artist, album)
            for release in found:
                MB.fetch_tracks(service.fetcher, release)
        except MB.LookupFailed as e:
            raise H.HttpError(503, str(e)) from e
        # Usable entries first, whatever the format: a vinyl entry with no
        # durations at all fits worse than a CD entry that has them.
        return H.ok({"releases": [_release(x) for x in MB.rank(found)]})

    @app.route("POST", "/api/firstpass/([^/]+)")
    def firstpass(r: H.Request) -> H.Response:
        """Build a spec from a chosen release and fit it against the sides."""
        slug = slug_of(r)
        mbid = str(r.json().get("mbid", ""))
        if not mbid:
            raise H.HttpError(400, "a release is needed; search first")
        letters = layout.sides_on_disk(slug)
        if not letters:
            raise H.HttpError(404, f"no side files for {slug}")

        def work(job: Job) -> dict[str, Any]:
            job.total = len(letters) + 1
            job.step("catalogue", "fetching the tracklist")
            release = MB.fetch_tracks(
                service.fetcher, MB.Release(mbid, "", "", "", "", [])
            )
            if not release.has_durations:
                # Fitting against zeros produces a plausible-looking plan that
                # is wrong everywhere.
                raise ValueError("that release has no track durations")
            job.finished += 1

            spans, lanes = [], {}
            for letter in letters:
                job.step(letter, "reading the envelope")
                built = C.prepared(layout, slug, letter)
                if built is None:
                    raise ValueError(f"side {letter} is not prepared")
                band = C.lanes_of(layout, slug, letter).band
                lo, hi = music_span(band)
                spans.append((letter, lo, hi))
                lanes[letter] = band
                job.finished += 1

            spec = spec_from(slug, release, spans)
            plan, working = fit_plan(spec, lanes, above=service.thresholds.gap_above)
            validate(plan)
            F.save(layout, slug, spec, plan)
            return {
                "album": plan.album,
                "artist": plan.artist,
                "tracks": sum(len(s.tracks) for s in plan.sides),
                "report": "\n".join(
                    f"=== side {letter}\n{report(fitted)}"
                    for letter, fitted in working.items()
                ),
            }

        try:
            return H.ok(service.jobs.start(slug, "firstpass", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("POST", "/api/relabel/([^/]+)")
    def relabel(r: H.Request) -> H.Response:
        """Take the titles from a different release. Move no boundary.

        A release picked at import time is usually picked because the one from
        the first pass was wrong - most often a CD edition against an LP that
        carries extra tracks. Re-fitting is the wrong answer by then: the
        boundaries are already correct and hard-won.

        It refuses when the counts differ rather than shifting every title by
        one, which is the mistake this exists to make impossible.
        """
        slug = slug_of(r)
        mbid = str(r.json().get("mbid", ""))
        if not mbid:
            raise H.HttpError(400, "a release is needed")
        spec = F.read_spec(_file(layout.spec_file(slug), slug))
        plan = F.read_plan(_file(layout.plan_file(slug), slug))
        # A spec with no sides in it is a record that has only been named. The
        # retitling walks its sides, so this would write a retitled plan beside
        # a spec that kept none of it, and the two would stay that way.
        if not spec.sides:
            raise H.HttpError(409, f"no saved cut for {slug} to re-label")
        try:
            release = MB.fetch_tracks(
                service.fetcher, MB.Release(mbid, "", "", "", "", [])
            )
        except MB.LookupFailed as e:
            raise H.HttpError(503, str(e)) from e

        mine = sum(len(s.tracks) for s in plan.sides)
        if len(release.tracks) != mine:
            raise H.HttpError(
                409,
                f"that release has {len(release.tracks)} tracks and this cut has "
                f"{mine} - re-labelling would shift every title",
            )
        titles = [t.title for t in release.tracks]
        F.save(
            layout,
            slug,
            _retitled_spec(spec, titles, release, mbid),
            _retitled_plan(plan, titles, release),
        )
        return H.ok(
            {"ok": True, "applied": mine, "release": release.title, "mbid": mbid}
        )

    @app.route("GET", "/api/library/existing/([^/]+)")
    def existing(r: H.Request) -> H.Response:
        """Whether this record is already in the library, and how much of it.

        Asked before an import rather than after, because finding out after is
        finding out from a directory holding two copies.
        """
        slug = slug_of(r)
        plan = _plan_of(service, slug)
        importer = IMP.choose(
            service.runner, service.settings.importer, service.state_dir
        )
        where, count = importer.locate(
            service.runner, service.settings.library, plan.artist, plan.album
        )
        return H.ok(
            {
                "existing": None
                if where is None
                else {"where": str(where), "tracks": count},
            }
        )

    @app.route("GET", "/api/library")
    def library(_r: H.Request) -> H.Response:
        root = service.settings.library
        return H.ok({"library": root, "exists": bool(root) and _isdir(root)})

    @app.route("POST", "/api/import/([^/]+)")
    def import_record(r: H.Request) -> H.Response:
        slug = slug_of(r)
        plan = _plan_of(service, slug)
        root = service.settings.library
        if not root:
            raise H.HttpError(409, "no library directory is configured")

        importer = IMP.choose(
            service.runner, service.settings.importer, service.state_dir
        )
        spec_file = layout.spec_file(slug)
        mbid = F.read_spec(spec_file).mbid if spec_file.is_file() else ""

        def work(job: Job) -> dict[str, Any]:
            review = str(layout.review_dir(slug))
            job.total = sum(len(s.tracks) for s in plan.sides)
            job.step(plan.album, f"importing with {importer.name}")
            done = importer.apply(
                service.runner,
                plan,
                review,
                root,
                mbid=mbid,
                file_mode=service.settings.file_mode,
                dir_mode=service.settings.dir_mode,
            )
            job.finished = done.tracks
            return {**done.as_dict(), "importer": importer.name}

        try:
            return H.ok(service.jobs.start(slug, "import", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    def _survey(r: H.Request, read: bool) -> dict[str, Any]:
        slug = slug_of(r)
        plan = _maybe_plan(service, slug)
        if plan is None:
            # A record that has been captured and not yet cut is the ordinary
            # state, not a missing one. The gate's answer is "no, and here is
            # why" rather than a 404 the page has to swallow.
            return {
                "ready": False,
                "why": "no saved cut yet - run a first pass",
                "library_path": None,
                "library_tracks": 0,
                "expected": 0,
                "will_archive_to": str(layout.archive / slug),
                "sides": [],
                "will_remove": [],
            }
        return AR.survey(
            service.runner,
            layout,
            IMP.choose(service.runner, service.settings.importer, service.state_dir),
            service.settings.library,
            slug,
            plan.artist,
            plan.album,
            sum(len(s.tracks) for s in plan.sides),
            read=read,
        )

    @app.route("GET", "/api/archive/([^/]+)")
    def archive_gate(r: H.Request) -> H.Response:
        """Whether the raw sides are safe to clear, and why not if they are not.

        The gate is the whole point: it is what stops twenty minutes a side
        being cleared before the record is provably somewhere else. `verify=1`
        reads every side back, which takes seconds each - worth it before the
        irreversible step, not worth it for a button's tooltip.
        """
        return H.ok(_survey(r, read=r.query.get("verify") == "1"))

    @app.route("POST", "/api/archive/([^/]+)")
    def archive(r: H.Request) -> H.Response:
        slug = slug_of(r)
        state = _survey(r, read=False)
        if not state["ready"]:
            raise H.HttpError(409, state["why"])

        def work(job: Job) -> dict[str, Any]:
            job.total = len(state["sides"])

            def step(name: str, what: str) -> None:
                job.step(name, what)
                job.finished += 1

            return AR.put_away(service.runner, layout, slug, progress=step)

        try:
            return H.ok(service.jobs.start(slug, "archive", work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e


def _file(where: Path, slug: str) -> Path:
    if not where.is_file():
        raise H.HttpError(404, f"no saved cut for {slug}")
    return where


def _retitled_spec(
    spec: Spec, titles: list[str], release: MB.Release, mbid: str
) -> Spec:
    n = 0
    sides = []
    for side in spec.sides:
        tracks = []
        for t in side.tracks:
            tracks.append(replace(t, title=titles[n]))
            n += 1
        sides.append(replace(side, tracks=tuple(tracks)))
    return replace(
        spec,
        sides=tuple(sides),
        mbid=mbid,
        album=release.title or spec.album,
        artist=release.artist or spec.artist,
        date=release.date or spec.date,
    )


def _retitled_plan(plan: Plan, titles: list[str], release: MB.Release) -> Plan:
    n = 0
    sides = []
    for side in plan.sides:
        tracks = []
        for t in side.tracks:
            tracks.append(replace(t, title=titles[n]))
            n += 1
        sides.append(replace(side, tracks=tuple(tracks)))
    return replace(
        plan,
        sides=tuple(sides),
        album=release.title or plan.album,
        artist=release.artist or plan.artist,
        date=release.date or plan.date,
    )


def _isdir(path: str) -> bool:
    return Path(path).is_dir()


def _maybe_plan(service: Service, slug: str):  # type: ignore[no-untyped-def]
    """The saved cut, or nothing when a record has not been cut yet."""
    if not service.layout.plan_file(slug).is_file():
        return None
    return _plan_of(service, slug)


def _plan_of(service: Service, slug: str):  # type: ignore[no-untyped-def]
    where = service.layout.plan_file(slug)
    if not where.is_file():
        raise H.HttpError(404, f"no plan for {slug}")
    try:
        plan = F.read_plan(where)
        validate(plan)
    except BadPlan as e:
        raise H.HttpError(409, str(e)) from e
    return plan
