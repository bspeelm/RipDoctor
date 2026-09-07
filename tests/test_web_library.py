"""The catalogue, the first pass, and getting a record into the library.

Nothing here reaches the network: the fetcher goes through the same kind of
seam as the external tools, so the whole first pass runs from a scripted reply.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import cache as C
from ripdoctor.store import files as F
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
    assert body["releases"][0]["mbid"] == "aaa"
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
    service = a_service(tmp_path)
    library = tmp_path / "music"
    service.settings = replace(service.settings, library=str(library))
    F.write_json(service.layout.plan_file("album"), a_plan_dict())
    return service, library


def test_the_gate_says_what_is_missing(tmp_path: Path) -> None:
    """It is what stops twenty minutes a side being cleared before the record
    is provably somewhere else."""
    service, _library = with_library(tmp_path)
    body = get(build(service), "/api/archive/album", service).json()
    assert not body["ready"] and body["expected"] == 1 and body["found"] == 0


def test_archiving_before_the_record_arrived_is_refused(tmp_path: Path) -> None:
    service, _library = with_library(tmp_path)
    r = post(build(service), "/api/archive/album", service)
    assert r.status == 409
    assert (service.layout.raw / "album").is_dir(), "raw was cleared anyway"


def test_a_record_that_arrived_can_be_archived(tmp_path: Path) -> None:
    service, library = with_library(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    (placed / "01 One.flac").write_bytes(b"fLaC")
    r = post(build(service), "/api/archive/album", service)
    assert r.status == 200
    assert (service.layout.archive / "album").is_dir()
    assert not (service.layout.raw / "album").exists()


def test_a_side_is_moved_and_never_deleted(tmp_path: Path) -> None:
    """Nothing in this application removes a finished side."""
    service, library = with_library(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    (placed / "01 One.flac").write_bytes(b"fLaC")
    post(build(service), "/api/archive/album", service)
    assert (service.layout.archive / "album" / "side-a.flac").is_file()


def test_archiving_twice_is_refused_rather_than_overwriting(tmp_path: Path) -> None:
    service, library = with_library(tmp_path)
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    (placed / "01 One.flac").write_bytes(b"fLaC")
    post(build(service), "/api/archive/album", service)
    (service.layout.raw / "album").mkdir()
    assert post(build(service), "/api/archive/album", service).status == 409
