"""The one place a socket appears.

Everything above this is values: a Request in, a Response out. This turns the
connection into the first and writes back the second, and owns the three things
that cannot be values - ranges, streaming, and the log.

Threaded on purpose. A browser holds the audio connection open while it streams
and fetches envelopes at the same time; single-threaded, the second request
waits for a twenty-minute side to finish playing.
"""

from __future__ import annotations

import sys
import traceback
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ripdoctor import __version__
from ripdoctor.web import http as H
from ripdoctor.web.app import App

# Generous enough for a cover: originals from the art archive are routinely
# 3000x3000 and several megabytes. Not unbounded, because the body is read into
# memory before anything looks at it.
MAX_BODY = 32 * 1024 * 1024

CHUNK = 256 * 1024

# Long enough for a slow upload, short enough that a half-open client does not
# hold a thread for the life of the process.
TIMEOUT = 120


def handler_for(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ripdoctor/{__version__}"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = TIMEOUT

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{self.client_address[0]} - {fmt % args}\n")

        def do_GET(self) -> None:
            self._run()

        def do_HEAD(self) -> None:
            self._run()

        def do_POST(self) -> None:
            self._run()

        # ---------------------------------------------------------- reading

        def _request(self) -> H.Request:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise H.HttpError(413, "body too large")
            return H.Request.of(
                self.command,
                self.path,
                headers={k: v for k, v in self.headers.items()},
                body=self.rfile.read(length) if length else b"",
                ip=self.client_address[0],
            )

        def _run(self) -> None:
            try:
                response = app.dispatch(self._request())
            except H.HttpError as e:
                response = H.fail(e.status, e.message)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                # The detail goes to the log, where the person running the
                # server can see it. The browser gets a number.
                traceback.print_exc()
                response = H.fail(500, "internal error")
            # The browser going away mid-answer is ordinary: it happens every
            # time somebody seeks in a side.
            with suppress(BrokenPipeError, ConnectionResetError):
                self._send(response)

        # ---------------------------------------------------------- writing

        def _headers(self, response: H.Response, length: int) -> None:
            for name, value in H.SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", response.cache)
            for name, value in response.headers:
                self.send_header(name, value)

        def _send(self, response: H.Response) -> None:
            if response.stream is not None:
                self._send_stream(response)
                return
            if response.path:
                self._send_file(response)
                return
            self.send_response(response.status)
            self._headers(response, len(response.body))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        def _send_stream(self, response: H.Response) -> None:
            """No length, so no keep-alive: the connection closes at the end.

            A listener closing a tab is how this normally ends, so the source is
            closed on the way out - otherwise whatever is producing the audio
            keeps the sound card for the life of the process.
            """
            assert response.stream is not None  # noqa: S101 - checked by caller
            self.close_connection = True
            self.send_response(200)
            for name, value in H.SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            chunks = response.stream()
            try:
                for block in chunks:
                    self.wfile.write(block)
                    self.wfile.flush()
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()

        def _send_file(self, response: H.Response) -> None:
            path = Path(str(response.path))
            try:
                size = path.stat().st_size
            except OSError:
                self._send(H.fail(404, "not found"))
                return
            try:
                span = H.parse_range(self.headers.get("Range"), size)
            except H.Unsatisfiable:
                self.send_response(416)
                for name, value in H.SECURITY_HEADERS:
                    self.send_header(name, value)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            start, end = span if span else (0, size - 1)
            length = end - start + 1
            self.send_response(206 if span else 200)
            self._headers(response, length)
            self.send_header("Accept-Ranges", "bytes")
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    block = f.read(min(CHUNK, left))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)

    return Handler


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    """Bind and return without serving, so a caller can report the real port."""
    server = ThreadingHTTPServer((host, port), handler_for(app))
    server.daemon_threads = True
    return server


def bound(server: ThreadingHTTPServer) -> str:
    """Where it actually listens. Port 0 asks the system to choose one."""
    host, port = server.socket.getsockname()[:2]
    return f"{host}:{port}"


def serve(app: App, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = make_server(app, host, port)
    print(f"ripdoctor serving on http://{bound(server)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
