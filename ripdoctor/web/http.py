"""Requests and responses as values.

The predecessor's handlers reached into the live connection object, so nothing
above the socket could be tested without one - and a thousand lines of routing
went untested for that reason alone. Here a route is a function from a Request
value to a Response value. The socket appears once, in the adapter.
"""

from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlsplit

JSON = "application/json"
OCTETS = "application/octet-stream"

# Sent on every response. No Access-Control-Allow-Origin appears anywhere: this
# serves one origin to one browser on a LAN, and the absence is the policy.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "no-referrer"),
)


class HttpError(Exception):
    """A refusal with a status code. Never a traceback to the browser."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@dataclass(frozen=True, slots=True)
class Request:
    """One request, complete. Nothing here reaches back to a connection."""

    method: str = "GET"
    path: str = "/"
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    params: tuple[str, ...] = ()
    user: str | None = None
    ip: str = ""

    @classmethod
    def of(
        cls,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        ip: str = "",
    ) -> Request:
        """Build one from a request line, splitting the query off the path."""
        split = urlsplit(target)
        return cls(
            method=method.upper(),
            path=split.path or "/",
            query=dict(parse_qsl(split.query)),
            headers=_lower(headers or {}),
            body=body,
            ip=ip,
        )

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def cookie(self, name: str) -> str | None:
        raw = self.header("cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            # A malformed cookie header is not an authenticated request, and it
            # is not a 500 either.
            return None
        found = jar.get(name)
        return found.value if found else None

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise HttpError(400, "malformed JSON body") from e
        if not isinstance(parsed, dict):
            raise HttpError(400, "expected a JSON object")
        return parsed


@dataclass(frozen=True, slots=True)
class Response:
    """One response. `path` names a file to stream instead of a body."""

    status: int = 200
    body: bytes = b""
    content_type: str = JSON
    headers: tuple[tuple[str, str], ...] = ()
    cache: str = "no-store"
    path: str | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body)

    def with_header(self, name: str, value: str) -> Response:
        from dataclasses import replace

        return replace(self, headers=(*self.headers, (name, value)))


def ok(payload: Any, status: int = 200) -> Response:
    return Response(status=status, body=json.dumps(payload).encode(), content_type=JSON)


def fail(status: int, message: str) -> Response:
    return ok({"error": message}, status=status)


def file_at(path: str, *, cache: str = "no-store") -> Response:
    """A response the adapter streams, with range requests, from disk."""
    return Response(
        content_type=mimetypes.guess_type(path)[0] or OCTETS,
        cache=cache,
        path=path,
    )


# Browsers seek media by sending `Range: bytes=N-`. Without a correct 206 the
# audio element refuses to seek at all, which on a twenty-minute side means
# there is no way to reach the boundary being judged.
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Unsatisfiable(Exception):
    """A range that cannot be served. Answered 416, never 200."""


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The byte span a Range header asks for, or None for the whole file.

    Only a single range is understood. Multipart ranges are not implemented
    because no browser asks for them on media, and answering one badly is worse
    than answering 200.
    """
    if not header:
        return None
    found = _RANGE.match(header.strip())
    if not found or not (found.group(1) or found.group(2)):
        raise Unsatisfiable(header)

    first, last = found.group(1), found.group(2)
    if first == "":
        # A suffix range: the last N bytes. Asking for more than there is means
        # the whole file, which is what the specification says.
        start, end = max(0, size - int(last)), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1

    if start >= size or start > end:
        raise Unsatisfiable(header)
    return start, end
