"""The route table, and dispatch. No socket, no threads, no files.

A route is a function from a Request to a Response, so the whole of the routing
- including who is allowed to reach what - is exercised the way the core is.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from ripdoctor.web import http as H
from ripdoctor.web.auth import COOKIE, Sessions

Handler = Callable[[H.Request], H.Response]

# Anything under this prefix answers with JSON and never falls through to a
# file, so a mistyped route cannot quietly return the front page with a 200.
API = "/api/"


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    needs_auth: bool = True


class App:
    """A table of routes and the rules for reaching them."""

    def __init__(
        self,
        sessions: Sessions,
        *,
        static: Handler | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.routes: list[Route] = []
        self.sessions = sessions
        self.static = static
        self.now = now

    def route(
        self, method: str, pattern: str, *, needs_auth: bool = True
    ) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.routes.append(
                Route(method.upper(), re.compile(f"^{pattern}$"), fn, needs_auth)
            )
            return fn

        return register

    @property
    def endpoints(self) -> list[str]:
        return sorted({r.pattern.pattern for r in self.routes})

    def user_of(self, request: H.Request) -> str | None:
        return self.sessions.check(request.cookie(COOKIE), now=self.now)

    def dispatch(self, request: H.Request) -> H.Response:
        """Find the route, check the session, and answer.

        An HttpError becomes a response, because a refusal is an answer.
        Anything else is left to travel: the adapter is what owns the log, and
        an error nobody logged is one nobody fixes. What the browser gets in
        that case is a bare 500.
        """
        try:
            return self._dispatch(request)
        except H.HttpError as e:
            return H.fail(e.status, e.message)

    def _dispatch(self, request: H.Request) -> H.Response:
        # HEAD is answered by the GET route; the adapter drops the body. A HEAD
        # that 404s where GET succeeds breaks every client that checks first.
        method = "GET" if request.method == "HEAD" else request.method

        for route in self.routes:
            if route.method != method:
                continue
            found = route.pattern.match(request.path)
            if not found:
                continue
            user = self.user_of(request)
            if route.needs_auth and not user:
                raise H.HttpError(401, "authentication required")
            return route.handler(replace(request, params=found.groups(), user=user))

        if method == "GET" and not request.path.startswith(API) and self.static:
            return self.static(request)
        return H.fail(404, "no such route")
