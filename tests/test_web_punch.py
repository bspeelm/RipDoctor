"""Punching from a browser: state, locate, apply, discard."""

from __future__ import annotations

from pathlib import Path

from ripdoctor.audio import capture as C
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core.xcorr import AlignError, Probe, Transform
from ripdoctor.store import files as F
from ripdoctor.web.routes import build
from ripdoctor.work import punch as P
from ripdoctor.work.capture import Recorder
from tests.test_punch import Tools, a_plan, a_pool, a_spec
from tests.test_web_routes import a_service, get, post


def punchable(tmp_path: Path, punch: bool = True):  # type: ignore[no-untyped-def]
    layout, library = a_pool(tmp_path / "pool", punch=punch)
    service = a_service(tmp_path, layout=layout)
    service.runner = Tools()
    service.settings = __import__("dataclasses").replace(
        service.settings, library=str(library)
    )
    F.save(layout, "album", a_spec(), a_plan())
    return service


def a_fit(monkeypatch, offset: float = -5.0) -> None:
    monkeypatch.setattr(
        P,
        "fit_punch",
        lambda *a, **k: (
            Transform(offset=offset, scale=1.0),
            (Probe(10.0, 10.0 + offset, 0.95),),
            True,
        ),
    )


# ----------------------------------------------------------------- state


def test_every_track_says_whether_it_can_be_punched(tmp_path: Path) -> None:
    service = punchable(tmp_path)

    body = get(build(service), "/api/punch/album", service).json()
    assert [t["number"] for t in body["tracks"]] == [1, 2]
    assert body["tracks"][0]["punch"] and body["tracks"][1]["punch"] is None


def test_a_record_with_no_plan_has_nothing_to_punch(tmp_path: Path) -> None:
    """Not a 404. A record captured and not yet cut is the ordinary state, and
    the page swallows a 404 silently - so the reason would reach nobody."""
    service = a_service(tmp_path)
    body = get(build(service), "/api/punch/album", service).json()
    assert body["tracks"] == [] and "no saved cut" in body["why"]


# ---------------------------------------------------------------- locate


def test_locating_fits_the_saved_boundaries_onto_the_capture(
    tmp_path: Path, monkeypatch
) -> None:

    service = punchable(tmp_path)
    a_fit(monkeypatch)
    r = post(build(service), "/api/punch/album/locate", service, {"number": 1})
    assert r.status == 202
    assert r.json()["result"]["start"] == 5.0
    assert r.json()["result"]["scale_assumed"]


def test_locating_without_a_track_number_is_refused(tmp_path: Path) -> None:

    service = punchable(tmp_path)
    assert post(build(service), "/api/punch/album/locate", service, {}).status == 400


def test_a_punch_that_does_not_match_is_reported_not_applied(
    tmp_path: Path, monkeypatch
) -> None:

    service = punchable(tmp_path)

    def refuse(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AlignError("only matched at r=0.20")

    monkeypatch.setattr(P, "fit_punch", refuse)
    body = post(
        build(service), "/api/punch/album/locate", service, {"number": 1}
    ).json()
    assert "r=0.20" in body["error"]


# ---------------------------------------------------------------- audio


def test_the_punch_can_be_listened_to(tmp_path: Path) -> None:

    service = punchable(tmp_path)
    r = get(build(service), "/api/punch/album/audio?number=1", service)
    assert r.path and r.path.endswith("punch-1.flac")


def test_a_track_with_no_punch_has_nothing_to_play(tmp_path: Path) -> None:

    service = punchable(tmp_path)
    assert get(build(service), "/api/punch/album/audio?number=2", service).status == 404


# ---------------------------------------------------------------- apply


def test_applying_replaces_the_library_file_and_keeps_the_old_one(
    tmp_path: Path,
) -> None:

    service = punchable(tmp_path)
    r = post(
        build(service),
        "/api/punch/album/apply",
        service,
        {"number": 1, "start": 5.0, "end": 65.0},
    )
    assert r.status == 200 and r.json()["ok"]
    assert Path(r.json()["backup"]).is_file(), "the replaced take was not kept"


def test_a_refusal_leaves_the_library_untouched(tmp_path: Path) -> None:
    """Every refusal in this path happens before anything is moved."""

    service = punchable(tmp_path)
    before = P.library_file(a_plan(), service.settings.library, 1).read_bytes()
    r = post(
        build(service),
        "/api/punch/album/apply",
        service,
        {"number": 1, "start": 5.0, "end": 5.2},
    )
    assert r.status == 409
    assert P.library_file(a_plan(), service.settings.library, 1).read_bytes() == before


def test_applying_without_boundaries_is_refused(tmp_path: Path) -> None:

    service = punchable(tmp_path)
    r = post(build(service), "/api/punch/album/apply", service, {"number": 1})
    assert r.status == 400


# -------------------------------------------------------------- discard


def test_a_punch_can_be_thrown_away_and_recorded_again(tmp_path: Path) -> None:

    service = punchable(tmp_path)
    app = build(service)
    assert post(app, "/api/punch/album/discard", service, {"number": 1}).status == 200
    assert post(app, "/api/punch/album/discard", service, {"number": 1}).status == 404


# -------------------------------------------------------------- recording


def test_a_punch_is_recorded_under_a_stem_no_side_scan_matches(
    tmp_path: Path,
) -> None:
    """`punch-7.flac` does not match `side-*.flac`, so the album picker and the
    archive gate cannot see it."""

    service = punchable(tmp_path, punch=False)
    # The capture ends at once, which is enough: what is being asserted is the
    # name the recorder was told to write.
    service.runner = FakeRunner(exit_after=1)
    service.recorder = Recorder(
        spawn=lambda work: work(),
        now=lambda: 1000.0,
        tick=lambda: 0.0,
        sleep=lambda _s: None,
    )
    service.settings = __import__("dataclasses").replace(
        service.settings, capture_device="hw:Rx,0"
    )
    r = post(
        build(service),
        "/api/rip/start",
        service,
        {"slug": "album", "side": "7", "kind": "punch"},
    )
    assert r.status == 202 and r.json()["kind"] == "punch"
    started = " ".join(service.runner.calls[0])
    assert ".punch-7.capturing.wav" in started
    assert "side-7" not in started
    assert C.partial_path(tmp_path, "7", "punch").name.startswith(".punch-")


def test_a_record_with_only_a_name_is_still_named(tmp_path: Path) -> None:
    service = a_service(tmp_path)
    F.remember(service.layout, "album", album="Second", artist="First")
    body = get(build(service), "/api/punch/album", service).json()
    assert (body["artist"], body["album"]) == ("First", "Second")
    assert body["tracks"] == []


def test_locating_against_a_record_with_no_cut_says_so(tmp_path: Path) -> None:
    """Not "no track 1". A record that has only been named has a spec with no
    sides in it, and the blunt answer was also the wrong one."""
    service = a_service(tmp_path)
    F.remember(service.layout, "album", album="Second", artist="First")
    r = post(build(service), "/api/punch/album/locate", service, {"number": 1})
    assert "no saved cut" in r.json()["error"]
