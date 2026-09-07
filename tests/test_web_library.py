"""The catalogue, the first pass, and getting a record into the library.

Nothing here reaches the network: the fetcher goes through the same kind of
seam as the external tools, so the whole first pass runs from a scripted reply.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core.plan import Plan
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
from ripdoctor.web import auth as A
from ripdoctor.web import http as H
from ripdoctor.web.routes import build
from ripdoctor.web.routes.library import spec_from
from tests.pool import a_layout, a_runner, quiet_then_loud
from tests.test_web_routes import a_service, get, post


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


def test_archiving_twice_is_refused_rather_than_overwriting(tmp_path: Path) -> None:
    service, _library = arrived(tmp_path)
    post(build(service), "/api/archive/album", service)
    (service.layout.raw / "album").mkdir()
    (service.layout.raw / "album" / "side-a.flac").write_bytes(b"fLaC" + b"\x00" * 99)
    body = post(build(service), "/api/archive/album", service).json()
    assert "already exists" in body["error"]


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


def test_asking_about_a_record_not_in_the_library_says_so(tmp_path: Path) -> None:
    service, _library = with_library(tmp_path)
    assert get(build(service), "/api/artwork/album", service).status == 409


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
