"""Replacing one track's audio and nothing else about it.

Every refusal here is about the same rule: nothing in the library is touched
until the replacement has been cut, decoded and measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner, Result
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack, Spec, SpecSide, SpecTrack
from ripdoctor.store import files as F
from ripdoctor.store.files import Layout
from ripdoctor.work import punch as P

TRACK_SECONDS = 60.0
PUNCH_SECONDS = 90.0


def a_plan() -> Plan:
    return Plan(
        slug="album",
        album="A Record",
        artist="A Band",
        date="2022",
        sides=(
            PlanSide(
                file="side-a.flac",
                tracks=(
                    PlanTrack(number=1, title="One", start=10.0, end=70.0, cat=60.0),
                    PlanTrack(number=2, title="Two", start=75.0, end=140.0, cat=65.0),
                ),
            ),
        ),
    )


def a_spec() -> Spec:
    return Spec(
        slug="album",
        album="A Record",
        artist="A Band",
        date="2022",
        sides=(
            SpecSide(
                letter="a",
                start=10.0,
                end=140.0,
                tracks=(
                    SpecTrack(number=1, title="One", cat=60.0, start=10.0, end=70.0),
                    SpecTrack(number=2, title="Two", cat=65.0, start=75.0, end=140.0),
                ),
            ),
        ),
    )


def a_pool(tmp_path: Path, punch: bool = True) -> tuple[Layout, Path]:
    layout = Layout(tmp_path / "vinyl")
    layout.ensure()
    album = layout.raw / "album"
    album.mkdir()
    (album / "side-a.flac").write_bytes(b"fLaC" + b"\x00" * 4000)
    if punch:
        (album / "punch-1.flac").write_bytes(b"fLaC" + b"\x00" * 9000)
    F.save(layout, "album", a_spec(), a_plan())

    library = tmp_path / "music"
    placed = library / "A Band" / "A Record"
    placed.mkdir(parents=True)
    from ripdoctor.integrations.tagger import placements

    for p in placements(a_plan(), "", str(library)):
        p.dest.write_bytes(b"fLaC" + b"\x00" * 3000)
    return layout, library


class Tools(FakeRunner):
    """Durations, a decodable cut, and an ffmpeg that leaves its output."""

    def __init__(self, *, cut_seconds: float | None = None, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kw)
        self.cut_seconds = cut_seconds

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        self.calls.append(tuple(args))
        if args[0] == "ffmpeg" and "-progress" in args:
            path = args[args.index("-i") + 1]
            if "punch" in path and ".new" not in path:
                seconds = PUNCH_SECONDS
            elif ".new" in path:
                seconds = (
                    self.cut_seconds if self.cut_seconds is not None else TRACK_SECONDS
                )
            else:
                seconds = TRACK_SECONDS
            return Result(
                tuple(args), 0, f"out_time_us={int(seconds * 1e6)}\n".encode(), b""
            )
        if args[0] == "ffmpeg" and args[-1].endswith(".flac"):
            Path(args[-1]).write_bytes(b"fLaC" + b"\x00" * 5000)
        for match, reply in self.replies:
            if match(tuple(args)):
                return Result(tuple(args), reply.returncode, reply.stdout, reply.stderr)
        return Result(tuple(args), 0, b"", b"")


# ------------------------------------------------------------- the stem


def test_a_punch_is_invisible_to_every_side_scan(tmp_path: Path) -> None:
    """`punch-7.flac` does not match `side-*.flac`, so the side listing, the
    album picker and the archive gate cannot see it."""
    layout, _library = a_pool(tmp_path)
    assert layout.sides_on_disk("album") == ["a"]
    assert P.capture_path(layout, "album", 1).name == "punch-1.flac"


def test_a_track_with_no_punch_reports_nothing(tmp_path: Path) -> None:
    layout, _library = a_pool(tmp_path, punch=False)
    assert P.recorded(layout, "album", 1) is None


# ------------------------------------------------------------- locating


def test_the_boundaries_are_fitted_rather_than_typed(
    tmp_path: Path, monkeypatch
) -> None:
    """The saved cut already says where the track starts and ends."""
    layout, _library = a_pool(tmp_path)
    from ripdoctor.core.xcorr import Probe, Transform

    monkeypatch.setattr(
        P,
        "fit_punch",
        lambda *a, **k: (
            Transform(offset=-5.0, scale=1.0),
            (Probe(10.0, 5.0, 0.95),),
            True,
        ),
    )
    found = P.locate(Tools(), layout, "album", 1)
    assert found.start == 5.0 and found.end == 65.0
    assert found.scale_assumed and found.as_dict()["fit"]["r"] == 0.95


def test_a_track_that_maps_outside_the_capture_says_what_to_do(
    tmp_path: Path, monkeypatch
) -> None:
    layout, _library = a_pool(tmp_path)
    from ripdoctor.core.xcorr import Probe, Transform

    monkeypatch.setattr(
        P,
        "fit_punch",
        lambda *a, **k: (
            Transform(offset=50.0, scale=1.0),
            (Probe(10.0, 60.0, 0.9),),
            True,
        ),
    )
    with pytest.raises(P.PunchError, match="Record a longer punch"):
        P.locate(Tools(), layout, "album", 1)


def test_locating_without_a_punch_says_so(tmp_path: Path) -> None:
    layout, _library = a_pool(tmp_path, punch=False)
    with pytest.raises(P.PunchError, match="no punch recorded"):
        P.locate(Tools(), layout, "album", 1)


def test_a_track_not_in_the_saved_cut_is_named(tmp_path: Path) -> None:
    layout, _library = a_pool(tmp_path)
    with pytest.raises(P.PunchError, match="no track 9"):
        P.locate(Tools(), layout, "album", 9)


# ------------------------------------------------------------- applying


def test_the_old_file_is_replaced_and_kept(tmp_path: Path) -> None:
    """It is the only copy of that take that is not buried inside a
    twenty-minute side."""
    layout, library = a_pool(tmp_path)
    done = P.apply(
        Tools(),
        layout,
        a_plan(),
        str(library),
        "album",
        1,
        5.0,
        65.0,
        now=lambda: 1_700_000_000.0,
    )
    assert done.path.is_file()
    assert done.kept.is_file(), "the replaced take was not kept"
    assert done.now == TRACK_SECONDS


def test_the_punch_capture_goes_with_it(tmp_path: Path) -> None:
    """So raw is not left holding a stray punch that nothing will ever use."""
    layout, library = a_pool(tmp_path)
    P.apply(Tools(), layout, a_plan(), str(library), "album", 1, 5.0, 65.0)
    assert P.recorded(layout, "album", 1) is None
    assert list(P.kept_dir(layout, "album").glob("*punch-1.flac"))


def test_the_tags_come_from_the_file_being_replaced(tmp_path: Path) -> None:
    """Not from the plan and not from the catalogue. A replacement that also
    re-tags is a partial re-import with a different set of failure modes."""
    layout, library = a_pool(tmp_path)
    fake = Tools()
    P.apply(fake, layout, a_plan(), str(library), "album", 1, 5.0, 65.0)
    exported = [c for c in fake.calls if any("--export-tags-to" in a for a in c)]
    assert exported, "the old file's tags were never read"
    assert "01 One.flac" in exported[0][-1]


def test_a_cut_that_will_not_decode_leaves_the_library_alone(
    tmp_path: Path,
) -> None:
    layout, library = a_pool(tmp_path)
    before = P.library_file(a_plan(), str(library), 1).read_bytes()
    fake = Tools().expect(lambda a: a[0] == "flac", returncode=1, stderr=b"bad")
    with pytest.raises(P.PunchError, match="will not decode"):
        P.apply(fake, layout, a_plan(), str(library), "album", 1, 5.0, 65.0)
    assert P.library_file(a_plan(), str(library), 1).read_bytes() == before


def test_a_cut_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    """A cut that came out short is a boundary that was wrong, not a track."""
    layout, library = a_pool(tmp_path)
    fake = Tools(cut_seconds=40.0)
    with pytest.raises(P.PunchError, match="was asked for"):
        P.apply(fake, layout, a_plan(), str(library), "album", 1, 5.0, 65.0)
    assert P.recorded(layout, "album", 1) is not None, "the punch was consumed anyway"


def test_a_length_nobody_meant_is_refused(tmp_path: Path) -> None:
    layout, library = a_pool(tmp_path)
    with pytest.raises(P.PunchError, match="not a track"):
        P.apply(Tools(), layout, a_plan(), str(library), "album", 1, 5.0, 5.2)


def test_boundaries_outside_the_capture_are_refused(tmp_path: Path) -> None:
    layout, library = a_pool(tmp_path)
    with pytest.raises(P.PunchError, match="outside the punch capture"):
        P.apply(Tools(), layout, a_plan(), str(library), "album", 1, 5.0, 500.0)


def test_a_missing_library_file_is_named(tmp_path: Path) -> None:
    layout, library = a_pool(tmp_path)
    P.library_file(a_plan(), str(library), 1).unlink()
    with pytest.raises(P.PunchError, match="missing"):
        P.apply(Tools(), layout, a_plan(), str(library), "album", 1, 5.0, 65.0)


def test_no_temporary_cut_is_left_behind(tmp_path: Path) -> None:
    layout, library = a_pool(tmp_path)
    fake = Tools(cut_seconds=40.0)
    with pytest.raises(P.PunchError):
        P.apply(fake, layout, a_plan(), str(library), "album", 1, 5.0, 65.0)
    assert not list((layout.raw / "album").glob(".punch-*"))


# ------------------------------------------------------------ discarding


def test_a_punch_can_be_thrown_away_and_recorded_again(tmp_path: Path) -> None:
    layout, _library = a_pool(tmp_path)
    assert P.discard(layout, "album", 1) > 0
    assert P.recorded(layout, "album", 1) is None


def test_discarding_nothing_says_so(tmp_path: Path) -> None:
    layout, _library = a_pool(tmp_path, punch=False)
    with pytest.raises(P.PunchError):
        P.discard(layout, "album", 1)


# ---------------------------------------------------------------- state


def test_every_track_reports_whether_it_can_be_punched(tmp_path: Path) -> None:
    layout, library = a_pool(tmp_path)
    found = P.state(Tools(), layout, a_plan(), str(library), "album")
    rows = found["tracks"]
    assert [r["number"] for r in rows] == [1, 2]
    assert all(r["in_library"] and r["side_audio"] for r in rows)
    assert rows[0]["punch"]["seconds"] == PUNCH_SECONDS
    assert rows[1]["punch"] is None


def test_the_state_is_json_a_page_can_read(tmp_path: Path) -> None:
    layout, library = a_pool(tmp_path)
    found = P.state(Tools(), layout, a_plan(), str(library), "album")
    assert json.loads(json.dumps(found))["album"] == "A Record"
