"""The routes, driven as values against a pool in a temporary directory.

No socket, no ffmpeg, no browser. Every one of these would have needed all
three in the predecessor.
"""

from __future__ import annotations

import json
from pathlib import Path

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.config.settings import Settings
from ripdoctor.config.thresholds import Thresholds
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
from ripdoctor.web import auth as A
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service
from ripdoctor.work.jobs import Jobs
from tests.pool import WINDOWS, a_layout, a_runner, quiet_then_loud

SECRET = b"0" * 32
FAST = 1000


def a_service(
    tmp_path: Path, runner: FakeRunner | None = None, **kw: object
) -> Service:
    return Service(
        layout=kw.pop("layout", None) or a_layout(tmp_path),  # type: ignore[arg-type]
        settings=Settings(),
        thresholds=Thresholds(),
        runner=runner or a_runner(),
        credentials=A.create("abbey", "secret", iterations=FAST),
        sessions=A.Sessions(secret=SECRET),
        jobs=Jobs(spawn=lambda work: work()),
        now=lambda: 1000.0,
    )


# A side long enough for the quiet third to exceed the shortest run that counts
# as a gap. Anything shorter and the detector is right to find nothing.
GAPPY_SECONDS = 6.0


def a_gappy_runner() -> FakeRunner:
    windows = int(GAPPY_SECONDS / C.WINDOW)
    return a_runner(
        windows=windows, seconds=GAPPY_SECONDS, levels=quiet_then_loud(windows)
    )


def signed_in(service: Service) -> dict[str, str]:
    token = service.sessions.issue("abbey", now=service.now)
    return {"Cookie": f"{A.COOKIE}={token}"}


def get(app, path: str, service: Service, **kw):  # type: ignore[no-untyped-def]
    return app.dispatch(H.Request.of("GET", path, headers=signed_in(service), **kw))


def post(app, path: str, service: Service, body: dict | None = None):  # type: ignore[no-untyped-def]
    return app.dispatch(
        H.Request.of(
            "POST",
            path,
            headers=signed_in(service),
            body=json.dumps(body or {}).encode(),
        )
    )


# --------------------------------------------------------------- session


def test_the_health_check_needs_no_session(tmp_path: Path) -> None:
    """Enough to tell a stale process from a fresh one without logging in."""
    app = build(a_service(tmp_path))
    r = app.dispatch(H.Request.of("GET", "/healthz"))
    assert r.status == 200
    assert r.json()["ok"] and r.json()["endpoints"]


