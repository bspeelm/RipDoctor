"""Recording a side from a browser: the meter, the controls, and salvage."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from ripdoctor.audio import capture as C
from ripdoctor.audio import passthru as PT
from ripdoctor.audio.devices import enumerate_devices
from ripdoctor.audio.runner import ToolFailed, ToolMissing
from ripdoctor.core.meter import Verdict
from ripdoctor.core.naming import Unsafe, token
from ripdoctor.store import cache as CACHE
from ripdoctor.store import files as F
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.service import Service
from ripdoctor.work.capture import Busy, Live

PARTIAL = ".capturing.wav"


def _format(service: Service, body: dict[str, Any] | None = None) -> C.Format:
    """The configured format, or what was asked for if it is one this records in.

    A format the card cannot take fails inside arecord, seconds after somebody
    has put the needle down.
    """
    asked = body or {}
    wanted = str(asked.get("format") or service.settings.capture_format)
    if wanted not in C.SAMPLE_FORMATS:
        raise H.HttpError(400, f"{wanted} is not a sample format this records in")
    try:
        rate = int(asked.get("rate") or service.settings.capture_rate)
    except (TypeError, ValueError) as e:
        raise H.HttpError(400, "the rate must be a number") from e
    return C.Format(
        rate=rate,
        channels=service.settings.capture_channels,
        sample_format=wanted,
    )


def _agreed_device(service: Service, body: dict[str, Any]) -> str:
    """The configured device, or another one somebody meant on purpose.

    The configured device is the one the thresholds were measured on and the
    one the turntable is plugged into. Recording from a different one is almost
    always a mistake rather than a decision: on 2026-08-23 a rip ran for 162
    seconds against an onboard codec whose input was set to Rear Mic, and
    everything needed to catch it was already known and used only to sort a
    dropdown.

    So a different device is refused unless it is asked for twice.
    """
    configured = service.settings.capture_device
    asked = str(body.get("device") or configured)
    if not asked:
        raise H.HttpError(400, "no capture device is set - run `ripdoctor devices`")
    if configured and asked != configured and not body.get("force_device"):
        raise H.HttpError(
            409,
            f"{asked} is not the configured capture device ({configured}). "
            "If the turntable really is on that one, tick the override.",
        )
    return asked


def _named(body: dict[str, Any], key: str) -> str:
    try:
        return token(str(body.get(key, "")))
    except Unsafe as e:
        raise H.HttpError(400, f"{key}: {e}") from e


def _kind(body: dict[str, Any]) -> str:
    kind = str(body.get("kind", "side"))
    if kind not in C.STEMS:
        raise H.HttpError(400, f"unknown capture kind: {kind!r}")
    return kind


def _forget(service: Service, slug: str, ended: Live | None = None) -> bool:
    """Drop the record's name if nothing at all is left under it.

    The store decides what is left; this only refuses while some other capture
    for the same record is running, because a partial that has not reached
    disk yet is still a take. The capture a back-out has just ended is not
    that, so abandon names it here.
    """
    live = service.recorder.live
    if live is not None and live is not ended and live.running and live.slug == slug:
        return False
    return F.forget(service.layout, slug)


def _note(forgotten: bool) -> str:
    return "nothing was left under that name, so it was forgotten" if forgotten else ""


def _not_while_recording(service: Service, slug: str, side: str, kind: str) -> None:
    """A capture being written is not one to finish or throw away.

    Both of these take the file out from under arecord: salvage encodes what
    has arrived so far and unlinks it, and discard simply unlinks it.
    """
    live = service.recorder.live
    if not live or not live.running:
        return
    if (live.slug, live.side, live.stem) == (slug, side, kind):
        raise H.HttpError(409, f"{slug} {kind} {side} is recording now")


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
                        "configured": d.id == service.settings.capture_device,
                    }
                    for d in found
                ],
                "configured": service.settings.capture_device,
                "rate": service.settings.capture_rate,
                "format": service.settings.capture_format,
            }
        )

    @app.route("GET", "/api/rip/status")
    def status(_r: H.Request) -> H.Response:
        """What is recording, and how much of it has arrived.

        The size comes from the file rather than from the capture loop, which
        does not know it until the outcome is written - so the line reporting
        it said `NaN MB` for the whole of every side.
        """
        out = service.recorder.status()
        live = service.recorder.live
        if live is not None and live.running:
            partial = C.partial_path(layout.raw / live.slug, live.side, live.stem)
            with suppress(OSError):
                out["bytes"] = partial.stat().st_size
        return H.ok(out)

    @app.route("POST", "/api/rip/start")
    def start(r: H.Request) -> H.Response:
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        if not slug or not side:
            raise H.HttpError(400, "a record and a side are needed")
        device = _agreed_device(service, body)
        # A punch is a capture of one track, recorded to replace a dirty take.
        # It is written under a stem no side scan matches.
        stem = str(body.get("kind", "side"))
        album = layout.raw / slug
        # The earliest moment a record's name is known. It was typed into a
        # form and lived nowhere else, so a reload threw it away and left
        # somebody with audio, a slug and no way to start the second side.
        # A punch is a track of a record that already has one, so it never
        # writes; neither does a start that carries no names.
        if stem == "side" and (body.get("artist") or body.get("album")):
            F.remember(
                layout,
                slug,
                album=str(body.get("album", "")),
                artist=str(body.get("artist", "")),
                date=str(body.get("date", "")),
            )
        try:
            live = service.recorder.start(
                service.runner,
                device,
                album,
                slug,
                side,
                _format(service, body),
                autostop=bool(body.get("autostop", True)),
                stem=stem,
            )
        except Busy as e:
            raise H.HttpError(409, str(e)) from e
        except C.CaptureError as e:
            raise H.HttpError(400, str(e)) from e
        return H.ok(
            live.as_dict(service.now(), service.recorder.dwell, service.recorder.below),
            202,
        )

    @app.route("POST", "/api/rip/stop")
    def stop(_r: H.Request) -> H.Response:
        return _control(service, "stop")

    @app.route("POST", "/api/rip/abandon")
    def abandon(_r: H.Request) -> H.Response:
        """Stop, and throw the capture away rather than keeping it.

        For a take that went wrong from the start - the wrong input, the wrong
        side, the needle in the wrong place. Distinct from stop, which keeps
        what was captured, because the two are one button apart and one of them
        cannot be undone.
        """
        live = service.recorder.live
        if live is None or not live.running:
            raise H.HttpError(409, "nothing is recording")
        service.recorder.stop()
        # Wait for the loop to put the capture down. Without this the auto-stop
        # can reach the encode first and leave behind the very side that was
        # just thrown away.
        service.recorder.settle()
        album = layout.raw / live.slug
        partial = C.partial_path(album, live.side, live.stem)
        freed = partial.stat().st_size if partial.is_file() else 0
        partial.unlink(missing_ok=True)
        notes = []
        finished = live.outcome.path if live.outcome else None
        if finished is not None and Path(finished).is_file():
            freed += Path(finished).stat().st_size
            Path(finished).unlink()
            notes.append("the encode had already started; its file went too")
        if _forget(service, live.slug, live):
            notes.append("nothing was left under that name, so it was forgotten")
        return H.ok(
            {
                "ok": True,
                "slug": live.slug,
                "side": live.side,
                "kind": live.stem,
                "seconds": live.outcome.seconds if live.outcome else 0.0,
                "freed_bytes": freed,
                "note": " ".join(notes),
            }
        )

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

    @app.route("GET", "/api/rip/monitor")
    def monitor(r: H.Request) -> H.Response:
        """Hear the input, live.

        The meter says there is signal and the probe says it is musical.
        Neither tells you the arm is tracking, or that this is the record you
        meant. While a capture runs the card is taken, so what is played is the
        file it is writing.
        """
        live = service.recorder.live
        if live is not None and live.running:
            # The capture is walked and fed to the encoder rather than handed
            # to it: ffmpeg reading the file itself ends at every end-of-file,
            # and a monitor keeping pace with a writer catches up constantly.
            wav = C.partial_path(layout.raw / live.slug, live.side, live.stem)
            fmt = _format(service)
            return H.streaming(lambda: PT.follow(service.runner, wav, fmt), "audio/ogg")

        device = str(r.query.get("device") or service.settings.capture_device)
        try:
            argv = PT.device_argv(device, _format(service))
        except C.CaptureError as e:
            raise H.HttpError(400, str(e)) from e
        return H.streaming(lambda: PT.stream(service.runner, argv), "audio/ogg")

    def _partial(album: Path, partial: Path) -> dict[str, Any]:
        """A capture that was interrupted, described well enough to judge it.

        Its length comes from the size rather than from decoding: a WAV that
        was never closed has no length in its header, and this is the one
        number that says whether it is most of a side or a false start.
        """
        fmt = C.wav_format(partial, _format(service))
        frame = fmt.channels * fmt.width
        size = partial.stat().st_size
        live = service.recorder.live
        return {
            "slug": album.name,
            "side": C.letter_of(partial),
            # Which kind of capture this is. A punch is listed here too, and
            # both buttons under it used to look for a side by that letter.
            "kind": C.stem_of(partial),
            "bytes": size,
            "seconds": round(max(0, size - C.HEADER_BYTES) / frame / fmt.rate, 1),
            "recording": bool(live and live.running and live.slug == album.name),
        }

    @app.route("GET", "/api/rip/orphans")
    def orphans(_r: H.Request) -> H.Response:
        """Captures an interrupted session left behind, across every record.

        Each one is most of a side, and a side is twenty minutes of somebody's
        evening.

        The one being written now is not one of them. It was listed, because a
        capture in progress looks exactly like a capture that was interrupted,
        and the panel offered to encode or delete a side while the needle was
        still on it.
        """
        found: list[dict[str, Any]] = []
        for album in sorted(p for p in layout.raw.iterdir() if p.is_dir()):
            found.extend(
                described
                for partial in C.salvageable(album)
                if not (described := _partial(album, partial))["recording"]
            )
        return H.ok({"orphans": found})

    @app.route("POST", "/api/rip/salvage")
    def salvage(r: H.Request) -> H.Response:
        """Encode a capture an interrupted session left behind."""
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        kind = _kind(body)
        _not_while_recording(service, slug, side, kind)
        album = layout.raw / slug
        partial = C.partial_path(album, side, kind)
        # Measured before the encode, and from the size: a WAV that was never
        # closed has no length in its header, and decoding a side to answer
        # one request would cost more than the encode did.
        seconds = _partial(album, partial)["seconds"] if partial.is_file() else 0.0
        try:
            written = C.finish(service.runner, album, side, kind)
        except C.CaptureError as e:
            raise H.HttpError(404, str(e)) from e
        return H.ok(
            {
                "ok": True,
                "path": written.name,
                "slug": slug,
                "side": side,
                "kind": kind,
                "duration": seconds,
            }
        )

    @app.route("POST", "/api/rip/discard")
    def discard(r: H.Request) -> H.Response:
        """Throw away a partial capture, and only ever a partial capture."""
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        kind = _kind(body)
        _not_while_recording(service, slug, side, kind)
        partial = C.partial_path(layout.raw / slug, side, kind)
        if not partial.name.endswith(PARTIAL) or not partial.is_file():
            raise H.HttpError(404, f"no interrupted capture for {slug} {kind} {side}")
        freed = partial.stat().st_size
        partial.unlink()
        return H.ok(
            {
                "ok": True,
                "discarded": f"{slug} {kind} {side}",
                "side": side,
                "kind": kind,
                "freed_bytes": freed,
                "note": _note(_forget(service, slug)),
            }
        )

    @app.route("POST", "/api/rip/discard-side")
    def discard_side(r: H.Request) -> H.Response:
        """Remove a finished side from raw, so it can be recorded again.

        Only from raw, and never from archive: a side in raw is a rip somebody
        can redo, and a side in archive is the only copy there is. The cache
        goes with it, or the next view of the record shows the envelope of a
        capture that no longer exists.
        """
        body = r.json()
        slug, side = _named(body, "slug"), _named(body, "side")
        where = layout.raw / slug / f"side-{side}.flac"
        if not where.is_file():
            raise H.HttpError(404, f"no side {side} in raw for {slug}")
        freed = where.stat().st_size
        where.unlink()
        forgotten = CACHE.forget(layout, slug, side)
        notes = [f"forgot {forgotten} cached files"] if forgotten else []
        if _forget(service, slug):
            notes.append("that was the last of it, so the name went too")
        return H.ok({"ok": True, "side": side, "freed_bytes": freed, "notes": notes})

    @app.route("GET", "/api/rip/sides/([^/]+)")
    def sides(r: H.Request) -> H.Response:
        """Every capture on disk for this record, finished or not.

        One list rather than two: what a person wants to know is what is there
        and which of it can be thrown away - and a side still being written
        cannot be.
        """
        slug = token(r.params[0])
        album = layout.raw / slug
        if not album.is_dir():
            return H.ok({"sides": []})
        found: list[dict[str, Any]] = [
            {
                "slug": slug,
                "side": p.name[len("side-") : -len(".flac")],
                "bytes": p.stat().st_size,
                "recording": False,
                "finished": True,
            }
            for p in sorted(album.glob("side-*.flac"))
        ]
        found.extend(
            {**_partial(album, p), "finished": False} for p in C.salvageable(album)
        )
        return H.ok({"sides": found})


def _control(service: Service, what: str) -> H.Response:
    try:
        getattr(service.recorder, what)()
    except Busy as e:
        raise H.HttpError(409, str(e)) from e
    return H.ok(service.recorder.status())
