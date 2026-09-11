"""Taking in a file recorded somewhere else, over the wire.

The load-bearing one here is that nothing appears under a side name while an
upload is arriving. Everything the cut stage assumes rests on it.
"""

from __future__ import annotations

from pathlib import Path

from ripdoctor.store import files as F
from ripdoctor.store import incoming as IN
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.routes.ingest import CHUNK, MAX_UPLOAD
from ripdoctor.web.service import Service
from tests.pool import a_layout
from tests.test_web_routes import a_service, get, post, signed_in

A_FILE = {"name": "album.flac", "size": 9, "artist": "A Band", "album": "A Record"}


def empty_pool(tmp_path: Path) -> Service:
    """A pool with no records in it - a_layout makes one, and an upload that
    creates a record has to be told from one that already existed."""
    layout = a_layout(tmp_path)
    for side in (layout.raw / "album").glob("*"):
        side.unlink()
    (layout.raw / "album").rmdir()
    return a_service(tmp_path, layout=layout)


def begun(service: Service, **over: object) -> H.Response:
    return post(build(service), "/api/ingest/album/begin", service, {**A_FILE, **over})


def sending(service: Service, at: int, body: bytes) -> H.Response:
    """The piece is the body itself, so this cannot go through `post`."""
    return build(service).dispatch(
        H.Request.of(
            "POST",
            f"/api/ingest/album/chunk?at={at}",
            headers=signed_in(service),
            body=body,
        )
    )


# ------------------------------------------------------------------ beginning


def test_beginning_an_upload_names_the_record(tmp_path: Path) -> None:
    """The earliest moment the name is known. A reload would otherwise throw
    away what was typed and leave a slug with nothing behind it."""
    service = empty_pool(tmp_path)
    assert begun(service).status == 200
    spec = F.read_spec(service.layout.spec_file("album"))
    assert spec.artist == "A Band" and spec.album == "A Record"


def test_the_server_names_the_piece_size(tmp_path: Path) -> None:
    """So no client hard-codes a number that would later disagree."""
    body = begun(empty_pool(tmp_path)).json()
    assert body["chunk"] == CHUNK and body["have"] == 0 and body["total"] == 9


def test_a_file_with_no_size_is_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    r = post(build(service), "/api/ingest/album/begin", service, {"name": "a.flac"})
    assert r.status == 400


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    assert begun(empty_pool(tmp_path), size=0).status == 400


def test_a_file_past_the_ceiling_is_refused(tmp_path: Path) -> None:
    assert begun(empty_pool(tmp_path), size=MAX_UPLOAD + 1).status == 413


def test_a_record_that_already_has_the_side_is_refused(tmp_path: Path) -> None:
    """a_layout leaves side-a in place, which is the case being refused."""
    service = a_service(tmp_path)
    r = begun(service)
    assert r.status == 409 and "already has side" in r.json()["error"]


def test_replacing_an_existing_side_is_allowed_when_asked_for(
    tmp_path: Path,
) -> None:
    assert begun(a_service(tmp_path), replace=True).status == 200


def test_a_different_file_part_way_here_is_refused_with_what_is_there(
    tmp_path: Path,
) -> None:
    """So the page can offer resume-or-restart rather than guess."""
    service = empty_pool(tmp_path)
    begun(service)
    sending(service, 0, b"abc")
    r = begun(service, name="something-else.flac")
    assert r.status == 409
    assert "album.flac" in r.json()["error"] and "3 of 9" in r.json()["error"]


def test_resuming_the_same_file_is_not_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    sending(service, 0, b"abc")
    assert begun(service).status == 200


def test_a_slug_that_is_not_a_token_is_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    r = post(build(service), "/api/ingest/..%2Fescape/begin", service, A_FILE)
    assert r.status == 400 and "bad path component" in r.json()["error"]


# -------------------------------------------------------------------- pieces


def test_pieces_assemble_and_the_count_comes_back(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    assert sending(service, 0, b"abc").json() == {"have": 3, "total": 9}
    assert sending(service, 3, b"def").json() == {"have": 6, "total": 9}


def test_nothing_is_written_under_a_side_name_until_it_is_finished(
    tmp_path: Path,
) -> None:
    """The load-bearing one. A file still arriving is not a record, and the
    listing, the prepare and the cut stage all assume a side is whole."""
    service = empty_pool(tmp_path)
    begun(service)
    sending(service, 0, b"fLaC")
    assert service.layout.albums() == []
    assert not (service.layout.raw / "album").exists()


def test_a_piece_at_the_wrong_offset_is_refused_and_says_where_it_is(
    tmp_path: Path,
) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    sending(service, 0, b"abc")
    r = sending(service, 0, b"abc")
    assert r.status == 409 and "is at 3" in r.json()["error"]


def test_an_empty_piece_is_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    assert sending(service, 0, b"").status == 400


def test_a_piece_over_the_ceiling_is_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service, size=CHUNK * 2)
    assert sending(service, 0, b"x" * (CHUNK + 1)).status == 413


def test_a_piece_running_past_the_end_is_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    assert sending(service, 0, b"x" * 10).status == 409


def test_a_piece_with_no_offset_is_refused(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    r = build(service).dispatch(
        H.Request.of(
            "POST",
            "/api/ingest/album/chunk",
            headers=signed_in(service),
            body=b"abc",
        )
    )
    assert r.status == 400


def test_a_piece_with_no_upload_in_progress_is_refused(tmp_path: Path) -> None:
    assert sending(empty_pool(tmp_path), 0, b"abc").status == 409


# --------------------------------------------------------------------- asking


def test_an_interrupted_upload_says_how_much_is_there(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    sending(service, 0, b"abcd")
    body = get(build(service), "/api/ingest/album", service).json()
    assert body["exists"] and body["have"] == 4 and body["name"] == "album.flac"


def test_nothing_arriving_says_so_rather_than_failing(tmp_path: Path) -> None:
    service = empty_pool(tmp_path)
    assert get(build(service), "/api/ingest/album", service).json() == {"exists": False}


# ------------------------------------------------------------------ cancelling


def test_cancelling_removes_the_scratch_and_forgets_an_empty_name(
    tmp_path: Path,
) -> None:
    service = empty_pool(tmp_path)
    begun(service)
    sending(service, 0, b"abc")
    body = post(build(service), "/api/ingest/album/cancel", service).json()
    assert body["freed_bytes"] > 0 and body["forgot"] is True
    assert IN.state(service.layout, "album") is None
    assert not service.layout.spec_file("album").is_file()
