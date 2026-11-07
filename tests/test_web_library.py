"""The catalogue, the first pass, and getting a record into the library.

Nothing here reaches the network: the fetcher goes through the same kind of
seam as the external tools, so the whole first pass runs from a scripted reply.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio import session as S
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core.plan import Plan
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.integrations.importer import _SEP as I_SEP
from ripdoctor.store import archive as AR
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
from ripdoctor.web import auth as A
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.routes.library import spec_from
from ripdoctor.web.service import Service
from ripdoctor.work import capture as W
from tests.beets_double import Library
from tests.pool import a_layout, a_runner, quiet_then_loud
from tests.test_web_routes import a_service, get, post

STATIC_JS = Path(__file__).resolve().parent.parent / "ripdoctor/web/static/app.js"


class Catalogue:
    """Answers from a script, and records what was asked."""

    def __init__(self, *replies: bytes | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append(url)
        reply = self.replies.pop(0) if self.replies else b"{}"
        if isinstance(reply, Exception):
            raise reply
        return reply


SEARCH = json.dumps(
    {
        "releases": [
            {
                "id": "aaa",
                "title": "A Record",
                "date": "2022",
                "artist-credit": [{"name": "A Band"}],
                "media": [{"format": "Vinyl"}],
            }
        ]
    }
).encode()


def release(lengths: list[int | None]) -> bytes:
    return json.dumps(
        {
            "media": [
                {
                    "format": "Vinyl",
                    "tracks": [
                        {"title": f"Track {i + 1}", "length": ms}
                        for i, ms in enumerate(lengths)
                    ],
                }
            ]
        }
    ).encode()


def a_catalogue_service(tmp_path: Path, *replies: bytes):  # type: ignore[no-untyped-def]
    service = a_service(tmp_path)
    service.fetcher = Catalogue(*replies)  # type: ignore[assignment]
    return service


# -------------------------------------------------------------- searching


def test_a_search_returns_ranked_releases(tmp_path: Path) -> None:
    service = a_catalogue_service(tmp_path, SEARCH, release([150000, 200000]))
    r = get(build(service), "/api/mb/search?artist=A+Band&album=A+Record", service)
    body = r.json()
    assert body["releases"][0]["id"] == "aaa"
    assert body["releases"][0]["has_durations"]


def test_an_entry_with_no_durations_is_marked(tmp_path: Path) -> None:
    """Vinyl entries are frequently bulk clones of a digital tracklist. Fitting
    against zeros produces a plan that looks right and is wrong everywhere."""
    service = a_catalogue_service(tmp_path, SEARCH, release([None, None]))
    body = get(build(service), "/api/mb/search?artist=A&album=B", service).json()
    assert not body["releases"][0]["has_durations"]
    assert "NO DURATIONS" in body["releases"][0]["describe"]


def test_a_search_needs_both_halves(tmp_path: Path) -> None:
    service = a_catalogue_service(tmp_path)
    assert get(build(service), "/api/mb/search?artist=A", service).status == 400


def test_a_catalogue_that_is_down_is_a_503_not_a_500(tmp_path: Path) -> None:
    """It answers 503 under load, and that is not this server's failure."""
    import io
    import urllib.error

    err = urllib.error.HTTPError("u", 404, "no", {}, io.BytesIO(b""))  # type: ignore[arg-type]
    service = a_catalogue_service(tmp_path, err)
    r = get(build(service), "/api/mb/search?artist=A&album=B", service)
    assert r.status == 503


# ------------------------------------------------------------ first pass


def prepared_service(tmp_path: Path, *replies: bytes, sides=("a", "b")):  # type: ignore[no-untyped-def]
    layout = a_layout(tmp_path, sides=sides)
    service = a_service(tmp_path, layout=layout)
    windows = int(6.0 / C.WINDOW)
    service.runner = a_runner(
        windows=windows, seconds=6.0, levels=quiet_then_loud(windows)
    )
    for letter in sides:
        C.build(service.runner, layout, "album", letter, dwell=0.0)
    service.fetcher = Catalogue(*replies)  # type: ignore[assignment]
    return service


def test_a_first_pass_writes_a_spec_and_a_plan(tmp_path: Path) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    r = post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    assert r.status == 202, r.json()
    body = r.json()
    assert not body["error"], body["error"]
    assert body["result"]["tracks"] == 4
    assert service.layout.plan_file("album").is_file()
    assert service.layout.spec_file("album").is_file()


def test_the_tracklist_is_laid_across_the_sides_that_were_recorded(
    tmp_path: Path,
) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    spec = F.read_spec(service.layout.spec_file("album"))
    assert [s.letter for s in spec.sides] == ["a", "b"]
    assert sum(len(s.tracks) for s in spec.sides) == 4


def test_a_release_with_no_durations_is_refused(tmp_path: Path) -> None:
    service = prepared_service(tmp_path, release([None, None]))
    body = post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"}).json()
    assert "durations" in body["error"]
    assert not service.layout.plan_file("album").exists()


