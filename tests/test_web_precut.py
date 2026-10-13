"""An album bought already cut, over the wire. ADR-062.

The load-bearing one here is that the upload is cleared only after the library
is confirmed to hold it - the same order the archive step uses.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.config.settings import Settings
from ripdoctor.store import incoming as IN
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.routes.precut import CHUNK, HELD
from ripdoctor.web.service import Service
from tests.pool import a_layout
from tests.test_web_routes import a_service, get, post, signed_in

MATCH = (
    b"/w (2 items)\nCorrecting tags from:\n    A Band - A Record\nURL:\n"
    b"    https://musicbrainz.org/release/1b7c2f4e-7d8a-4e2c-9f11-6a3d5e8c0b92\n"
    b"(Similarity: 98.0%)\n"
)
MBID = "1b7c2f4e-7d8a-4e2c-9f11-6a3d5e8c0b92"


def an_album(names: dict[str, bytes] | None = None) -> bytes:
    held = names or {"01 One.flac": b"aaaa", "02 Two.flac": b"bbbb", "cover.jpg": b"c"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in held.items():
            zf.writestr(name, body)
    return buf.getvalue()


def a_precut_service(
    tmp_path: Path, runner: FakeRunner | None = None, importer: str = "beets"
) -> Service:
    service = a_service(tmp_path, layout=a_layout(tmp_path))
    service.settings = Settings(library=str(tmp_path / "lib"), importer=importer)
    service.runner = runner or FakeRunner(installed={"beet"}).expect(
        "import", stdout=MATCH
    )
    return service


class Moving(FakeRunner):
    """A beets that takes the tracks, the way `move: yes` does."""

    def __init__(self, staged: Path, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.staged = staged

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if "import" in args and stdin:
            for f in self.staged.glob("*.flac"):
                f.unlink()
        return super().run(argv, stdin=stdin, timeout=timeout)


def upload(service: Service, blob: bytes) -> H.Response:
    post(
        build(service),
        "/api/precut/begin",
        service,
        {"name": "a.zip", "size": len(blob)},
    )
    for at in range(0, len(blob), CHUNK):
        build(service).dispatch(
            H.Request.of(
                "POST",
                f"/api/precut/chunk?at={at}",
                headers=signed_in(service),
                body=blob[at : at + CHUNK],
            )
        )
    return post(build(service), "/api/precut/finish", service, {})


def test_an_album_unpacks_and_beets_is_asked_what_it_is(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    body = upload(service, an_album()).json()

    assert not body["error"], body["error"]
    got = body["result"]
    assert got["tracks"] == ["01 One.flac", "02 Two.flac"]
    assert got["art"] == ["cover.jpg"]
    assert got["mbid"] == MBID


def test_asking_what_it_is_moves_nothing(tmp_path: Path) -> None:
    """beets is run with stdin closed, so it cannot have taken anything."""
    service = a_precut_service(tmp_path)
    upload(service, an_album())
    assert service.runner.fed == [b""]  # type: ignore[union-attr]


def test_a_file_that_is_not_an_album_is_refused(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    body = upload(service, b"fLaC" + b"\x00" * 200).json()
    assert "not a zip" in body["error"], body["error"]


def test_a_short_upload_is_refused_before_any_work(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    post(build(service), "/api/precut/begin", service, {"name": "a.zip", "size": 999})
    r = post(build(service), "/api/precut/finish", service, {})
    assert r.status == 409 and "send the rest" in r.json()["error"]


def test_taking_it_needs_the_release_that_was_shown(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    upload(service, an_album())
    assert post(build(service), "/api/precut/take", service, {}).status == 400


def test_taking_it_with_nothing_unpacked_is_refused(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    r = post(build(service), "/api/precut/take", service, {"mbid": MBID})
    assert r.status == 409 and "upload an album first" in r.json()["error"]


def test_the_upload_is_cleared_only_once_the_library_holds_it(
    tmp_path: Path,
) -> None:
    lib = tmp_path / "lib" / "A Band" / "A Record"
    rows = f"A Record\x1f{lib}/01 One.flac\n"
    service = a_precut_service(tmp_path)
    staged = service.layout.work / "precut"
    service.runner = (
        Moving(staged, installed={"beet"})
        .expect(lambda a: "import" in a, stdout=MATCH)
        .expect("ls", stdout=rows.encode())
    )
    upload(service, an_album())
    lib.mkdir(parents=True)
    (lib / "01 One.flac").write_bytes(b"a")

    body = post(build(service), "/api/precut/take", service, {"mbid": MBID}).json()

    assert not body["error"], body["error"]
    assert IN.state(service.layout, HELD) is None
    assert not staged.exists()


def test_a_track_left_behind_keeps_the_upload(tmp_path: Path) -> None:
    """Nothing is cleared until the record is proved to be somewhere else."""
    service = a_precut_service(
        tmp_path,
        FakeRunner(installed={"beet"})
        .expect(lambda a: "import" in a, stdout=b"Skipping album")
        .expect("ls", stdout=b""),
    )
    upload(service, an_album())
    body = post(build(service), "/api/precut/take", service, {"mbid": MBID}).json()

    assert "still waiting" in body["error"]
    assert IN.state(service.layout, HELD) is not None
    assert (service.layout.work / "precut" / "01 One.flac").is_file()


def test_the_fallback_tagger_cannot_do_this(tmp_path: Path) -> None:
    """It writes the tags it is given and has no matcher, so there is nothing
    for it to do here - said plainly rather than failing mid-job."""
    service = a_precut_service(
        tmp_path, FakeRunner(installed={"metaflac"}), importer="tagger"
    )
    r = post(build(service), "/api/precut/take", service, {"mbid": MBID})
    assert r.status == 409 and "needs beets" in r.json()["error"]


def test_cancelling_drops_the_upload_and_the_unpacked_files(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    upload(service, an_album())
    assert post(build(service), "/api/precut/cancel", service, {}).status == 200
    assert IN.state(service.layout, HELD) is None
    assert not (service.layout.work / "precut").exists()


def test_the_state_says_what_is_waiting(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    upload(service, an_album())
    got = get(build(service), "/api/precut", service).json()

    assert got["chunk"] == CHUNK
    assert got["staged"] == ["01 One.flac", "02 Two.flac", "cover.jpg"]
    assert got["upload"]["name"] == "a.zip"


def test_nothing_is_waiting_on_a_clean_pool(tmp_path: Path) -> None:
    service = a_precut_service(tmp_path)
    got = get(build(service), "/api/precut", service).json()
    assert got["upload"] is None and got["staged"] == []
