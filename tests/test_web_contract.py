"""What the page reads off each answer, against what each answer contains.

Four payload mismatches reached a person before this existed. Every one was
the same shape: a route returning something reasonable, a page reading a field
that was not in it, and nothing failing until somebody clicked the one control
that needed it. `/api/albums` returned bare names for weeks because the picker
is only drawn when a record is in raw/, and there wasn't one.

The other front-end tests check that a route exists and that an element exists.
Neither notices a route answering with the wrong thing. This is the missing
half: the fields are listed here by hand, because a page's reads cannot be
extracted reliably, and a list somebody maintains is worth more than a clever
scan that misses the case that matters.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio import capture as CAP
from ripdoctor.web.routes import build
from tests.test_web_capture import a_stalled_recorder
from tests.test_web_routes import a_plan_body, a_service, get, post

# endpoint -> the fields the front end reads off each item of that list.
LISTS = {
    "/api/albums": (
        "albums",
        (
            "slug",
            "where",
            "old_format",
            "album",
            "artist",
            "date",
            "sides",
            "archived_copy",
        ),
    ),
    "/api/rip/sides/album": ("sides", ("slug", "side", "bytes", "recording")),
    "/api/rip/orphans": ("orphans", ("slug", "side", "kind", "bytes", "seconds")),
    "/api/review/album": ("tracks", ("index", "name")),
}

# The clip endpoints have no page: boundaries are auditioned in the browser
# from the preview, with the tick mixed there. They mirror `ripdoctor check`
# for anyone driving this over HTTP, and are covered by their own tests.

# endpoint -> the fields the front end reads off the answer itself.
OBJECTS = {
    "/api/album/album": (
        "sides",
        "ready",
        "recording",
        "mbid",
        "album",
        "artist",
        "date",
        "tracks_by_side",
    ),
    "/api/rip/status": ("running", "stage"),
    "/api/rip/devices": ("devices", "configured", "rate", "format"),
    "/api/library": ("library", "exists"),
    "/api/archive/album": (
        "ready",
        "why",
        "library_path",
        "library_tracks",
        "will_archive_to",
        "sides",
        "will_remove",
    ),
    "/api/punch/album": ("slug", "album", "artist", "tracks"),
    "/api/ingest/album": ("exists",),
}

# The back-outs. Each is a POST, and each one's answer is read straight into a
# toast - which is where `abandoned side undefined after NaN` came from.
BACKOUTS = {
    "/api/rip/abandon": ("z", ("side", "kind", "seconds", "freed_bytes", "note")),
    "/api/rip/salvage": ("z", ("side", "kind", "duration", "path")),
    "/api/rip/discard": ("z", ("side", "kind", "freed_bytes", "note")),
    "/api/rip/discard-side": ("a", ("side", "freed_bytes", "notes")),
}


def a_pool(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A record with a capture, a saved plan and an interrupted side."""
    service = a_service(tmp_path)
    post(build(service), "/api/plan/album", service, a_plan_body())
    album = service.layout.raw / "album"
    CAP.partial_path(album, "z").write_bytes(b"RIFF" + b"\x00" * 9000)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True)
    (review / "01 One.flac").write_bytes(b"fLaC")
    clips = service.layout.clips_dir("album")
    clips.mkdir(parents=True)
    (clips / "01 track 1 start.flac").write_bytes(b"fLaC")
    return service


@pytest.mark.parametrize(("path", "spec"), LISTS.items(), ids=lambda v: str(v)[:40])
def test_every_item_carries_what_the_page_draws_it_with(
    tmp_path: Path, path: str, spec: tuple[str, tuple[str, ...]]
) -> None:
    key, fields = spec
    service = a_pool(tmp_path)
    body = get(build(service), path, service).json()
    assert key in body, f"{path} has no {key!r}"
    assert body[key], f"{path} returned nothing to check - the fixture is wrong"
    for item in body[key]:
        missing = [f for f in fields if f not in item]
        assert not missing, f"{path} items are missing {missing}"


@pytest.mark.parametrize(("path", "fields"), OBJECTS.items(), ids=lambda v: str(v)[:40])
def test_every_answer_carries_what_the_page_reads(
    tmp_path: Path, path: str, fields: tuple[str, ...]
) -> None:
    service = a_pool(tmp_path)
    body = get(build(service), path, service).json()
    missing = [f for f in fields if f not in body]
    assert not missing, f"{path} is missing {missing}"


def test_the_saved_plan_round_trips_through_the_page_s_own_shape(
    tmp_path: Path,
) -> None:
    """The editor sends sides by letter and no filenames. A save that comes
    back 400 is a silent dead end at the one step that must never lose work."""
    service = a_service(tmp_path)
    r = post(build(service), "/api/plan/album", service, a_plan_body())
    assert r.status == 200
    back = get(build(service), "/api/album/album", service).json()
    assert back["tracks_by_side"]["a"][0]["title"] == "One"


def test_the_lists_named_here_are_the_ones_the_page_actually_calls() -> None:
    """A contract for an endpoint nobody calls protects nothing, and one the
    page calls but this forgot is how the last four got through."""
    source = (
        Path(__file__).resolve().parent.parent
        / "ripdoctor"
        / "web"
        / "static"
        / "app.js"
    ).read_text()
    for path in list(LISTS) + list(OBJECTS):
        stem = path.replace("/album", "/").rstrip("/")
        assert stem in source, f"nothing calls {path}"
    assert json.dumps(a_plan_body())


@pytest.mark.parametrize(("path", "spec"), BACKOUTS.items(), ids=lambda v: str(v)[:40])
def test_every_way_out_says_what_it_took(
    tmp_path: Path, path: str, spec: tuple[str, tuple[str, ...]]
) -> None:
    side, fields = spec
    service = a_pool(tmp_path)
    if path == "/api/rip/abandon":
        service.settings = replace(service.settings, capture_device="hw:Rx,0")
        service.recorder = a_stalled_recorder()
    app = build(service)
    if path == "/api/rip/abandon":
        post(app, "/api/rip/start", service, {"slug": "album", "side": side})
    r = post(app, path, service, {"slug": "album", "side": side})
    assert r.status == 200, r.json()
    missing = [f for f in fields if f not in r.json()]
    assert not missing, f"{path} is missing {missing}"
