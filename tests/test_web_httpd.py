"""The adapter, over a real loopback socket.

Everything above it is tested as values; this is the part that cannot be. A
server on an ephemeral port costs milliseconds and is the only way to know the
ranges, the streaming and the headers actually work.
"""

from __future__ import annotations

import http.client
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from ripdoctor.web import http as H
from ripdoctor.web import httpd as httpd_module
from ripdoctor.web import static
from ripdoctor.web.app import App
from ripdoctor.web.auth import Sessions
from ripdoctor.web.httpd import make_server

AUDIO = b"".join(bytes([i % 251]) for i in range(5000))


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, int, Path]]:
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<html>the front page</html>")
    (root / "side.opus").write_bytes(AUDIO)

    app = App(Sessions(secret=b"0" * 32), static=static.handler(root))

    @app.route("GET", "/healthz", needs_auth=False)
    def health(_r: H.Request) -> H.Response:
        return H.ok({"ok": True})

    @app.route("POST", "/api/echo", needs_auth=False)
    def echo(r: H.Request) -> H.Response:
        return H.ok({"got": r.json()})

    @app.route("GET", "/api/audio", needs_auth=False)
    def audio(_r: H.Request) -> H.Response:
        return H.file_at(str(root / "side.opus"))

    @app.route("GET", "/api/boom", needs_auth=False)
    def boom(_r: H.Request) -> H.Response:
        raise ZeroDivisionError("not thought of")

    httpd = make_server(app, "127.0.0.1", 0)
    host, port = httpd.socket.getsockname()[:2]
    # A short poll interval only affects how quickly shutdown is noticed. The
    # default half-second is right for a real server and is a half-second of
    # teardown in every test here.
    thread = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield host, port, root
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def call(server, method: str, path: str, **kw):  # type: ignore[no-untyped-def]
    host, port, _root = server
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request(method, path, **kw)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), body


# ---------------------------------------------------------------- basics


def test_a_route_answers_over_the_socket(server) -> None:  # type: ignore[no-untyped-def]
    status, headers, body = call(server, "GET", "/healthz")
    assert status == 200 and b'"ok": true' in body
    assert headers["Content-Type"] == "application/json"


