"""Logging in, logging out, and saying who you are."""

from __future__ import annotations

from ripdoctor import __version__
from ripdoctor.web import auth as A
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.service import Service


def add(app: App, service: Service) -> None:
    @app.route("GET", "/healthz", needs_auth=False)
    def health(_r: H.Request) -> H.Response:
        # The version and the route list together: enough to tell a stale
        # process from a fresh one without logging in.
        return H.ok({"ok": True, "version": __version__, "endpoints": app.endpoints})

    @app.route("POST", "/api/login", needs_auth=False)
    def login(r: H.Request) -> H.Response:
        now = service.now()
        if service.throttle.blocked(r.ip, now):
            raise H.HttpError(429, "too many attempts; wait a minute")
        body = r.json()
        user = str(body.get("user", ""))
        if not service.credentials.verify(user, str(body.get("password", ""))):
            service.throttle.failed(r.ip, now)
            # One message for both halves. Saying which was wrong tells an
            # unknown name apart from a wrong password.
            raise H.HttpError(401, "bad username or password")
        service.throttle.clear(r.ip)
        token = service.sessions.issue(user, now=service.now)
        return H.ok({"ok": True, "user": user}).with_header(
            "Set-Cookie", A.cookie(token, service.sessions.ttl)
        )

    @app.route("POST", "/api/logout", needs_auth=False)
    def logout(_r: H.Request) -> H.Response:
        # Reachable without a session on purpose: an expired cookie is exactly
        # when somebody presses this, and refusing leaves it in the browser.
        return H.ok({"ok": True}).with_header("Set-Cookie", A.cleared_cookie())

    @app.route("GET", "/api/me")
    def me(r: H.Request) -> H.Response:
        return H.ok({"user": r.user})