def test_a_first_pass_needs_a_prepared_side(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.fetcher = Catalogue(release([2000, 2000]))  # type: ignore[assignment]
    body = post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"}).json()
    assert "not prepared" in body["error"]


def test_a_first_pass_without_a_release_is_refused(tmp_path: Path) -> None:
    service = prepared_service(tmp_path)
    r = post(build(service), "/api/firstpass/album", service, {})
    assert r.status == 400


# ------------------------------------------------------- laying out sides


def test_the_split_across_sides_is_chosen_whole_not_greedily() -> None:
    """Putting one track on the wrong side displaces every side after it."""
    tracks = [MB.Track(i + 1, f"T{i + 1}", 100.0) for i in range(6)]
    rel = MB.Release("x", "A", "B", "2022", "Vinyl", tracks)
    spec = spec_from("album", rel, [("a", 0.0, 300.0), ("b", 0.0, 300.0)])
    assert [len(s.tracks) for s in spec.sides] == [3, 3]


def test_uneven_sides_get_uneven_tracklists() -> None:
    tracks = [MB.Track(i + 1, f"T{i + 1}", 100.0) for i in range(6)]
    rel = MB.Release("x", "A", "B", "2022", "Vinyl", tracks)
    spec = spec_from("album", rel, [("a", 0.0, 400.0), ("b", 0.0, 200.0)])
    assert [len(s.tracks) for s in spec.sides] == [4, 2]


# --------------------------------------------------------------- library


def test_the_library_root_is_reported(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.settings = replace(service.settings, library=str(tmp_path / "music"))
    body = get(build(service), "/api/library", service).json()
    assert body["library"].endswith("music") and not body["exists"]


def test_importing_without_a_library_is_refused(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    service.settings = replace(service.settings, library="")
    F.write_json(service.layout.plan_file("album"), a_plan_dict())
    assert post(build(service), "/api/import/album", service).status == 409


def ready_to_import(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A record whose tracks are cut and waiting in review."""
    service, library = with_library(tmp_path)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True, exist_ok=True)
    (review / "01 One.flac").write_bytes(b"fLaC" + b"\x00" * 2000)
    return service, library


def test_importing_nothing_is_refused_rather_than_reported_as_done(
    tmp_path: Path,
) -> None:
    """With review empty the importer finds nothing to move, reports no
    failure, and the summary reads off whatever is already in the library - so
    an import that did nothing looked like one that worked."""
    service, _library = with_library(tmp_path)
    r = post(build(service), "/api/import/album", service)
    assert r.status == 409 and "cut the tracks first" in r.json()["error"]


def test_the_release_chosen_in_the_dialog_is_the_one_imported(
    tmp_path: Path,
) -> None:
    """It used to come from the spec alone, so picking one and pressing Import
    imported the release picked before it."""
    service, _library = ready_to_import(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    rows = every_row("/music/A Band/A Record/01 One.flac")
    service.runner = FakeRunner(installed={"beet"}).expect("ls", stdout=rows.encode())
    F.remember(service.layout, "album", album="A Record", artist="A Band")

    # The importer moves nothing here, so review is still full and the run
    # reports a failure - what matters is which release it was told to use.
    post(build(service), "/api/import/album", service, {"mbid": "chosen-here"})
    argv = service.runner.argv_for("import")
    assert "--search-id" in argv and "chosen-here" in argv


def a_plan_dict() -> dict:
    return {
        "slug": "album",
        "album": "A Record",
        "artist": "A Band",
        "date": "2022",
        "sides": [
            {
                "file": "side-a.flac",
                "tracks": [
                    {"number": 1, "title": "One", "start": 1.0, "end": 9.0, "cat": 8.0}
                ],
            }
        ],
    }


# --------------------------------------------------------------- archive


def every_row(path: str) -> str:
    """Beets' answer to being asked for every row: this album, this file."""
    fields = ("T", "A Band", "A Record", "3:00", path, "", "1", "", "A Band", "", "")
    return I_SEP.join(fields)


def with_library(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A pool and a library, filed by the built-in tagger.

    These are about the archive gate and the import step rather than about
    beets, so the importer that puts files where this project would put them is
    the one to ask.
    """
    service = a_service(tmp_path)
    library = tmp_path / "music"
    service.settings = replace(
        service.settings, library=str(library), importer="tagger"
    )
    F.write_json(service.layout.plan_file("album"), a_plan_dict())
    return service, library


def test_the_gate_says_what_is_missing(tmp_path: Path) -> None:
    """It is what stops twenty minutes a side being cleared before the record
    is provably somewhere else."""
    service, _library = with_library(tmp_path)
    body = get(build(service), "/api/archive/album", service).json()
    assert not body["ready"] and body["expected"] == 1
    assert body["library_tracks"] == 0 and "not in the library" in body["why"]


def test_archiving_before_the_record_arrived_is_refused(tmp_path: Path) -> None:
    service, _library = with_library(tmp_path)
    r = post(build(service), "/api/archive/album", service)
    assert r.status == 409
    assert (service.layout.raw / "album").is_dir(), "raw was cleared anyway"


class Archiving(FakeRunner):
    """Sides that decode, and an ffmpeg that leaves what it re-encodes."""

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if args[0] == "ffmpeg" and "-progress" in args:
            from ripdoctor.audio.runner import Result

            return Result(tuple(args), 0, b"out_time_us=1200000000\n", b"")
        if args[0] == "ffmpeg" and args[-1].endswith(".flac"):
            Path(args[-1]).write_bytes(b"fLaC" + b"\x00" * 4000)
        return super().run(argv, stdin=stdin, timeout=timeout)


def arrived(tmp_path: Path):  # type: ignore[no-untyped-def]
    service, library = with_library(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    (placed / "01 One.flac").write_bytes(b"fLaC")
    service.runner = Archiving()
    return service, library


def test_a_record_that_arrived_is_archived_and_read_back(tmp_path: Path) -> None:
    service, _library = arrived(tmp_path)
    r = post(build(service), "/api/archive/album", service)
    assert r.status == 202 and not r.json()["error"], r.json()["error"]
    assert (service.layout.archive / "album" / "side-a.flac").is_file()
    assert not (service.layout.raw / "album").exists()


def test_archiving_keeps_what_the_record_is_called(tmp_path: Path) -> None:
    """An archived record still exists. Both documents stay in work, which is
    what lets it be listed, re-cut, aligned against and punched into."""
    service, _library = arrived(tmp_path)
    F.remember(service.layout, "album", album="Second", artist="First")
    post(build(service), "/api/archive/album", service)
    assert service.layout.spec_file("album").is_file()
    assert service.layout.plan_file("album").is_file()
    listed = get(build(service), "/api/albums?archive=1", service).json()["albums"][0]
    assert listed["where"] == "archive" and listed["artist"]


def test_what_is_cleared_is_only_what_can_be_made_again(tmp_path: Path) -> None:
    """The cut tracks, the tick clips and the measurements. All derived, all
    large, and all of them fill the disk if nothing clears them."""
    service, _library = arrived(tmp_path)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True)
    (review / "01 One.flac").write_bytes(b"fLaC")
    body = post(build(service), "/api/archive/album", service).json()
    assert not review.exists()
    assert len(body["result"]["removed"]) >= 2


def test_a_truncated_side_is_re_encoded_rather_than_copied(tmp_path: Path) -> None:
    """A stream ended with a signal has no length in its header, and archiving
    it as-is preserves that for as long as the file exists."""
    service, _library = arrived(tmp_path)
    service.runner = Archiving().expect(
        lambda a: a[0] == "flac" and "raw" in a[-1], returncode=1
    )
    body = post(build(service), "/api/archive/album", service).json()
    assert not body["error"], body["error"]
    assert "re-encoded" in body["result"]["notes"][0]


def test_a_side_that_does_not_read_back_stops_everything(tmp_path: Path) -> None:
    """Nothing is removed until every side has been read from where it will
    live."""
    service, _library = arrived(tmp_path)
    service.runner = Archiving().expect(
        lambda a: a[0] == "flac" and "archive" in a[-1], returncode=1
    )
    body = post(build(service), "/api/archive/album", service).json()
    assert "did not read back" in body["error"]
    assert (service.layout.raw / "album" / "side-a.flac").is_file()


def rerip(service: Service, *, filed: bool = True) -> bytes:
    """Archive a record, then put a newer capture of one side in raw, by default one
    the library's tracks have since been cut from and filed."""
    post(build(service), "/api/archive/album", service)
    first = (service.layout.archive / "album" / "side-a.flac").read_bytes()
    (service.layout.raw / "album").mkdir()
    again = service.layout.raw / "album" / "side-a.flac"
    again.write_bytes(b"fLaC" + b"\x00" * 99)
    if filed:
        AR.note_filed(service.layout, "album", {again.name: C.stamp_of(again)})
    return first


def test_a_re_rip_the_library_was_not_cut_from_cannot_cost_the_earlier_capture(
    tmp_path: Path,
) -> None:
    """Measured in a browser: the earlier import counted in full, the rail read Ready,
    and pressing Archive replaced the only capture those tracks were cut from."""
    service, _library = arrived(tmp_path)
    first = rerip(service, filed=False)
    body = get(build(service), "/api/archive/album", service).json()
    assert not body["ready"] and "captured again" in body["why"]
    assert "side a" in body["why"] and body["captured_again"]
    r = post(build(service), "/api/archive/album", service)
    assert r.status == 409 and "captured again" in r.json()["error"]
    assert (service.layout.archive / "album" / "side-a.flac").read_bytes() == first
    assert (service.layout.raw / "album" / "side-a.flac").is_file()


def test_a_proof_of_an_older_capture_does_not_cover_a_newer_one(tmp_path: Path) -> None:
    service, _library = arrived(tmp_path)
    rerip(service)
    again = service.layout.raw / "album" / "side-a.flac"
    again.write_bytes(b"fLaC" + b"\x00" * 123)
    assert (
        "captured again"
        in get(build(service), "/api/archive/album", service).json()["why"]
    )


def test_a_plan_on_another_capture_shuts_the_gate(tmp_path: Path) -> None:
    """Rows reading `From an earlier capture` under a rail reading Ready."""
    service, _library = arrived(tmp_path)
    doc = a_plan_dict()
    doc["sides"][0]["audio"] = "some-other-capture"
    F.write_json(service.layout.plan_file("album"), doc)
    body = get(build(service), "/api/archive/album", service).json()
    assert not body["ready"] and "captured again" in body["why"]


def test_an_import_records_the_capture_its_tracks_came_from(tmp_path: Path) -> None:
    service, _library = ready_to_import(tmp_path)
    service.runner = a_runner()
    raw = service.layout.raw / "album" / "side-a.flac"
    raw.write_bytes(b"fLaC")
    os.utime(raw, (1000, 1000))
    doc = a_plan_dict()
    doc["sides"][0]["audio"] = C.stamp_of(raw)
    F.write_json(service.layout.plan_file("album"), doc)
    stamp = C.stamp_of(raw)
    r = post(build(service), "/api/import/album", service, {})
    assert not r.json()["error"], r.json()["error"]
    assert json.loads(service.layout.filed_file("album").read_text()) == {
        "side-a.flac": stamp
    }


def test_a_cut_older_than_the_capture_proves_nothing_about_it(tmp_path: Path) -> None:
    """Cut, captured again, saved so the plan's stamp moved, then imported: the tracks
    are the old capture's whatever the plan now says."""
    service, _library = ready_to_import(tmp_path)
    raw = service.layout.raw / "album" / "side-a.flac"
    raw.write_bytes(b"fLaC")
    doc = a_plan_dict()
    doc["sides"][0]["audio"] = C.stamp_of(raw)
    F.write_json(service.layout.plan_file("album"), doc)
    cut = service.layout.review_dir("album") / "01 One.flac"
    os.utime(cut, (raw.stat().st_mtime - 60, raw.stat().st_mtime - 60))
    plan = F.read_plan(service.layout.plan_file("album"))
    assert AR.cut_captures(service.layout, "album", plan) == {}


def test_the_rail_says_why_a_record_was_not_archived_and_offers_one_press() -> None:
    js = STATIC_JS.read_text()
    step = js[js.index("function railArchive(") :]
    step = step[: step.index("\n}\n")]
    assert "archive_failed" in step and "are cleared unfiled" not in step
    cut = js[js.index("function railCut(") :]
    assert "f.arc?.captured_again" in cut[: cut.index("\n}\n")]
    assert '.replace(/\\.$/, "")' in step


def test_a_record_that_was_archived_before_can_be_archived_again(
    tmp_path: Path,
) -> None:
    """A re-rip of one side of a finished record has to reach the end. Refusing
    leaves it a step short with nothing in the page to resolve it."""
    service, _library = arrived(tmp_path)
    first = rerip(service)
    body = post(build(service), "/api/archive/album", service).json()
    assert not body["error"], body["error"]
    assert (service.layout.archive / "album" / "side-a.flac").read_bytes() != first


def test_the_earlier_take_is_moved_aside_once_the_new_one_has_read_back(
    tmp_path: Path,
) -> None:
    """Nobody is asked before an import archives, so nothing is final. ADR-065."""
    service, _library = arrived(tmp_path)
    first = rerip(service)
    post(build(service), "/api/archive/album", service).json()
    aside = list((service.layout.archive / "album" / "_superseded").iterdir())
    assert [p.read_bytes() for p in aside] == [first]


def test_the_earlier_take_is_kept_when_the_setting_asks_for_it(
    tmp_path: Path,
) -> None:
    """Comparing two cartridges is a real reason to want both."""
    service, _library = arrived(tmp_path)
    service.settings = replace(service.settings, keep_superseded=True)
    first = rerip(service)
    body = post(build(service), "/api/archive/album", service).json()
    kept = sorted((service.layout.archive / "album" / "_superseded").glob("*.flac"))
    assert len(kept) == 1 and kept[0].read_bytes() == first
    assert any("_superseded" in n for n in body["result"]["notes"])


def test_the_preview_names_the_sides_it_will_replace(tmp_path: Path) -> None:
    """Said before the irreversible step, not reported after it."""
    service, _library = arrived(tmp_path)
    rerip(service)
    body = get(build(service), "/api/archive/album", service).json()
    assert body["will_replace"] == ["side-a.flac"]


# --------------------------------------------------------------- artwork

JPEG = b"\xff\xd8\xff" + b"\x00" * 4000


def probed(width: int = 1000, height: int = 1000) -> bytes:
    return json.dumps(
        {"streams": [{"width": width, "height": height, "codec_name": "mjpeg"}]}
    ).encode()


class Imaging(FakeRunner):
    """An ffmpeg that writes the cover it was asked to scale."""

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if args[0] == "ffmpeg" and args[-1].endswith(".jpg"):
            Path(args[-1]).write_bytes(JPEG)
        return super().run(argv, stdin=stdin, timeout=timeout)


def in_the_library(tmp_path: Path, image: bytes | None = None):  # type: ignore[no-untyped-def]
    service, library = with_library(tmp_path)
    album = library / "A Band" / "A Record"
    album.mkdir(parents=True)
    (album / "01 One.flac").write_bytes(b"fLaC" + b"\x00" * 2000)
    service.runner = Imaging().expect(
        "ffprobe", stdout=image if image is not None else probed()
    )
    service.fetcher = Catalogue(JPEG)  # type: ignore[assignment]
    return service, album


def test_the_art_a_record_already_has_is_reported(tmp_path: Path) -> None:
    service, album = in_the_library(tmp_path)
    (album / "cover.jpg").write_bytes(JPEG)
    body = get(build(service), "/api/artwork/album", service).json()
    assert body["cover"]["name"] == "cover.jpg"
    assert len(body["tracks"]) == 1


def uploading(service, body: bytes = b""):  # type: ignore[no-untyped-def]
    """The image is the body itself, so this cannot go through `post`."""
    return build(service).dispatch(
        H.Request.of(
            "POST",
            "/api/artwork/album/upload",
            headers={"Cookie": f"{A.COOKIE}={service.sessions.issue('listener')}"},
            body=body,
        )
    )


def test_asking_about_a_record_with_nothing_cut_says_so(tmp_path: Path) -> None:
    service, _library = with_library(tmp_path)
    r = get(build(service), "/api/artwork/album", service)
    assert r.status == 409 and "not filed yet" in r.json()["error"]


def test_art_can_be_given_to_a_record_before_it_is_imported(tmp_path: Path) -> None:
    """A record with no release in the catalogue gets no cover automatically,
    which is exactly the record somebody has to supply one for. Requiring the
    library copy meant that could only be done after an import."""
    service, _library = with_library(tmp_path)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True)
    (review / "01 One.flac").write_bytes(b"fLaC" + b"\x00" * 2000)
    service.runner = Imaging().expect("ffprobe", stdout=probed())
    r = uploading(service, JPEG)
    assert r.status == 200, r.json()
    assert (review / "cover.jpg").is_file()


def test_the_cuts_win_while_they_are_still_in_review(tmp_path: Path) -> None:
    """beets is commonly set to take a cover from the folder it is importing,
    so art put there travels in with the tracks. After an import there is no
    review copy left and the library one is the only one."""
    service, album = in_the_library(tmp_path)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True)
    (review / "01 One.flac").write_bytes(b"fLaC" + b"\x00" * 2000)
    assert uploading(service, JPEG).status == 200
    assert (review / "cover.jpg").is_file()

    for cut in review.glob("*.flac"):
        cut.unlink()
    assert uploading(service, JPEG).status == 200
    assert (album / "cover.jpg").is_file()


def test_candidates_are_offered_with_their_sizes(tmp_path: Path) -> None:
    service, _album = in_the_library(tmp_path)
    r = post(build(service), "/api/artwork/album/search", service, {"mbid": "aaa"})
    assert r.json()["candidates"][0]["big_enough"]


def test_installing_from_a_url_writes_and_embeds(tmp_path: Path) -> None:
    service, album = in_the_library(tmp_path)
    r = post(
        build(service),
        "/api/artwork/album/install",
        service,
        {"url": "https://coverartarchive.org/release/aaa/front"},
    )
    assert r.status == 200 and r.json()["embedded"] == 1
    assert (album / "cover.jpg").is_file()


def test_an_address_that_is_not_https_is_refused(tmp_path: Path) -> None:
    service, _album = in_the_library(tmp_path)
    r = post(
        build(service),
        "/api/artwork/album/install",
        service,
        {"url": "http://example.invalid/cover.jpg"},
    )
    assert r.status == 400


def test_an_uploaded_image_gets_the_same_verification(tmp_path: Path) -> None:
    service, album = in_the_library(tmp_path)
    app = build(service)
    r = app.dispatch(
        H.Request.of(
            "POST",
            "/api/artwork/album/upload",
            headers={"Cookie": f"{A.COOKIE}={service.sessions.issue('listener')}"},
            body=JPEG,
        )
    )
    assert r.status == 200 and (album / "cover.jpg").is_file()


def test_an_error_page_never_reaches_the_files(tmp_path: Path) -> None:
    """The expensive failure: art removed, then the replacement turning out to
    be a 170-byte HTML page."""
    service, album = in_the_library(tmp_path)
    (album / "cover.jpg").write_bytes(b"the existing cover")
    service.runner = Imaging().expect("ffprobe", returncode=1)
    r = post(
        build(service),
        "/api/artwork/album/install",
        service,
        {"url": "https://coverartarchive.org/release/aaa/front"},
    )
    assert r.status == 400
    assert (album / "cover.jpg").read_bytes() == b"the existing cover"


def test_the_best_cover_is_found_and_installed_in_one_step(tmp_path: Path) -> None:
    """The release the record was fitted against is in the spec, which is the
    only reason the catalogue can be asked again later."""
    service, album = in_the_library(tmp_path, image=probed(1500, 1500))
    F.save(
        service.layout,
        "album",
        replace(F.spec_of(Plan.from_dict(a_plan_dict())), mbid="aaa"),
        Plan.from_dict(a_plan_dict()),
    )
    service.fetcher = Catalogue(JPEG, JPEG)  # type: ignore[assignment]
    r = post(build(service), "/api/artwork/album/fetch", service, {})
    assert r.status == 200 and (album / "cover.jpg").is_file()
    assert r.json()["source"] == "release" and r.json()["embedded"] == 1


def test_fetching_without_a_release_says_what_to_do(tmp_path: Path) -> None:
    service, _album = in_the_library(tmp_path)
    r = post(build(service), "/api/artwork/album/fetch", service, {})
    assert r.status == 409 and "first pass" in r.json()["error"]


def test_nothing_big_enough_is_reported_rather_than_installed(
    tmp_path: Path,
) -> None:
    """A small cover is worse than the one a player already shows for a record
    with none."""
    service, album = in_the_library(tmp_path, image=probed(316, 316))
    F.save(
        service.layout,
        "album",
        replace(F.spec_of(Plan.from_dict(a_plan_dict())), mbid="aaa"),
        Plan.from_dict(a_plan_dict()),
    )
    service.fetcher = Catalogue(JPEG, JPEG)  # type: ignore[assignment]
    r = post(build(service), "/api/artwork/album/fetch", service, {})
    assert r.status == 404 and "nothing usable" in r.json()["error"]
    assert not (album / "cover.jpg").exists()


# -------------------------------------------------------------- relabel


def with_a_cut(tmp_path: Path, *replies: bytes):  # type: ignore[no-untyped-def]
    service, library = with_library(tmp_path)
    plan = Plan.from_dict(a_plan_dict())
    F.save(service.layout, "album", F.spec_of(plan), plan)
    service.fetcher = Catalogue(*replies)  # type: ignore[assignment]
    return service, library


def test_titles_can_be_taken_from_another_release_without_moving_anything(
    tmp_path: Path,
) -> None:
    """A release picked at import time is usually picked because the first one
    was wrong. Re-fitting by then throws away boundaries that are already
    correct and hard-won."""
    service, _library = with_a_cut(tmp_path, release([200000]))
    before = F.read_plan(service.layout.plan_file("album")).sides[0].tracks[0]
    r = post(build(service), "/api/relabel/album", service, {"mbid": "bbb"})
    assert r.status == 200 and r.json()["applied"] == 1
    after = F.read_plan(service.layout.plan_file("album")).sides[0].tracks[0]
    assert after.title == "Track 1" and after.title != before.title
    assert after.start == before.start and after.end == before.end


def test_the_release_id_is_recorded_so_art_can_be_found_later(
    tmp_path: Path,
) -> None:
    service, _library = with_a_cut(tmp_path, release([200000]))
    post(build(service), "/api/relabel/album", service, {"mbid": "bbb"})
    assert F.read_spec(service.layout.spec_file("album")).mbid == "bbb"


def test_a_release_with_a_different_number_of_tracks_is_refused(
    tmp_path: Path,
) -> None:
    """Re-labelling one of those would shift every title by one."""
    service, _library = with_a_cut(tmp_path, release([200000, 200000]))
    r = post(build(service), "/api/relabel/album", service, {"mbid": "bbb"})
    assert r.status == 409 and "shift every title" in r.json()["error"]


def test_relabelling_a_record_with_no_cut_is_a_404(tmp_path: Path) -> None:
    service, _library = with_library(tmp_path)
    service.fetcher = Catalogue(release([200000]))  # type: ignore[assignment]
    (service.layout.plan_file("album")).unlink()
    assert (
        post(build(service), "/api/relabel/album", service, {"mbid": "b"}).status == 404
    )


# -------------------------------------------------------------- existing


def test_a_record_already_in_the_library_is_reported_before_the_import(
    tmp_path: Path,
) -> None:
    """Finding out afterwards means finding out from a directory holding two
    copies."""
    service, library = with_library(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    (placed / "01 One.flac").write_bytes(b"fLaC")
    body = get(build(service), "/api/library/existing/album", service).json()
    assert body["existing"]["tracks"] == 1


def test_a_blank_release_asks_about_the_names_and_not_the_specs_release(
    tmp_path: Path,
) -> None:
    """The dialog sends `mbid=` for a from-plan import. Read as no answer, the panel
    named the spec's edition while a Replace from the plan would refuse over both."""
    service, _library = with_library(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    pinned(service, "aaa")
    there = tmp_path / "01 One.flac"
    there.write_bytes(b"fLaC")
    rows = [
        I_SEP.join(
            (
                "T",
                "A Band",
                "A Record",
                "3:00",
                f"{there}",
                "",
                "1",
                rel,
                "A Band",
                "",
                "",
            )
        )
        for rel in ("aaa", "bbb")
    ]
    service.runner = FakeRunner(installed={"beet"}).expect(
        "ls", stdout="\n".join(rows).encode()
    )
    app = build(service)
    blank = get(app, "/api/library/existing/album?mbid=", service).json()["existing"]
    assert blank["tracks"] == 2 and "2 different releases" in blank["refused"]
    silent = get(app, "/api/library/existing/album", service).json()["existing"]
    assert silent["tracks"] == 1 and not silent["refused"]


def test_stale_rows_are_reported_before_the_import(tmp_path: Path) -> None:
    """They read as a copy already there and stop an import part-way, with a
    message from beets that does not say why."""
    service, _library = with_library(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    gone = tmp_path / "gone" / "01 One.flac"
    service.runner = FakeRunner(installed={"beet"}).expect(
        "ls",
        stdout=I_SEP.join(
            (
                "T",
                "A Band",
                "A Record",
                "3:00",
                f"{gone}",
                "",
                "1",
                "",
                "A Band",
                "",
                "",
            )
        ).encode(),
    )
    body = get(build(service), "/api/library/existing/album", service).json()
    assert body["stale"]["tracks"] == 1
    assert "deleted outside beets" in body["stale"]["note"]


def test_asking_what_the_library_holds_lists_it_once(tmp_path: Path) -> None:
    """Every Import dialog and every change of release asks this, and two whole
    listings were twice the wait: 0.3 s on 1,000 rows and 3 s on 20,000, measured."""
    service, _library = with_library(tmp_path)
    filed_as(service, tmp_path, "aaa")
    get(build(service), "/api/library/existing/album?mbid=", service)
    assert len([c for c in service.runner.calls if "ls" in c]) == 1


def test_stale_rows_are_cleared_only_when_every_file_is_absent(
    tmp_path: Path,
) -> None:
    service, _library = with_library(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    there = tmp_path / "01 One.flac"
    there.write_bytes(b"fLaC")
    service.runner = FakeRunner(installed={"beet"}).expect(
        "ls",
        stdout=I_SEP.join(
            (
                "T",
                "A Band",
                "A Record",
                "3:00",
                f"{there}",
                "",
                "1",
                "",
                "A Band",
                "",
                "",
            )
        ).encode(),
    )
    r = post(build(service), "/api/library/clear-stale/album", service)
    assert r.status == 409 and "still there" in r.json()["error"]
    assert not [c for c in service.runner.calls if "remove" in c]


def test_a_record_that_is_not_there_reports_nothing(tmp_path: Path) -> None:
    service, _library = with_library(tmp_path)
    body = get(build(service), "/api/library/existing/album", service).json()
    assert body["existing"] is None


def test_a_record_with_no_cut_is_not_ready_rather_than_missing(
    tmp_path: Path,
) -> None:
    """A record captured and not yet cut is the ordinary state. The gate
    answers "no, and here is why" rather than a 404 the page has to swallow -
    which it does silently, so the reason never reaches anybody."""
    service, _library = with_library(tmp_path)
    service.layout.plan_file("album").unlink()
    r = get(build(service), "/api/archive/album", service)
    assert r.status == 200
    body = r.json()
    assert body["ready"] is False and "first pass" in body["why"]
    assert body["sides"] == [] and body["will_remove"] == []


def test_re_labelling_a_record_with_only_a_name_is_refused(tmp_path: Path) -> None:
    """A spec with no sides would keep none of the new titles while the plan
    took all of them, and the two documents would stay diverged."""
    service, _library = with_library(tmp_path)
    F.remember(service.layout, "album", album="Second", artist="First")
    r = post(build(service), "/api/relabel/album", service, {"mbid": "x"})
    assert r.status == 409 and "no saved cut" in r.json()["error"]
    assert json.loads(service.layout.spec_file("album").read_text())["sides"] == []


def test_art_finds_the_album_when_only_the_spec_carries_its_name(
    tmp_path: Path,
) -> None:
    """A library path built from whichever document happened to be read points
    at Unknown Artist the moment that one is blank, and the record is then
    reported as not being in a library it is plainly in."""
    service, library = with_library(tmp_path)
    album = library / "A Band" / "A Record"
    album.mkdir(parents=True)
    (album / "01 One.flac").write_bytes(b"fLaC" + b"\x00" * 2000)
    F.write_json(
        service.layout.plan_file("album"),
        {"slug": "album", "album": "", "artist": "", "date": "", "sides": []},
    )
    F.remember(service.layout, "album", album="A Record", artist="A Band")
    service.runner = Imaging().expect("ffprobe", stdout=probed())
    assert uploading(service, JPEG).status == 200
    assert (album / "cover.jpg").is_file()


# --------------------------------------------------- the escape hatch


def pinned(service: Service, mbid: str) -> None:
    """A record whose spec names a release, as a first pass leaves it."""
    layout = service.layout
    F.remember(layout, "album", album="A Record", artist="A Band", date="2022")
    spec = F.read_spec(layout.spec_file("album"))
    F.save(
        layout,
        "album",
        replace(spec, mbid=mbid),
        F.read_plan(layout.plan_file("album")),
    )


def importing_with(tmp_path: Path, body: dict) -> FakeRunner:
    service, _library = ready_to_import(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    service.runner = FakeRunner(installed={"beet", "metaflac"}).expect("ls", stdout=b"")
    pinned(service, "from-the-spec")
    post(build(service), "/api/import/album", service, body)
    return service.runner


def test_an_empty_release_in_the_dialog_means_no_release(tmp_path: Path) -> None:
    """Falling back on the spec made the no-catalogue path unreachable for any
    record a first pass had touched, which is almost all of them."""
    argv = importing_with(tmp_path, {"mbid": ""}).argv_for("import")
    assert "--search-id" not in argv, "it used the spec's release anyway"
    assert "-A" in argv


def test_a_caller_that_says_nothing_still_gets_the_spec(tmp_path: Path) -> None:
    """Absent and empty are different answers."""
    argv = importing_with(tmp_path, {}).argv_for("import")
    assert "--search-id" in argv and "from-the-spec" in argv


def test_a_refused_import_reads_as_the_refusal_and_not_a_class_name(
    tmp_path: Path,
) -> None:
    """A person read `ImportFailed: ` in front of why nothing was removed."""
    service, _library = ready_to_import(tmp_path)
    service.settings = replace(service.settings, importer="beets")
    service.runner = FakeRunner(installed={"beet", "metaflac"}).expect("ls", stdout=b"")
    pinned(service, "from-the-spec")
    r = post(
        build(service), "/api/import/album", service, {"mbid": "", "replace": True}
    )
    assert r.json()["error"].startswith("the library holds nothing that is exactly")


def test_the_duplicate_answer_reaches_beets(tmp_path: Path) -> None:
    """beets asks a second question when the album is already there, and an
    unanswered one ends the import with a message about stdin."""
    runner = importing_with(tmp_path, {"duplicates": "keep"})
    assert runner.stdin_for("import") == b"A\nK\n"


def test_saying_nothing_never_sends_beets_own_replace(tmp_path: Path) -> None:
    assert importing_with(tmp_path, {}).stdin_for("import") == b"A\nK\n"


def filed_as(service: Service, tmp_path: Path, *releases: str) -> None:
    """One row of this record's names in the library, for each release given."""
    there = tmp_path / "01 One.flac"
    there.write_bytes(b"fLaC")
    rows = [
        I_SEP.join(
            (
                "T",
                "A Band",
                "A Record",
                "3:00",
                f"{there}",
                "",
                "1",
                rel,
                "A Band",
                "",
                "",
            )
        )
        for rel in releases
    ]
    service.runner = FakeRunner(installed={"beet"}).expect(
        "ls", stdout="\n".join(rows).encode()
    )
    service.settings = replace(service.settings, importer="beets")


def test_another_edition_of_the_same_name_does_not_open_the_archive_gate(
    tmp_path: Path,
) -> None:
    """Falling back on the names counted another release's rows, so a record never
    filed could read as in the library and have its raw side cleared."""
    service, _library = with_library(tmp_path)
    pinned(service, "this-one")
    filed_as(service, tmp_path, "another-one")
    raw = service.layout.raw / "album"
    (raw / "side-a.flac").write_bytes(b"x")
    body = get(build(service), "/api/archive/album", service).json()
    assert not body["ready"] and "another release" in body["why"]
    assert body["library_tracks"] == 0
    r = post(build(service), "/api/archive/album", service, {})
    assert r.status == 409 and (raw / "side-a.flac").is_file()


def test_a_record_filed_with_no_release_still_opens_the_gate_by_its_names(
    tmp_path: Path,
) -> None:
    """From the plan, the rows carry no release even when the spec names one."""
    service, _library = with_library(tmp_path)
    pinned(service, "this-one")
    filed_as(service, tmp_path, "")
    raw = service.layout.raw / "album"
    (raw / "side-a.flac").write_bytes(b"x")
    assert get(build(service), "/api/archive/album", service).json()["ready"]


def test_an_import_under_a_pasted_release_makes_it_the_one_the_plan_names() -> None:
    """A pasted id reached the import and not the plan, so the gate then looked for a
    release nothing was filed under and read another edition's rows."""
    js = (STATIC_JS).read_text()
    run = js[js.index("async function runImport()") :]
    run = run[: run.index("\n}\n")]
    assert (
        "if (mbid && mbid !== S.meta.mbid) { S.meta.mbid = mbid; markDirty(); }" in run
    )
    assert run.index("S.meta.mbid = mbid") < run.index(
        "if (S.dirty && !(await save()))"
    )


def test_archiving_over_a_doubled_library_is_refused(tmp_path: Path) -> None:
    """More than was cut is not a safer kind of enough: it is what an earlier
    copy that was not replaced looks like, and clearing raw/ on the strength of
    it archives over a record the library holds twice. ADR-057."""
    service, library = with_library(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    for i in range(4):  # the plan has one track
        (placed / f"{i:02d} One.flac").write_bytes(b"fLaC")
    body = get(build(service), "/api/archive/album", service).json()
    assert not body["ready"]
    assert "not replaced" in body["why"], body["why"]


# ------------------------------------------------- forced placement (ADR-061)


def drifting_levels(window: float) -> bytes:
    """A side whose two gaps both sit later than the catalogue predicts."""
    rows = []
    for i in range(int(40.0 / window)):
        t = i * window
        quiet = 14.0 <= t < 17.0 or 27.0 <= t < 30.0
        rows.append(f"frame:{i} pts:{i} pts_time:{t}")
        rows.append(f"lavfi.astats.Overall.RMS_level={-85.0 if quiet else -25.0}")
        rows.append(f"lavfi.astats.Overall.Peak_level={-70.0 if quiet else -9.0}")
    return "\n".join(rows).encode()


def drifting_service(tmp_path: Path, *replies: bytes):  # type: ignore[no-untyped-def]
    layout = a_layout(tmp_path, sides=("a",))
    service = a_service(tmp_path, layout=layout)
    windows = int(40.0 / C.WINDOW)
    service.runner = a_runner(
        windows=windows, seconds=40.0, levels=drifting_levels(C.WINDOW)
    )
    C.build(service.runner, layout, "album", "a", dwell=0.0)
    service.fetcher = Catalogue(*replies)  # type: ignore[assignment]
    return service


# 38s of catalogue into 40s of music: it is the displacement that does not fit,
# not the durations.
DRIFTS = [11000, 11000, 13000, 3000]


def test_a_side_the_gaps_displace_off_the_end_is_refused(tmp_path: Path) -> None:
    service = drifting_service(tmp_path, release(DRIFTS))
    body = post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"}).json()
    assert "side a" in body["error"], body["error"]
    assert not service.layout.plan_file("album").exists()


def test_forcing_places_that_side_from_the_catalogue_and_says_which(
    tmp_path: Path,
) -> None:
    service = drifting_service(tmp_path, release(DRIFTS))
    body = post(
        build(service), "/api/firstpass/album", service, {"mbid": "aaa", "force": True}
    ).json()
    assert not body["error"], body["error"]
    assert body["result"]["forced"] == ["a"]
    assert service.layout.plan_file("album").is_file()

    plan = F.read_plan(service.layout.plan_file("album"))
    starts = [t.start for t in plan.sides[0].tracks]
    assert starts == [0.0, 11.0, 22.0, 35.0]


def a_second_pass(service, body=None):  # type: ignore[no-untyped-def]
    service.fetcher = Catalogue(release([2000, 2000, 2000, 2000]))  # type: ignore[assignment]
    return post(
        build(service), "/api/firstpass/album", service, {"mbid": "aaa", **(body or {})}
    ).json()


def settled_at(service, number: int, start: float, end: float, who: str) -> None:  # type: ignore[no-untyped-def]
    plan = F.read_plan(service.layout.plan_file("album"))
    side = next(s for s in plan.sides if any(t.number == number for t in s.tracks))
    moved = tuple(
        replace(t, start=start, end=end, settled=who) if t.number == number else t
        for t in side.tracks
    )
    plan = replace(
        plan,
        sides=tuple(replace(s, tracks=moved) if s is side else s for s in plan.sides),
    )
    F.save(service.layout, "album", F.spec_of(plan), plan)


def track_in(service, number: int):  # type: ignore[no-untyped-def]
    plan = F.read_plan(service.layout.plan_file("album"))
    return next(t for s in plan.sides for t in s.tracks if t.number == number)


@pytest.mark.parametrize("who", ["hand", "ear"])
def test_fitting_again_never_moves_what_a_person_settled(
    tmp_path: Path, who: str
) -> None:
    """Run again used to start from the catalogue, and took every corrected
    boundary with it behind one confirmation."""
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    plain = track_in(service, 1)
    settled_at(service, 1, plain.start + 0.25, plain.end - 0.25, who)

    body = a_second_pass(service)
    assert not body["error"], body["error"]
    assert body["result"]["kept"] == 1

    again = track_in(service, 1)
    assert (again.start, again.end) == (plain.start + 0.25, plain.end - 0.25)
    assert again.settled == who
    spec = F.read_spec(service.layout.spec_file("album"))
    assert spec.sides[0].tracks[0].settled == who


def test_fitting_again_still_moves_what_only_the_fit_placed(tmp_path: Path) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    found = track_in(service, 2)
    plan = F.read_plan(service.layout.plan_file("album"))
    moved = tuple(
        replace(t, start=found.start + 0.25) if t.number == 2 else t
        for t in plan.sides[0].tracks
    )
    plan = replace(plan, sides=(replace(plan.sides[0], tracks=moved), *plan.sides[1:]))
    F.save(service.layout, "album", F.spec_of(plan), plan)

    body = a_second_pass(service)
    assert not body["error"], body["error"]
    assert body["result"]["kept"] == 0
    assert track_in(service, 2).start == found.start


def test_fitting_again_refuses_a_release_with_no_place_for_a_settled_track(
    tmp_path: Path,
) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    plan = F.read_plan(service.layout.plan_file("album"))
    side = plan.sides[0]
    last = side.tracks[-1]
    extra = replace(last, number=9, start=last.end, end=last.end + 0.5, settled="hand")
    plan = replace(
        plan, sides=(replace(side, tracks=(*side.tracks, extra)), *plan.sides[1:])
    )
    F.save(service.layout, "album", F.spec_of(plan), plan)
    before = service.layout.plan_file("album").read_text()

    body = a_second_pass(service)
    assert "9" in body["error"] and "no place" in body["error"]
    assert service.layout.plan_file("album").read_text() == before


def test_a_pass_that_needed_no_forcing_says_so(tmp_path: Path) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    body = post(
        build(service), "/api/firstpass/album", service, {"mbid": "aaa", "force": True}
    ).json()
    assert not body["error"], body["error"]
    assert body["result"]["forced"] == []


def test_a_saved_plan_keeps_what_the_fit_rested_on_through_the_page(
    tmp_path: Path,
) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    before = [
        t.placed
        for s in F.read_plan(service.layout.plan_file("album")).sides
        for t in s.tracks
    ]
    assert "length" in before and "gap" in before
    app = build(service)
    shown = get(app, "/api/album/album", service).json()["tracks_by_side"]
    assert [t["placed"] for s in shown.values() for t in s] == before


def captured_again(service: Service, letter: str = "a") -> None:
    """What a rip, an upload and a salvage all come to: a new file under the name."""
    side = service.layout.raw / "album" / f"side-{letter}.flac"
    side.write_bytes(side.read_bytes() + b"another take")
    C.build(service.runner, service.layout, "album", letter, dwell=0.0)


def a_fit_with_a_mark(tmp_path: Path, who: str = "hand"):  # type: ignore[no-untyped-def]
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    plain = track_in(service, 1)
    settled_at(service, 1, plain.start + 0.25, plain.end - 0.25, who)
    return service, plain


def page_saves(service: Service, moved: dict[int, float] | None = None):  # type: ignore[no-untyped-def]
    """The document the page sends: what it was shown, with any end moved by hand."""
    shown = get(build(service), "/api/album/album", service).json()
    sides = []
    for letter, tracks in shown["tracks_by_side"].items():
        for t in tracks:
            if t["number"] in (moved or {}):
                t.update(end=moved[t["number"]], settled="hand", placed="")  # type: ignore[index]
        sides.append({"letter": letter, "tracks": tracks})
    return post(
        build(service),
        "/api/plan/album",
        service,
        {"album": "A", "artist": "B", "sides": sides},
    ).json()


@pytest.mark.parametrize("who", ["hand", "ear"])
def test_a_first_pass_after_a_re_rip_does_not_hold_times_on_the_audio_that_went(
    tmp_path: Path, who: str
) -> None:
    """Run again pinned settled tracks, and after a re-rip those were positions on
    a replaced capture that would be cut as "Set by you"."""
    service, plain = a_fit_with_a_mark(tmp_path, who)
    captured_again(service)

    body = a_second_pass(service)
    assert not body["error"], body["error"]
    assert body["result"]["kept"] == 0
    assert body["result"]["released"] == [
        {
            "side": "a",
            "number": 1,
            "title": plain.title,
            "why": "that side has been captured again since",
        }
    ]
    again = track_in(service, 1)
    assert again.settled == ""
    assert (again.start, again.end) == (plain.start, plain.end)


def test_a_replaced_side_lets_go_only_of_its_own_marks(tmp_path: Path) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    plan = F.read_plan(service.layout.plan_file("album"))
    on_a, on_b = plan.sides[0].tracks[0], plan.sides[1].tracks[0]
    settled_at(service, on_a.number, on_a.start + 0.25, on_a.end - 0.25, "hand")
    settled_at(service, on_b.number, on_b.start + 0.25, on_b.end - 0.25, "ear")
    captured_again(service, "a")

    body = a_second_pass(service)
    assert body["result"]["kept"] == 1
    assert [x["number"] for x in body["result"]["released"]] == [on_a.number]
    assert track_in(service, on_b.number).settled == "ear"


def test_the_same_audio_says_nothing_was_released(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    body = a_second_pass(service)
    assert body["result"]["kept"] == 1
    assert body["result"]["released"] == []


def test_a_side_since_removed_does_not_stop_the_first_pass_over_its_marks(
    tmp_path: Path,
) -> None:
    """The page cannot show a side that is gone, so "remove it first" had no way out."""
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    on_b = F.read_plan(service.layout.plan_file("album")).sides[1].tracks[0]
    settled_at(service, on_b.number, on_b.start + 0.25, on_b.end - 0.25, "hand")
    (service.layout.raw / "album" / "side-b.flac").unlink()

    service.fetcher = Catalogue(release([1000, 1000, 1000, 1000]))  # type: ignore[assignment]
    body = post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"}).json()
    assert not body["error"], body["error"]
    assert body["result"]["released"][0]["why"] == "that side is no longer there"


def test_a_mark_on_a_plan_that_never_said_which_capture_is_not_held(
    tmp_path: Path,
) -> None:
    """Holding it is the hazard, and letting it go is named."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    plan = F.read_plan(service.layout.plan_file("album"))
    bare = replace(plan, sides=tuple(replace(s, audio="") for s in plan.sides))
    F.save(service.layout, "album", F.spec_of(bare), bare)

    body = a_second_pass(service)
    assert "nothing records" in body["result"]["released"][0]["why"]


def test_a_first_pass_records_the_capture_it_placed_on(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    plan = F.read_plan(service.layout.plan_file("album"))
    stamps = C.stamps_of(service.layout, "album")
    assert [s.audio for s in plan.sides] == [stamps[s.file] for s in plan.sides]
    assert all(s.audio for s in plan.sides)


def test_a_replaced_side_reads_as_from_an_earlier_capture_before_anything_is_run(
    tmp_path: Path,
) -> None:
    service, plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)
    shown = get(build(service), "/api/album/album", service).json()["tracks_by_side"]
    assert {t["placed"] for t in shown["a"]} == {"earlier"}
    assert {t["settled"] for t in shown["a"]} == {""}
    assert shown["a"][0]["start"] == plain.start + 0.25
    assert "earlier" not in {t["placed"] for t in shown["b"]}


def test_saving_over_a_re_rip_does_not_launder_an_old_mark_into_a_new_one(
    tmp_path: Path,
) -> None:
    """Nothing here tells a page's unchanged row from a judgement, except its time."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)

    untouched = page_saves(service)
    assert untouched["released"] == []
    saved = F.read_plan(service.layout.plan_file("album"))
    assert saved.sides[0].tracks[0].settled == "hand", "a save moved no boundary"

    page_saves(service, {1: track_in(service, 1).end - 0.5})
    saved = F.read_plan(service.layout.plan_file("album"))
    assert saved.sides[0].audio == C.stamps_of(service.layout, "album")["side-a.flac"]
    assert saved.sides[0].tracks[0].settled == "hand"


def test_a_boundary_moved_after_a_re_rip_is_a_judgement_about_the_new_capture(
    tmp_path: Path,
) -> None:
    service, plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)
    new_end = plain.end - 0.5

    assert page_saves(service, {1: new_end})["released"] == []
    assert track_in(service, 1).settled == "hand"

    body = a_second_pass(service)
    assert body["result"]["kept"] == 1
    assert track_in(service, 1).end == new_end


def cut(service: Service) -> H.Response:
    return post(build(service), "/api/split/album", service)


def test_a_cut_of_a_replaced_side_is_refused_by_name_before_anything_is_saved(
    tmp_path: Path,
) -> None:
    """The rail said Ready over rows reading "From an earlier capture", and cutting
    would have used times from audio that was gone."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True, exist_ok=True)
    (review / "01 One.flac").write_bytes(b"fLaC")

    r = cut(service)
    assert r.status == 409
    said = r.json()["error"]
    assert "side a was captured again" in said
    assert "First pass" in said and "Checked" in said
    assert "Align" not in said, "no archived capture here, so it is no way out"
    assert (review / "01 One.flac").is_file(), "a refused cut leaves the last one"


def test_a_side_saved_over_is_not_offered_align_and_is_told_the_ways_that_remain(
    tmp_path: Path,
) -> None:
    """Align is a way out and is named until a boundary is moved or checked. That moves
    the stamp, and from then on naming it would send a person to a refusal."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    archived = service.layout.archive / "album"
    archived.mkdir(parents=True, exist_ok=True)
    (archived / "side-a.flac").write_bytes(b"the earlier capture")
    captured_again(service)
    app = build(service)
    assert get(app, "/api/align/album", service).json()["sides"] == ["a"]
    assert "Align from archive" in cut(service).json()["error"]

    page_saves(service)
    assert get(app, "/api/align/album", service).json()["sides"] == ["a"]
    assert "Align from archive" in cut(service).json()["error"]

    page_saves(service, {1: track_in(service, 1).end - 0.5})
    assert get(app, "/api/align/album", service).json()["sides"] == []
    assert "Align" not in cut(service).json()["error"]


def test_a_save_from_a_page_left_open_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """Two tabs: the second's whole document would put back what the first saved."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    app = build(service)
    seen = get(app, "/api/album/album", service).json()["seen"]
    doc = {"album": "A Record", "artist": "A Band", "date": "2022", "sides": []}
    first = post(app, "/api/plan/album", service, {**doc, "seen": seen})
    assert first.status == 200 and first.json()["seen"]
    on_disk = service.layout.plan_file("album").read_bytes()
    stale = post(app, "/api/plan/album", service, {**doc, "seen": seen, "date": "1999"})
    assert stale.status == 409 and stale.json()["stale"]
    assert "nothing was saved" in stale.json()["error"]
    assert service.layout.plan_file("album").read_bytes() == on_disk
    assert get(app, "/api/seen/album", service).json()["seen"] == first.json()["seen"]


def test_a_save_during_a_job_waits_and_one_during_a_prepare_does_not(
    tmp_path: Path,
) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    app = build(service)
    doc = {"album": "A Record", "artist": "A Band", "date": "2022", "sides": []}
    with service.jobs.claim("album", "first pass"):
        held = post(app, "/api/plan/album", service, doc)
    assert held.status == 409 and held.json()["busy"]
    assert "saves when it ends" in held.json()["error"]
    assert post(app, "/api/plan/album", service, doc).status == 200


def test_asking_what_changed_about_a_record_that_went_is_a_404(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    assert get(build(service), "/api/seen/nothing-here", service).status == 404


def test_a_cut_older_than_the_plan_is_said_to_be(tmp_path: Path) -> None:
    """With no unsaved state left, this is what says the page has moved on from it."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    review = service.layout.review_dir("album")
    review.mkdir(parents=True, exist_ok=True)
    cut_file = review / "01 One.flac"
    cut_file.write_bytes(b"fLaC")
    plan = service.layout.plan_file("album")
    app = build(service)
    os.utime(plan, (1000, 1000))
    assert not get(app, "/api/review/album", service).json()["older_than_plan"]
    os.utime(cut_file, (500, 500))
    assert get(app, "/api/review/album", service).json()["older_than_plan"]


def test_a_save_does_not_turn_a_replaced_sides_times_into_something_to_cut(
    tmp_path: Path,
) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)
    page_saves(service)
    assert cut(service).status == 409


def test_moving_some_boundaries_on_a_new_capture_names_only_the_rest(
    tmp_path: Path,
) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service, "b")
    page_saves(service, {2: track_in(service, 2).end - 0.5})
    said = cut(service).json()["error"]
    assert "side b was captured again" in said
    assert '3 "Track 3"' in said and '4 "Track 4"' in said
    assert '2 "Track 2"' not in said


def test_only_the_replaced_sides_tracks_are_named(
    tmp_path: Path,
) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service, "b")
    said = cut(service).json()["error"]
    assert '1 "Track 1"' not in said and '2 "Track 2"' in said


def test_a_first_pass_gets_out_and_keeps_what_was_moved(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service, "b")
    new_end = track_in(service, 2).end - 0.5
    page_saves(service, {2: new_end})
    assert cut(service).status == 409

    a_second_pass(service)
    assert track_in(service, 2).end == new_end, "what they set stays"
    assert cut(service).status == 202


def test_every_track_moved_on_the_new_capture_can_be_cut_with_no_first_pass(
    tmp_path: Path,
) -> None:
    """Somebody with no catalogue entry and no archive has this and Checked left."""
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)
    shown = get(build(service), "/api/album/album", service).json()["tracks_by_side"]
    moved = {t["number"]: t["end"] - 0.5 for rows in shown.values() for t in rows}
    page_saves(service, moved)
    assert cut(service).status == 202


def test_checked_on_a_row_from_an_earlier_capture_is_a_way_out(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    captured_again(service)
    shown = get(build(service), "/api/album/album", service).json()
    for rows in shown["tracks_by_side"].values():
        for t in rows:
            if t["placed"] == "earlier":
                t["settled"] = "ear"
    reply = post(
        build(service),
        "/api/plan/album",
        service,
        {
            "album": "A",
            "artist": "B",
            "sides": [
                {"letter": k, "tracks": v} for k, v in shown["tracks_by_side"].items()
            ],
        },
    ).json()
    assert reply["released"] == []
    assert track_in(service, 1).settled == "ear"
    assert track_in(service, 1).placed == ""
    assert cut(service).status == 202


def test_saving_on_the_same_audio_leaves_every_mark_alone(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    assert page_saves(service)["released"] == []
    assert track_in(service, 1).settled == "hand"


def test_the_page_cannot_tell_the_server_which_capture_a_plan_is_on(
    tmp_path: Path,
) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    shown = get(build(service), "/api/album/album", service).json()["tracks_by_side"]
    post(
        build(service),
        "/api/plan/album",
        service,
        {
            "album": "A",
            "artist": "B",
            "sides": [
                {"letter": k, "audio": "made up", "tracks": v} for k, v in shown.items()
            ],
        },
    )
    saved = F.read_plan(service.layout.plan_file("album"))
    assert saved.sides[0].audio == C.stamps_of(service.layout, "album")["side-a.flac"]


def test_relabelling_keeps_which_capture_the_times_are_on(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    before = F.read_plan(service.layout.plan_file("album")).sides[0].audio
    service.fetcher = Catalogue(release([2000, 2000, 2000, 2000]))  # type: ignore[assignment]
    captured_again(service)
    assert (
        post(build(service), "/api/relabel/album", service, {"mbid": "aaa"}).status
        == 200
    )
    assert F.read_plan(service.layout.plan_file("album")).sides[0].audio == before


def test_a_copy_into_the_archive_is_still_the_same_capture(tmp_path: Path) -> None:
    service, _library = arrived(tmp_path)
    side = service.layout.raw / "album" / "side-a.flac"
    plan = F.read_plan(service.layout.plan_file("album"))
    plan = replace(plan, sides=(replace(plan.sides[0], audio=C.stamp_of(side)),))
    F.save(service.layout, "album", F.spec_of(plan), plan)

    assert not post(build(service), "/api/archive/album", service).json()["error"]
    held = F.read_plan(service.layout.plan_file("album")).sides[0]
    assert held.audio == C.stamps_of(service.layout, "album")["side-a.flac"]


def test_a_truncated_side_repaired_on_the_way_is_still_the_same_capture(
    tmp_path: Path,
) -> None:
    """The repair writes a different file, and the times on it are as right as ever."""
    service, _library = arrived(tmp_path)
    side = service.layout.raw / "album" / "side-a.flac"
    plan = F.read_plan(service.layout.plan_file("album"))
    plan = replace(plan, sides=(replace(plan.sides[0], audio=C.stamp_of(side)),))
    F.save(service.layout, "album", F.spec_of(plan), plan)
    service.runner = Archiving().expect(
        lambda a: a[0] == "flac" and "raw" in a[-1], returncode=1
    )

    assert not post(build(service), "/api/archive/album", service).json()["error"]
    held = F.read_plan(service.layout.plan_file("album")).sides[0]
    assert held.audio == C.stamps_of(service.layout, "album")["side-a.flac"]


def test_a_person_can_ask_to_start_over_on_the_same_audio(tmp_path: Path) -> None:
    service, plain = a_fit_with_a_mark(tmp_path)
    body = a_second_pass(service, {"start_over": True})
    assert not body["error"], body["error"]
    assert body["result"]["kept"] == 0
    assert [x["number"] for x in body["result"]["replaced"]] == [1]
    assert body["result"]["released"] == []
    again = track_in(service, 1)
    assert again.settled == "" and (again.start, again.end) == (plain.start, plain.end)


def test_without_being_asked_nothing_starts_over(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    body = a_second_pass(service, {"start_over": False})
    assert body["result"]["kept"] == 1 and body["result"]["replaced"] == []


def test_starting_over_gets_past_a_mark_the_new_release_has_no_place_for(
    tmp_path: Path,
) -> None:
    service = prepared_service(tmp_path, release([2000, 2000, 2000, 2000]))
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    plan = F.read_plan(service.layout.plan_file("album"))
    side = plan.sides[0]
    last = side.tracks[-1]
    extra = replace(last, number=9, start=last.end, end=last.end + 0.5, settled="hand")
    plan = replace(
        plan, sides=(replace(side, tracks=(*side.tracks, extra)), *plan.sides[1:])
    )
    F.save(service.layout, "album", F.spec_of(plan), plan)

    body = a_second_pass(service, {"start_over": True})
    assert not body["error"], body["error"]
    assert 9 in [x["number"] for x in body["result"]["replaced"]]


def an_overlap(service: Service) -> None:
    """Track 2 moved back past the end of track 1, as a drag does and a save keeps."""
    plan = F.read_plan(service.layout.plan_file("album"))
    one, two = plan.sides[0].tracks[:2]
    moved = tuple(
        replace(t, start=one.end - 0.5, settled="hand") if t is two else t
        for t in plan.sides[0].tracks
    )
    plan = replace(plan, sides=(replace(plan.sides[0], tracks=moved), *plan.sides[1:]))
    F.save(service.layout, "album", F.spec_of(plan), plan)


def with_tracks_to_overlap(tmp_path: Path):  # type: ignore[no-untyped-def]
    service = prepared_service(
        tmp_path, release([1000, 1000, 1000, 1000]), sides=("a",)
    )
    post(build(service), "/api/firstpass/album", service, {"mbid": "aaa"})
    return service


def test_an_overlap_keeps_the_save_and_what_the_person_did(tmp_path: Path) -> None:
    service = with_tracks_to_overlap(tmp_path)
    an_overlap(service)
    shown = get(build(service), "/api/album/album", service).json()["tracks_by_side"][
        "a"
    ]
    assert shown[1]["start"] < shown[0]["end"]
    assert page_saves(service)["ok"]
    assert track_in(service, 2).settled == "hand"


def test_the_archive_gates_409_is_the_overlap_saying_so(tmp_path: Path) -> None:
    """Issue #54 could not say whether the two were linked. They are: the gate
    validates the plan, and this is the refusal it gives."""
    service = with_tracks_to_overlap(tmp_path)
    assert get(build(service), "/api/archive/album", service).status == 200
    an_overlap(service)
    r = get(build(service), "/api/archive/album", service)
    assert r.status == 409
    assert "before track 1 ends" in r.json()["error"]


def test_a_partial_cut_may_not_replace_the_record_through_the_route(
    tmp_path: Path,
) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    service.settings = replace(service.settings, library=str(tmp_path / "lib"))
    review = service.layout.review_dir("album")
    review.mkdir(parents=True, exist_ok=True)
    (review / "01 One.flac").write_bytes(b"fLaC")
    plan = F.read_plan(service.layout.plan_file("album"))
    assert sum(len(s.tracks) for s in plan.sides) != 1
    r = post(build(service), "/api/import/album", service, {"replace": True})
    assert r.status == 409 and "would lose the rest" in r.json()["error"]


def capture_of(slug: str) -> W.Live:
    return W.Live(slug, "b", "hw:9", started=100.0, control=S.Control())


def test_archiving_during_a_capture_of_the_record_once_deleted_the_capture(
    tmp_path: Path,
) -> None:
    service, _library = arrived(tmp_path)
    partial = service.layout.raw / "album" / ".side-b.flac.partial"
    partial.write_bytes(b"being written")
    service.recorder.live = capture_of("album")
    gate = get(build(service), "/api/archive/album", service).json()
    assert not gate["ready"] and gate["capturing"] and "still being" in gate["why"]
    r = post(build(service), "/api/archive/album", service)
    assert r.status == 409 and "still being" in r.json()["error"]
    assert partial.is_file()
    r = post(build(service), "/api/import/album", service, {})
    assert r.status == 409 and "still being" in r.json()["error"]


def test_a_capture_of_another_record_stops_nothing_here(tmp_path: Path) -> None:
    service, _library = arrived(tmp_path)
    service.recorder.live = capture_of("another")
    assert get(build(service), "/api/archive/album", service).json()["ready"]


def test_clearing_stale_rows_asks_about_the_release_the_page_showed(
    tmp_path: Path,
) -> None:
    service, _library = arrived(tmp_path)
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "x.flac").write_bytes(b"x")
    service.settings = replace(
        service.settings, importer="beets", library=str(tmp_path / "keep")
    )
    gone = tmp_path / "keep" / "gone.flac"
    fake = Library(tmp_path / "keep", tmp_path, installed={"beet"})
    fake.add(gone, albumartist="Another Name", album="Other", mb_albumid="rel")
    gone.unlink()
    service.runner = fake
    r = post(build(service), "/api/library/clear-stale/album", service, {"mbid": "rel"})
    assert r.status == 200 and r.json()["cleared"] == 1
    assert not fake.recs


# ------------------------------------------------- archiving as an import's last step


def cut_and_captured(tmp_path: Path) -> tuple[Service, Path]:
    """Cut from the capture in raw, with a plan on that capture, and nothing filed."""
    service, library = ready_to_import(tmp_path)
    service.runner = Archiving()
    raw = service.layout.raw / "album" / "side-a.flac"
    raw.write_bytes(b"fLaC")
    os.utime(raw, (1000, 1000))
    doc = a_plan_dict()
    doc["sides"][0]["audio"] = C.stamp_of(raw)
    F.write_json(service.layout.plan_file("album"), doc)
    return service, library


def test_an_import_archives_as_its_last_step_and_says_both(tmp_path: Path) -> None:
    service, _library = cut_and_captured(tmp_path)
    r = post(build(service), "/api/import/album", service, {})
    result = r.json()["result"]
    assert result["tracks"] == 1 and result["archive"]["archived"]
    assert (service.layout.archive / "album" / "side-a.flac").is_file()
    assert not (service.layout.raw / "album").exists()


def test_an_import_short_of_the_gate_stands_says_why_and_clears_nothing(
    tmp_path: Path,
) -> None:
    service, library = cut_and_captured(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    (placed / "00 Earlier.flac").write_bytes(b"fLaC")
    r = post(build(service), "/api/import/album", service, {})
    assert not r.json()["error"], r.json()["error"]
    archive = r.json()["result"]["archive"]
    assert not archive["archived"] and "not replaced" in archive["why"]
    assert (service.layout.raw / "album" / "side-a.flac").is_file()
    assert (placed / "01 One.flac").is_file()


def test_archiving_that_fails_leaves_the_import_standing_and_can_be_pressed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, library = cut_and_captured(tmp_path)
    real = AR.put_away

    def full(*_a: object, **_k: object) -> dict[str, object]:
        raise OSError("the archive cannot be written")

    monkeypatch.setattr(AR, "put_away", full)
    r = post(build(service), "/api/import/album", service, {})
    assert not r.json()["error"], r.json()["error"]
    archive = r.json()["result"]["archive"]
    assert not archive["archived"] and "cannot be written" in archive["why"]
    assert list(library.rglob("*.flac")), "the import was rolled back"
    assert (service.layout.raw / "album" / "side-a.flac").is_file()
    gate = get(build(service), "/api/archive/album", service).json()
    assert "cannot be written" in gate["archive_failed"] and gate["ready"]

    monkeypatch.setattr(AR, "put_away", real)
    again = post(build(service), "/api/archive/album", service).json()
    assert not again["error"], again["error"]
    assert (service.layout.archive / "album" / "side-a.flac").is_file()
    assert (
        not get(build(service), "/api/albums?archive=1", service)
        .json()["albums"][0]
        .get("archive_failed")
    )


def test_cuts_from_an_earlier_capture_never_archive_over_it(tmp_path: Path) -> None:
    """Cut, captured again, then imported: the new capture was never cut, and the
    archived one is what the tracks came from."""
    service, _library = cut_and_captured(tmp_path)
    archived = service.layout.archive / "album" / "side-a.flac"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"the earlier capture")
    raw = service.layout.raw / "album" / "side-a.flac"
    review = next(service.layout.review_dir("album").glob("*.flac"))
    os.utime(raw, (review.stat().st_mtime + 60, review.stat().st_mtime + 60))
    r = post(build(service), "/api/import/album", service, {})
    archive = r.json()["result"]["archive"]
    assert not archive["archived"] and "captured again" in archive["why"]
    assert archived.read_bytes() == b"the earlier capture"
    assert raw.is_file()


def test_a_re_ripped_side_cut_and_imported_moves_the_earlier_capture_aside(
    tmp_path: Path,
) -> None:
    service, _library = cut_and_captured(tmp_path)
    archived = service.layout.archive / "album" / "side-a.flac"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"the earlier capture")
    r = post(build(service), "/api/import/album", service, {})
    assert r.json()["result"]["archive"]["archived"], r.json()
    aside = list((archived.parent / "_superseded").iterdir())
    assert [p.read_bytes() for p in aside] == [b"the earlier capture"]
    assert archived.read_bytes() == b"fLaC"


def test_a_merge_holding_more_rows_than_its_plan_opens_the_gate(tmp_path: Path) -> None:
    """It said an earlier copy had not been replaced, which told a person to remove a
    correct file, and it could never pass."""
    service, library = arrived(tmp_path)
    (library / "A Band" / "A Record" / "02 Two.flac").write_bytes(b"fLaC")
    doubled = get(build(service), "/api/archive/album", service).json()
    assert not doubled["ready"] and "not replaced" in doubled["why"]
    AR.note_filed(service.layout, "album", {}, merged=True)
    assert get(build(service), "/api/archive/album", service).json()["ready"]
    AR.note_filed(service.layout, "album", {})
    assert not get(build(service), "/api/archive/album", service).json()["ready"]


def test_rows_whose_files_have_gone_do_not_open_the_gate(tmp_path: Path) -> None:
    """The rail read In library and Ready with the files gone."""
    service, _library = arrived(tmp_path)
    gone = tmp_path / "gone" / "01 One.flac"
    service.runner = FakeRunner(installed={"beet"}).expect(
        "ls", stdout=every_row(str(gone)).encode()
    )
    service.settings = replace(service.settings, importer="beets")
    gate = get(build(service), "/api/archive/album", service).json()
    assert not gate["ready"] and gate["library_tracks"] == 0


def test_a_record_with_no_release_does_not_open_the_gate_on_an_edition(
    tmp_path: Path,
) -> None:
    service, _library = arrived(tmp_path)
    there = tmp_path / "01 One.flac"
    there.write_bytes(b"fLaC")
    row = I_SEP.join(
        (
            "T",
            "A Band",
            "A Record",
            "3:00",
            f"{there}",
            "",
            "1",
            "rel",
            "A Band",
            "",
            "",
        )
    )
    service.runner = FakeRunner(installed={"beet"}).expect("ls", stdout=row.encode())
    service.settings = replace(service.settings, importer="beets")
    gate = get(build(service), "/api/archive/album", service).json()
    assert not gate["ready"] and "another release" in gate["why"]


def test_a_save_once_forgot_a_side_the_page_was_not_showing(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    (service.layout.raw / "album" / "side-b.flac").unlink()
    saved_b = F.read_plan(service.layout.plan_file("album")).sides[1]
    assert page_saves(service)["ok"]
    plan = F.read_plan(service.layout.plan_file("album"))
    assert [s.file for s in plan.sides] == ["side-a.flac", "side-b.flac"]
    assert plan.sides[1].tracks == saved_b.tracks


def test_discarding_a_side_takes_it_out_of_both_documents(tmp_path: Path) -> None:
    service, _plain = a_fit_with_a_mark(tmp_path)
    r = post(
        build(service),
        "/api/rip/discard-side",
        service,
        {"slug": "album", "side": "b"},
    )
    assert r.status == 200, r.json()
    assert [s.file for s in F.read_plan(service.layout.plan_file("album")).sides] == [
        "side-a.flac"
    ]
    assert [s.letter for s in F.read_spec(service.layout.spec_file("album")).sides] == [
        "a"
    ]
