"""Recording a side from a browser: the meter, the controls, and salvage."""

from __future__ import annotations

from typing import Any

from ripdoctor.audio import capture as C
from ripdoctor.audio.devices import enumerate_devices
from ripdoctor.audio.runner import ToolFailed, ToolMissing
from ripdoctor.core.meter import Verdict
from ripdoctor.core.naming import Unsafe, token
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.service import Service
from ripdoctor.work.capture import Busy

PARTIAL = ".capturing.wav"


def _format(service: Service) -> C.Format:
    return C.Format(
        rate=service.settings.capture_rate,
        channels=service.settings.capture_channels,
        sample_format=service.settings.capture_format,
    )


def _named(body: dict[str, Any], key: str) -> str:
    try:
        return token(str(body.get(key, "")))
    except Unsafe as e:
        raise H.HttpError(400, f"{key}: {e}") from e


def _verdict(v: Verdict) -> dict[str, Any]:
    return {
        "ok": v.ok,
        "summary": v.summary,
        "full_rms": v.full_rms,
        "full_peak": v.full_peak,
        "band_rms": v.band_rms,
    }


def add(app: App, service: Service) -> None:
    layout = service.layout

    @app.route("GET", "/api/rip/devices")
    def devices(_r: H.Request) -> H.Response:
        try:
            found = enumerate_devices(service.runner)
        except (ToolMissing, ToolFailed) as e:
            raise H.HttpError(503, str(e)) from e
        return H.ok(
            {
                "devices": [
                    {
                        "id": d.id,
                        "name": d.name,
                        "rates": list(d.rates),
                        "formats": list(d.formats),
                    }
                    for d in found
                ],
                "configured": service.settings.capture_device,
            }
        )

    @app.route("GET", "/api/rip/status")
    def status(_r: H.Request) -> H.Response:
        return H.ok(service.recorder.status())

    @app.route("POST", "/api/rip/start")
    def start(r: H.Request) -> H.Response:
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        if not slug or not side:
            raise H.HttpError(400, "a record and a side are needed")
        device = str(body.get("device") or service.settings.capture_device)
        # A punch is a capture of one track, recorded to replace a dirty take.
        # It is written under a stem no side scan matches.
        stem = str(body.get("kind", "side"))
        album = layout.raw / slug
        try:
            live = service.recorder.start(
                service.runner,
                device,
                album,
                slug,
                side,
                _format(service),
                autostop=bool(body.get("autostop", True)),
                stem=stem,
            )
        except Busy as e:
            raise H.HttpError(409, str(e)) from e
        except C.CaptureError as e:
            raise H.HttpError(400, str(e)) from e
        return H.ok(live.as_dict(service.now(), service.recorder.dwell), 202)

    @app.route("POST", "/api/rip/stop")
    def stop(_r: H.Request) -> H.Response:
        return _control(service, "stop")

    @app.route("POST", "/api/rip/snooze")
    def snooze(_r: H.Request) -> H.Response:
        """Forgive the quiet stretch in progress without disarming anything."""
        return _control(service, "snooze")

    @app.route("POST", "/api/rip/autostop")
    def autostop(r: H.Request) -> H.Response:
        on = bool(r.json().get("on", True))
        try:
            service.recorder.set_autostop(on)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e
        return H.ok(service.recorder.status())

    @app.route("POST", "/api/rip/test")
    def test(r: H.Request) -> H.Response:
        """Twenty seconds, and what arrived.

        Synchronous on purpose: it is twenty seconds and the answer is the
        whole point, so there is nothing useful to do with a job handle.
        """
        device = str(r.json().get("device") or service.settings.capture_device)
        try:
            C.check_device(device)
        except C.CaptureError as e:
            raise H.HttpError(400, str(e)) from e
        scratch = layout.cache / "probe.wav"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        argv = C.test_capture_argv(
            device, str(scratch), _format(service), C.TEST_SECONDS
        )
        try:
            service.runner.run(argv, timeout=C.TEST_SECONDS + 30).require()
            if not scratch.is_file() or scratch.stat().st_size < 1024:
                raise H.HttpError(503, "nothing was captured")
            return H.ok(_verdict(C.judge(service.runner, str(scratch))))
        finally:
            scratch.unlink(missing_ok=True)

    @app.route("GET", "/api/rip/orphans")
    def orphans(_r: H.Request) -> H.Response:
        """Captures an interrupted session left behind, across every record.

        Each one is most of a side, and a side is twenty minutes of somebody's
        evening.
        """
        found = []
        for album in sorted(p for p in layout.raw.iterdir() if p.is_dir()):
            for partial in C.salvageable(album):
                found.append(
                    {
                        "slug": album.name,
                        "side": C.letter_of(partial),
                        "bytes": partial.stat().st_size,
                    }
                )
        return H.ok({"orphans": found})

    @app.route("POST", "/api/rip/salvage")
    def salvage(r: H.Request) -> H.Response:
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        try:
            written = C.finish(service.runner, layout.raw / slug, side)
        except C.CaptureError as e:
            raise H.HttpError(404, str(e)) from e
        return H.ok({"ok": True, "path": written.name})

    @app.route("POST", "/api/rip/discard")
    def discard(r: H.Request) -> H.Response:
        """Throw away a partial capture, and only ever a partial capture."""
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        partial = C.partial_path(layout.raw / slug, side)
        if not partial.name.endswith(PARTIAL) or not partial.is_file():
            raise H.HttpError(404, f"no interrupted capture for {slug} side {side}")
        partial.unlink()
        return H.ok({"ok": True, "discarded": f"{slug} side {side}"})

    @app.route("GET", "/api/rip/sides/([^/]+)")
    def sides(r: H.Request) -> H.Response:
        album = layout.raw / token(r.params[0])
        finished = (
            sorted(p.name for p in album.glob("side-*.flac")) if album.is_dir() else []
        )
        return H.ok(
            {
                "sides": finished,
                "capturing": [
                    C.letter_of(p) for p in C.salvageable(album) if album.is_dir()
                ],
            }
        )


def _control(service: Service, what: str) -> H.Response:
    try:
        getattr(service.recorder, what)()
    except Busy as e:
        raise H.HttpError(409, str(e)) from e
    return H.ok(service.recorder.status())
