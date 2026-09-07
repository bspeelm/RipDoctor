"""Routing and who is allowed to reach what.

Every one of these would have needed a live socket in the predecessor, which is
why none of them existed.
"""

from __future__ import annotations

import pytest

from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.auth import COOKIE, Sessions

SECRET = b"0" * 32


def an_app(*, static: bool = False) -> App:
    app = App(
        Sessions(secret=SECRET),
        static=(lambda r: H.Response(body=b"<html>", content_type="text/html"))
        if static
        else None,
        now=lambda: 1000.0,
    )

    @app.route("GET", "/healthz", needs_auth=False)
    def health(_r: H.Request) -> H.Response:
        return H.ok({"ok": True})

    @app.route("POST", "/api/login", needs_auth=False)
    def login(_r: H.Request) -> H.Response:
        return H.ok({"ok": True})

    @app.route("GET", "/api/album/([^/]+)")
    def album(r: H.Request) -> H.Response:
        return H.ok({"slug": r.params[0], "user": r.user})

    @app.route("POST", "/api/boom")
    def boom(_r: H.Request) -> H.Response:
        raise ZeroDivisionError("something the author did not think of")

    return app


def signed_in(app: App, headers: dict[str, str] | None = None) -> dict[str, str]:
    token = app.sessions.issue("abbey", now=lambda: 1000.0)
    return {**(headers or {}), "Cookie": f"{COOKIE}={token}"}


# ---------------------------------------------------------------- routing


def test_a_route_answers() -> None:
    r = an_app().dispatch(H.Request.of("GET", "/healthz"))
    assert r.status == 200 and r.json() == {"ok": True}


def test_the_captured_part_of_the_path_reaches_the_handler() -> None:
    app = an_app()
    r = app.dispatch(
        H.Request.of("GET", "/api/album/hell-on-church-street", headers=signed_in(app))
    )
    assert r.json()["slug"] == "hell-on-church-street"


def test_a_path_that_matches_nothing_is_a_404() -> None:
    assert an_app().dispatch(H.Request.of("GET", "/api/nope")).status == 404


def test_the_right_path_with_the_wrong_method_is_a_404() -> None:
    assert an_app().dispatch(H.Request.of("POST", "/healthz")).status == 404


def test_a_pattern_matches_the_whole_path_not_a_prefix() -> None:
    """Otherwise /api/album/x/../../etc reaches the album route."""
    app = an_app()
    r = app.dispatch(H.Request.of("GET", "/healthz/extra", headers=signed_in(app)))
    assert r.status == 404


def test_head_is_answered_by_the_get_route() -> None:
    """A HEAD that 404s where GET succeeds breaks every client that checks
    before fetching."""
    assert an_app().dispatch(H.Request.of("HEAD", "/healthz")).status == 200


# ------------------------------------------------------------------ auth


def test_a_route_that_needs_a_session_refuses_without_one() -> None:
    r = an_app().dispatch(H.Request.of("GET", "/api/album/x"))
    assert r.status == 401 and "authentication" in r.json()["error"]


def test_a_valid_session_names_the_user_to_the_handler() -> None:
    app = an_app()
    r = app.dispatch(H.Request.of("GET", "/api/album/x", headers=signed_in(app)))
    assert r.json()["user"] == "abbey"


def test_an_expired_session_is_no_session() -> None:
    app = App(Sessions(secret=SECRET, ttl=60.0), now=lambda: 2000.0)

    @app.route("GET", "/api/thing")
    def thing(_r: H.Request) -> H.Response:
        return H.ok({})

    token = app.sessions.issue("abbey", now=lambda: 1000.0)
    r = app.dispatch(
        H.Request.of("GET", "/api/thing", headers={"Cookie": f"{COOKIE}={token}"})
    )
    assert r.status == 401


def test_a_session_from_elsewhere_is_refused() -> None:
    app = an_app()
    other = Sessions(secret=b"1" * 32).issue("root")
    r = app.dispatch(
        H.Request.of("GET", "/api/album/x", headers={"Cookie": f"{COOKIE}={other}"})
    )
    assert r.status == 401


def test_every_api_route_but_login_needs_a_session() -> None:
    """The security posture, as a test rather than a paragraph.

    A route added without thinking about who may call it defaults to needing
    one; this catches the case where somebody turns that off.
    """
    open_api = [
        r.pattern.pattern
        for r in an_app().routes
        if r.pattern.pattern.startswith("^/api/") and not r.needs_auth
    ]
    assert open_api == ["^/api/login$"], f"reachable without a session: {open_api}"


def test_at_least_a_few_routes_were_examined() -> None:
    """A table that silently emptied would pass the test above."""
    assert len(an_app().routes) >= 4


# ------------------------------------------------------------- failures


def test_an_unexpected_error_travels_rather_than_being_swallowed() -> None:
    """The adapter owns the log, and an error nobody logged is one nobody
    fixes. Catching it here would turn a defect into a quiet 500."""
    app = an_app()
    with pytest.raises(ZeroDivisionError):
        app.dispatch(H.Request.of("POST", "/api/boom", headers=signed_in(app)))


def test_a_refusal_carries_its_status_and_reason() -> None:
    app = an_app()

    @app.route("GET", "/api/gone")
    def gone(_r: H.Request) -> H.Response:
        raise H.HttpError(410, "that album was archived")

    r = app.dispatch(H.Request.of("GET", "/api/gone", headers=signed_in(app)))
    assert r.status == 410 and r.json()["error"] == "that album was archived"


# -------------------------------------------------------------- static


def test_an_unmatched_page_falls_through_to_the_front_end() -> None:
    r = an_app(static=True).dispatch(H.Request.of("GET", "/index.html"))
    assert r.status == 200 and r.content_type == "text/html"


def test_an_unmatched_api_path_never_falls_through() -> None:
    """A mistyped route would otherwise return the front page with a 200, and
    the browser would report a JSON parse error a long way from the cause."""
    r = an_app(static=True).dispatch(H.Request.of("GET", "/api/mistyped"))
    assert r.status == 404


def test_the_endpoints_are_listable() -> None:
    """`/healthz` reports them, which is how a stale process is spotted."""
    assert "^/healthz$" in an_app().endpoints
