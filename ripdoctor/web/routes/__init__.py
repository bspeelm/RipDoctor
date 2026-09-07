"""The routes, assembled. See ripdoctor/web/app.py for how they are reached."""

from __future__ import annotations

from ripdoctor.web.app import App
from ripdoctor.web.routes import (
    art,
    capture,
    cutting,
    library,
    punching,
    records,
    session,
)
from ripdoctor.web.service import Service


def build(service: Service, *, static: object = None) -> App:
    app = App(service.sessions, static=static, now=service.now)  # type: ignore[arg-type]
    session.add(app, service)
    records.add(app, service)
    cutting.add(app, service)
    capture.add(app, service)
    library.add(app, service)
    art.add(app, service)
    punching.add(app, service)
    return app