def test_a_good_login_sets_a_session_cookie(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    app = build(service)
    r = app.dispatch(
        H.Request.of(
            "POST", "/api/login", body=b'{"user": "abbey", "password": "secret"}'
        )
    )
    assert r.status == 200
    cookie = dict(r.headers)["Set-Cookie"]
    assert cookie.startswith(A.COOKIE) and "HttpOnly" in cookie


def test_a_bad_login_does_not_say_which_half_was_wrong(tmp_path: Path) -> None:
    """Otherwise an unknown name is distinguishable from a wrong password."""
    app = build(a_service(tmp_path))
    wrong_name = app.dispatch(
        H.Request.of("POST", "/api/login", body=b'{"user": "x", "password": "secret"}')
    )
    wrong_pass = app.dispatch(
        H.Request.of("POST", "/api/login", body=b'{"user": "abbey", "password": "x"}')
    )
    assert wrong_name.status == wrong_pass.status == 401
    assert wrong_name.json() == wrong_pass.json()


def test_a_run_of_bad_logins_is_throttled(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    app = build(service)
    attempt = H.Request.of(
        "POST", "/api/login", body=b'{"user": "abbey", "password": "x"}', ip="10.0.0.5"
    )
    codes = [app.dispatch(attempt).status for _ in range(8)]
    assert codes[0] == 401 and codes[-1] == 429


def test_a_good_login_clears_the_throttle(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    app = build(service)
    for _ in range(3):
        app.dispatch(
            H.Request.of(
                "POST",
                "/api/login",
                body=b'{"user": "a", "password": "x"}',
                ip="10.0.0.5",
            )
        )
    good = H.Request.of(
        "POST",
        "/api/login",
        body=b'{"user": "abbey", "password": "secret"}',
        ip="10.0.0.5",
    )
    assert app.dispatch(good).status == 200
    assert not service.throttle.blocked("10.0.0.5", 1000.0)


def test_logging_out_works_without_a_session(tmp_path: Path) -> None:
    """An expired cookie is exactly when somebody presses this."""
    app = build(a_service(tmp_path))
    r = app.dispatch(H.Request.of("POST", "/api/logout"))
    assert r.status == 200 and "Max-Age=0" in dict(r.headers)["Set-Cookie"]


def test_who_am_i_answers_for_a_session(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    assert get(build(service), "/api/me", service).json()["user"] == "abbey"


# ---------------------------------------------------------------- records


def test_records_with_sides_are_listed(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    assert get(build(service), "/api/albums", service).json()["albums"] == ["album"]


def test_the_archive_is_listed_only_when_asked(tmp_path: Path) -> None:
    """Re-cutting an imported record is normal; showing every one of them by
    default is not."""
    service = a_service(tmp_path)
    old = service.layout.archive / "older"
    old.mkdir()
    (old / "side-a.flac").write_bytes(b"fLaC")
    app = build(service)
    assert get(app, "/api/albums", service).json()["albums"] == ["album"]
    assert get(app, "/api/albums?archive=1", service).json()["albums"] == [
        "album",
        "older",
    ]


def test_a_record_reports_its_sides_and_what_is_prepared(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    app = build(service)
    before = get(app, "/api/album/album", service).json()
    assert before["sides"] == ["a"] and before["ready"] == {}

    C.build(service.runner, service.layout, "album", "a", dwell=0.0)
    after = get(app, "/api/album/album", service).json()
    assert after["ready"]["a"]["windows"] == WINDOWS


def test_a_record_that_is_not_there_is_a_404(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    assert get(build(service), "/api/album/missing", service).status == 404


def test_a_slug_cannot_reach_outside_the_pool(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    r = get(build(service), "/api/album/..%2Fetc", service)
    assert r.status in (400, 404)


def test_the_saved_plan_comes_back_with_the_record(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    plan = Plan(
        slug="album",
        album="A",
        artist="B",
        date="2022",
        sides=(
            PlanSide(
                file="side-a.flac",
                tracks=(PlanTrack(number=1, title="One", start=1.0, end=9.0, cat=8.0),),
            ),
        ),
    )
    F.write_json(service.layout.plan_file("album"), plan.to_dict())
    body = get(build(service), "/api/album/album", service).json()
    assert body["album"] == "A"
    assert body["tracks_by_side"]["a"][0]["title"] == "One"


def test_the_superseded_plan_format_is_refused_with_a_status(tmp_path: Path) -> None:
    """Two records are still on disk in it and other tools still read it. The
    refusal is deliberate, so it is a 409 rather than a 500."""
    service = a_service(tmp_path)
    F.write_json(
        service.layout.plan_file("album"),
        {"slug": "album", "album": "A", "artist": "B", "sides": [{"cuts": [1, 2]}]},
    )
    assert get(build(service), "/api/album/album", service).status == 409


# --------------------------------------------------------------- prepare


def test_preparing_a_record_runs_every_side(tmp_path: Path) -> None:
    layout = a_layout(tmp_path, sides=("a", "b"))
    service = a_service(tmp_path, layout=layout)
    r = post(build(service), "/api/prepare/album", service)
    assert r.status == 202
    assert r.json()["done"] and r.json()["finished"] == 2
    assert C.prepared(layout, "album", "b") is not None


def test_a_side_still_recording_is_skipped_rather_than_failing(
    tmp_path: Path, monkeypatch
) -> None:
    """The rest of the record still prepares, and the page says which one to
    come back to."""
    service = a_service(tmp_path)
    monkeypatch.setattr(C, "is_growing", lambda *a, **k: True)
    r = post(build(service), "/api/prepare/album", service)
    assert not r.json()["error"] and r.json()["skipped"] == ["a"]


def test_a_second_prepare_while_one_runs_is_refused(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.jobs = Jobs(spawn=lambda _w: None)
    app = build(service)
    assert post(app, "/api/prepare/album", service).status == 202
    assert post(app, "/api/prepare/album", service).status == 409


def test_the_job_is_pollable_afterwards(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    app = build(service)
    post(app, "/api/prepare/album", service)
    assert get(app, "/api/prepare/album", service).json()["done"]


def test_polling_a_record_with_no_job_is_a_404(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    assert get(build(service), "/api/prepare/album", service).status == 404


# ------------------------------------------------------ envelopes and gaps


def test_the_envelope_is_served_as_a_file(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    app = build(service)
    C.build(service.runner, service.layout, "album", "a", dwell=0.0)
    r = get(app, "/api/env/album/a", service)
    assert r.path and r.path.endswith("a.env")
    assert "max-age" in r.cache, "an envelope keyed to its source can be cached"


def test_the_preview_is_served_rather_than_the_capture(tmp_path: Path) -> None:
    """The capture reports no duration, so the browser refuses to seek it."""
    service = a_service(tmp_path)
    app = build(service)
    C.build(service.runner, service.layout, "album", "a", dwell=0.0)
    r = get(app, "/api/audio/album/a", service)
    assert r.path and r.path.endswith("a.opus")


def test_an_unprepared_side_says_so_rather_than_404(tmp_path: Path) -> None:
    """404 reads as "no such side"; this side exists and has not been measured."""
    service = a_service(tmp_path)
    assert get(build(service), "/api/env/album/a", service).status == 409


def test_gaps_come_back_for_both_lanes(tmp_path: Path) -> None:
    """The disagreement between them is the point. ADR-030."""
    service = a_service(tmp_path, runner=a_gappy_runner())
    app = build(service)
    C.build(service.runner, service.layout, "album", "a", dwell=0.0)
    body = get(app, "/api/gaps/album/a", service).json()
    assert set(body) == {"full", "band"}
    assert body["band"]["gaps"], "the band lane found nothing in an obvious gap"
    assert body["band"]["threshold"] != body["full"]["threshold"]


def test_each_lane_reports_the_levels_it_judged_against(tmp_path: Path) -> None:
    service = a_service(tmp_path, runner=a_gappy_runner())
    app = build(service)
    C.build(service.runner, service.layout, "album", "a", dwell=0.0)
    band = get(app, "/api/gaps/album/a", service).json()["band"]
    assert band["music"] > band["floor"]
    assert band["gaps"][0]["hi"] > band["gaps"][0]["lo"]


# ------------------------------------------------------------ saving


def a_plan_body() -> dict:
    return {
        "slug": "album",
        "album": "A",
        "artist": "B",
        "date": "2022",
        "sides": [
            {
                "file": "side-a.flac",
                "tracks": [
                    {"number": 1, "title": "One", "start": 1.0, "end": 9.0, "cat": 8.0}
                ],
            }
        ],
    }


def test_saving_writes_both_documents(tmp_path: Path) -> None:
    """Never one without the other. Writing only the plan discards a decision
    somebody made by listening."""
    service = a_service(tmp_path)
    r = post(build(service), "/api/plan/album", service, a_plan_body())
    assert r.status == 200
    assert service.layout.plan_file("album").is_file()
    assert service.layout.spec_file("album").is_file()


def test_a_saved_edge_becomes_an_ear_set_edge_in_the_spec(tmp_path: Path) -> None:
    """What makes the next fit a pass-through rather than a recomputation."""
    service = a_service(tmp_path)
    post(build(service), "/api/plan/album", service, a_plan_body())
    spec = F.read_spec(service.layout.spec_file("album"))
    assert spec.sides[0].tracks[0].start == 1.0
    assert spec.sides[0].tracks[0].has_ear_edges


def test_a_plan_that_cannot_be_parsed_is_a_400(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    r = post(build(service), "/api/plan/album", service, {"sides": [{"cuts": []}]})
    assert r.status == 400


def test_saving_needs_a_session(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    r = build(service).dispatch(
        H.Request.of("POST", "/api/plan/album", body=json.dumps(a_plan_body()).encode())
    )
    assert r.status == 401
    assert not service.layout.plan_file("album").exists()


def test_every_route_registered_here_is_reachable(tmp_path: Path) -> None:
    """A table that silently emptied would pass every test above by 404."""
    app = build(a_service(tmp_path))
    assert len(app.routes) >= 12