def test_a_body_reaches_the_handler(server) -> None:  # type: ignore[no-untyped-def]
    status, _h, body = call(
        server,
        "POST",
        "/api/echo",
        body=b'{"slug": "album"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 200 and b'"slug": "album"' in body


def test_every_response_carries_the_security_headers(server) -> None:  # type: ignore[no-untyped-def]
    _s, headers, _b = call(server, "GET", "/healthz")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "Access-Control-Allow-Origin" not in headers


def test_a_head_carries_the_length_but_no_body(server) -> None:  # type: ignore[no-untyped-def]
    status, headers, body = call(server, "HEAD", "/healthz")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0


def test_an_unexpected_error_is_a_500_with_nothing_in_it(server, capfd) -> None:  # type: ignore[no-untyped-def]
    status, _h, body = call(server, "GET", "/api/boom")
    assert status == 500
    assert b"ZeroDivisionError" not in body, "the browser was told the internals"
    assert "ZeroDivisionError" in capfd.readouterr().err, "nothing was logged"


# ---------------------------------------------------------------- static


def test_the_front_page_is_served_for_a_bare_path(server) -> None:  # type: ignore[no-untyped-def]
    status, headers, body = call(server, "GET", "/")
    assert status == 200 and b"the front page" in body
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-cache", "a stale app.js is unreproducible"


def test_a_front_end_file_can_be_revalidated(server) -> None:  # type: ignore[no-untyped-def]
    """`no-cache` says revalidate, and revalidating needs something to
    revalidate against. Without a validator a browser decides for itself - and
    one that decided to keep app.js served a page from before a deployment
    against an API from after it, which is what the header exists to prevent."""
    status, headers, _b = call(server, "GET", "/")
    assert status == 200 and headers["ETag"]
    again, _h, body = call(
        server, "GET", "/", headers={"If-None-Match": headers["ETag"]}
    )
    assert again == 304 and not body


def test_a_stale_validator_is_not_answered_304(server) -> None:  # type: ignore[no-untyped-def]
    stale = '"0-0"'
    status, _h, body = call(server, "GET", "/", headers={"If-None-Match": stale})
    assert status == 200 and body, "a stale validator must not answer 304"


@pytest.mark.parametrize("attempt", ["/../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd"])
def test_nothing_outside_the_front_end_is_reachable(server, attempt: str) -> None:  # type: ignore[no-untyped-def]
    status, _h, _b = call(server, "GET", attempt)
    assert status in (400, 403, 404)


def test_a_symlink_out_of_the_tree_is_refused(server) -> None:  # type: ignore[no-untyped-def]
    """Every component of the path is ordinary; only resolving it shows where
    it goes."""
    _host, _port, root = server
    (root / "escape").symlink_to("/etc")
    status, _h, _b = call(server, "GET", "/escape/passwd")
    assert status == 403


# ---------------------------------------------------------------- ranges


def test_a_whole_file_is_streamed(server) -> None:  # type: ignore[no-untyped-def]
    status, headers, body = call(server, "GET", "/api/audio")
    assert status == 200 and body == AUDIO
    assert headers["Accept-Ranges"] == "bytes"


def test_a_range_comes_back_as_a_206(server) -> None:  # type: ignore[no-untyped-def]
    """Without this the audio element refuses to seek at all, which on a
    twenty-minute side means no way to reach the boundary being judged."""
    status, headers, body = call(
        server, "GET", "/api/audio", headers={"Range": "bytes=100-199"}
    )
    assert status == 206 and body == AUDIO[100:200]
    assert headers["Content-Range"] == f"bytes 100-199/{len(AUDIO)}"
    assert headers["Content-Length"] == "100"


def test_an_open_ended_range_runs_to_the_end(server) -> None:  # type: ignore[no-untyped-def]
    status, _h, body = call(
        server, "GET", "/api/audio", headers={"Range": "bytes=4900-"}
    )
    assert status == 206 and body == AUDIO[4900:]


def test_a_range_past_the_end_is_a_416_not_a_200(server) -> None:  # type: ignore[no-untyped-def]
    """A 200 hands the player the wrong bytes with no way to tell."""
    status, headers, _b = call(
        server, "GET", "/api/audio", headers={"Range": "bytes=99999-"}
    )
    assert status == 416
    assert headers["Content-Range"] == f"bytes */{len(AUDIO)}"


def test_a_file_that_vanished_is_a_404(server) -> None:  # type: ignore[no-untyped-def]
    _host, _port, root = server
    (root / "side.opus").unlink()
    status, _h, _b = call(server, "GET", "/api/audio")
    assert status == 404


def test_two_requests_are_served_at_once(server) -> None:  # type: ignore[no-untyped-def]
    """A browser holds the audio connection open while it fetches envelopes.
    Single-threaded, the second waits for the side to finish playing."""
    host, port, _root = server
    held = http.client.HTTPConnection(host, port, timeout=10)
    held.request("GET", "/api/audio", headers={"Range": "bytes=0-9"})
    held.getresponse().read()  # connection stays open, keep-alive
    status, _h, _b = call(server, "GET", "/healthz")
    held.close()
    assert status == 200


def test_a_connection_that_never_asks_for_anything_is_not_an_error(
    tmp_path: Path, capfd, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A tab somebody left open holds a keep-alive connection until it times
    out. Reported as a failure, it is indistinguishable in the log from a
    request that arrived and could not be served - which is the one case the
    log has to be able to tell apart."""
    monkeypatch.setattr(httpd_module, "TIMEOUT", 0.2)
    app = App(Sessions(secret=b"0" * 32))

    @app.route("GET", "/healthz", needs_auth=False)
    def health(_r: H.Request) -> H.Response:
        return H.ok({"ok": True})

    server = make_server(app, "127.0.0.1", 0)
    host, port = server.socket.getsockname()[:2]
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        capfd.readouterr()
        sock = socket.create_connection((host, port), timeout=5)
        sock.recv(1)  # nothing is sent; wait for the server to give up
        sock.close()
        assert "timed out" not in capfd.readouterr().err
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
