"""The on-disk layer: containment, layout, and writing both documents together."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.core.naming import Unsafe
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack, Spec, SpecSide, SpecTrack
from ripdoctor.store import files as F
from ripdoctor.store.safety import contains, under

# ------------------------------------------------------------ containment


def test_a_valid_name_resolves_inside_the_root(tmp_path: Path) -> None:
    (tmp_path / "album").mkdir()
    assert under(tmp_path, "album") == (tmp_path / "album").resolve()


@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "/abs", ".hidden"])
def test_a_bad_component_is_refused_by_the_allowlist(tmp_path: Path, bad: str) -> None:
    with pytest.raises(Unsafe):
        under(tmp_path, bad)


def test_a_symlink_out_of_the_tree_is_caught_by_the_second_check(
    tmp_path: Path,
) -> None:
    """The reason resolution happens at all.

    Every component of this path passes the allowlist - the name is perfectly
    ordinary. Only resolving it shows where it actually goes.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "pool"
    root.mkdir()
    (root / "escape").symlink_to(outside)

    with pytest.raises(Unsafe, match="escapes"):
        under(root, "escape")


def test_containment_is_reported_without_raising(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    assert contains(tmp_path, tmp_path / "in")
    assert contains(tmp_path, tmp_path)
    assert not contains(tmp_path / "in", tmp_path)


# ---------------------------------------------------------------- layout


def a_layout(tmp_path: Path) -> F.Layout:
    layout = F.Layout(tmp_path / "vinyl")
    layout.ensure()
    return layout


def test_the_directories_are_made_where_they_are_expected(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    for d in (layout.raw, layout.work, layout.review, layout.archive, layout.cache):
        assert d.is_dir()
    assert layout.cache.is_relative_to(layout.work)


def test_an_album_is_found_in_raw(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    (layout.raw / "album").mkdir()
    assert layout.album_dir("album") == (layout.raw / "album").resolve()


def test_an_imported_album_is_still_found_in_archive(tmp_path: Path) -> None:
    """Re-cutting an imported record is normal. Looking only in raw makes it
    fail obscurely - the decode writes nothing and the envelope comes back
    empty, far from the actual cause."""
    layout = a_layout(tmp_path)
    (layout.archive / "album").mkdir()
    assert layout.album_dir("album") == (layout.archive / "album").resolve()


def test_raw_wins_when_a_record_is_in_both(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    (layout.raw / "album").mkdir()
    (layout.archive / "album").mkdir()
    assert layout.album_dir("album").parent == layout.raw.resolve()


def test_a_record_in_neither_is_named(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no album"):
        a_layout(tmp_path).album_dir("missing")


def test_sides_are_listed_including_ones_that_are_not_letters(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    d = layout.raw / "album"
    d.mkdir()
    for name in ("side-a.flac", "side-b.flac", "side-Orphan.flac", "notes.txt"):
        (d / name).write_bytes(b"")
    assert layout.sides_on_disk("album") == ["Orphan", "a", "b"]


def test_clips_land_beside_the_album_not_inside_it(tmp_path: Path) -> None:
    """An importer pointed at the review directory would take clips for tracks."""
    layout = a_layout(tmp_path)
    assert layout.clips_dir("album").parent == layout.review.resolve()
    assert layout.clips_dir("album") != layout.review_dir("album")


def test_a_slug_cannot_escape_the_pool(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    for bad in ("../etc", "a/b"):
        with pytest.raises(Unsafe):
            layout.plan_file(bad)


# ----------------------------------------------------------- the writes


def a_spec() -> Spec:
    return Spec(
        slug="album",
        album="A",
        artist="B",
        date="2022",
        sides=(
            SpecSide(
                letter="a",
                start=15.0,
                end=1098.0,
                tracks=(
                    SpecTrack(number=1, title="One", cat=150.0),
                    SpecTrack(number=2, title="Two", cat=200.0, start=170.0),
                ),
                fix={1: (160.0, 168.0)},
            ),
        ),
    )


def a_plan() -> Plan:
    return Plan(
        slug="album",
        album="A",
        artist="B",
        date="2022",
        sides=(
            PlanSide(
                file="side-a.flac",
                tracks=(
                    PlanTrack(number=1, title="One", start=15.0, end=165.0, cat=150.0),
                    PlanTrack(number=2, title="Two", start=170.0, end=370.0, cat=200.0),
                ),
            ),
        ),
    )


def test_a_write_is_atomic(tmp_path: Path) -> None:
    """An interrupted write must not truncate what was already there."""
    target = tmp_path / "plan.json"
    target.write_text('{"old": true}')
    F.write_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"new": True}
    assert not list(tmp_path.glob("*.tmp")), "a temporary file was left behind"


def test_the_temporary_file_is_made_beside_the_target(tmp_path: Path) -> None:
    """os.replace cannot cross filesystems, and a pool is often its own mount."""
    import inspect

    source = inspect.getsource(F.write_json)
    assert "with_name" in source
    assert "tempfile" not in source
    assert "gettempdir" not in source


def test_both_documents_are_written_together(tmp_path: Path) -> None:
    """Never one without the other.

    Writing only the plan means the next fit recomputes over a boundary
    somebody set by listening, with nothing to say it happened.
    """
    layout = a_layout(tmp_path)
    spec_path, plan_path = F.save(layout, "album", a_spec(), a_plan())
    assert spec_path.is_file() and plan_path.is_file()

    back = F.read_spec(spec_path)
    assert back.slug == "album" and back.lead == 1.3
    assert back.sides[0].fix == {1: (160.0, 168.0)}, "a manual override was lost"
    assert back.sides[0].tracks[1].start == 170.0, "an ear-set edge was lost"
    assert back.sides[0].tracks[0].start is None, "an edge was invented"


def test_a_spec_round_trips_through_disk(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    spec_path, _ = F.save(layout, "album", a_spec(), a_plan())
    assert F.read_spec(spec_path) == a_spec()


def test_a_plan_round_trips_through_disk(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    _, plan_path = F.save(layout, "album", a_spec(), a_plan())
    assert F.read_plan(plan_path) == a_plan()


# --------------------------------------------- folding a plan back in


def test_moved_boundaries_become_ear_set_edges() -> None:
    """What makes a re-fit safe after somebody has moved a boundary."""
    folded = F.spec_from_plan(a_spec(), a_plan())
    first = folded.sides[0].tracks[0]
    assert first.start == 15.0 and first.end == 165.0
    assert first.has_ear_edges, "the edge would be recomputed on the next fit"


def test_folding_leaves_the_rest_of_the_spec_alone() -> None:
    folded = F.spec_from_plan(a_spec(), a_plan())
    assert folded.slug == "album" and folded.lead == 1.3
    assert folded.sides[0].fix == {1: (160.0, 168.0)}
    assert folded.sides[0].tracks[0].cat == 150.0, "a catalogue duration was lost"


def test_a_track_the_plan_does_not_mention_keeps_what_it_had() -> None:
    spec = a_spec()
    plan = Plan(
        slug="album",
        album="A",
        artist="B",
        sides=(
            PlanSide(
                file="side-a.flac",
                tracks=(
                    PlanTrack(number=1, title="One", start=15.0, end=165.0, cat=150.0),
                ),
            ),
        ),
    )
    folded = F.spec_from_plan(spec, plan)
    assert folded.sides[0].tracks[1].start == 170.0
    assert folded.sides[0].tracks[1].end is None


def test_the_fold_is_idempotent() -> None:
    once = F.spec_from_plan(a_spec(), a_plan())
    assert F.spec_from_plan(once, a_plan()) == once


def test_measurements_can_be_kept_under_a_name_a_pool_already_uses(
    tmp_path: Path,
) -> None:
    """Adopting an existing pool costs nothing: the file for a side is the same
    file, and re-measuring an archive to change a directory name would be hours
    of ffmpeg for a rename."""
    layout = F.Layout(tmp_path / "vinyl", ".cutassist-cache")
    layout.ensure()
    assert layout.cache.name == ".cutassist-cache"
    assert layout.cache.is_dir() and layout.cache.parent == layout.work


def test_the_default_name_is_this_project_s_own(tmp_path: Path) -> None:
    assert F.Layout(tmp_path).cache.name == ".cache"
