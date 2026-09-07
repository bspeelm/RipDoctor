"""One record, from an empty pool to cut tracks, over HTTP.

Every other test drives one layer. This one goes through the socket, the
session, the job runner and the pool the way a browser does, because the parts
can each be right while the whole is not.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.web import auth as A
from ripdoctor.web.httpd import make_server
from ripdoctor.web.routes import build
from tests.pool import a_layout, a_runner, quiet_then_loud
from tests.test_web_routes import a_service

SIDE_SECONDS = 6.0


class Tools(FakeRunner):
    """ffmpeg that leaves behind whatever it was told to write."""

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if args[0] == "ffmpeg" and args[-1].endswith((".tmp", ".flac")):
            Path(args[-1]).write_bytes(b"\x00" * 600)
        return super().run(argv, stdin=stdin, timeout=timeout)


class Client:
    """A browser, more or less: one connection and a cookie jar of one."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port, self.cookie = host, port, ""

    def __call__(self, method: str, path: str, body: dict | None = None):  # type: ignore[no-untyped-def]
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        headers = {"Cookie": self.cookie} if self.cookie else {}
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        setcookie = r.getheader("Set-Cookie")
        if setcookie:
            self.cookie = setcookie.split(";", 1)[0]
        conn.close()
        return r.status, (json.loads(raw) if raw.startswith(b"{") else raw)


@pytest.fixture
def running(tmp_path: Path) -> Iterator[tuple[Client, Path]]:
    layout = a_layout(tmp_path, sides=("a",))
    service = a_service(tmp_path, layout=layout)
    windows = int(SIDE_SECONDS / 0.05)
    measuring = a_runner(
        windows=windows, seconds=SIDE_SECONDS, levels=quiet_then_loud(windows)
    )
    service.runner = Tools(replies=measuring.replies)
    service.settings = replace(service.settings, library=str(tmp_path / "music"))

    httpd = make_server(build(service), "127.0.0.1", 0)
    host, port = httpd.socket.getsockname()[:2]
    thread = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield Client(host, port), tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_record_goes_from_the_pool_to_cut_tracks(running) -> None:  # type: ignore[no-untyped-def]
    call, tmp_path = running

    # Nothing is reachable before logging in.
    status, _body = call("GET", "/api/albums")
    assert status == 401

    status, body = call(
        "POST", "/api/login", {"user": "listener", "password": "secret"}
    )
    assert status == 200 and body["user"] == "listener"

    status, body = call("GET", "/api/albums")
    assert status == 200 and [a["slug"] for a in body["albums"]] == ["album"]

    status, body = call("POST", "/api/prepare/album")
    assert status == 202 and body["done"] and not body["error"], body["error"]

    status, body = call("GET", "/api/album/album")
    assert status == 200 and body["ready"]["a"]["windows"] > 0

    status, body = call("GET", "/api/gaps/album/a")
    assert status == 200 and body["band"]["gaps"], "no gaps in a side with one"

    gap = body["band"]["gaps"][0]
    plan = {
        "slug": "album",
        "album": "A Record",
        "artist": "A Band",
        "date": "2022",
        "sides": [
            {
                "file": "side-a.flac",
                "tracks": [
                    {
                        "number": 1,
                        "title": "One",
                        "start": 0.5,
                        "end": round(gap["lo"], 2),
                        "cat": 2.0,
                    },
                    {
                        "number": 2,
                        "title": "Two",
                        "start": round(gap["hi"], 2),
                        "end": SIDE_SECONDS - 0.5,
                        "cat": 2.0,
                    },
                ],
            }
        ],
    }
    status, body = call("POST", "/api/plan/album", plan)
    assert status == 200 and body["ok"]

    # The saved edges came back as ear-set ones, which is what makes the next
    # fit a pass-through rather than a recomputation.
    spec = json.loads((tmp_path / "vinyl" / "work" / "album.spec.json").read_text())
    assert spec["sides"][0]["tracks"][0]["start"] == 0.5

    status, body = call("POST", "/api/split/album")
    assert status == 202 and body["result"]["tracks"] == 2

    status, body = call("GET", "/api/review/album")
    assert [t["index"] for t in body["tracks"]] == [0, 1]

    status, body = call("POST", "/api/clips/album")
    assert status == 202 and body["result"]["clips"] == 4

    status, body = call("GET", "/api/archive/album")
    assert not body["ready"] and body["expected"] == 2, "the gate let it through"

    status, _body = call("POST", "/api/logout")
    assert status == 200
    assert call("GET", "/api/albums")[0] == 401, "the session outlived the logout"


def test_the_session_cookie_is_what_carries_the_login(running) -> None:  # type: ignore[no-untyped-def]
    call, _tmp = running
    call("POST", "/api/login", {"user": "listener", "password": "secret"})
    assert call.cookie.startswith(A.COOKIE)
    call.cookie = f"{A.COOKIE}=forged"
    assert call("GET", "/api/albums")[0] == 401
