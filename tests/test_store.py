"""The on-disk layer: containment, layout, and writing both documents together."""

from __future__ import annotations

import json
from dataclasses import replace
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


def a_side(album: Path, letter: str = "a") -> Path:
    album.mkdir(parents=True, exist_ok=True)
    where = album / f"side-{letter}.flac"
    where.write_bytes(b"")
    return where


def test_an_album_is_found_in_raw(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    a_side(layout.raw / "album")
    assert layout.album_dir("album") == (layout.raw / "album").resolve()


def test_an_imported_album_is_still_found_in_archive(tmp_path: Path) -> None:
    """Re-cutting an imported record is normal. Looking only in raw makes it
    fail obscurely - the decode writes nothing and the envelope comes back
    empty, far from the actual cause."""
    layout = a_layout(tmp_path)
    a_side(layout.archive / "album")
    assert layout.album_dir("album") == (layout.archive / "album").resolve()


def test_raw_wins_when_a_record_is_in_both(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    a_side(layout.raw / "album")
    a_side(layout.archive / "album")
    assert layout.album_dir("album").parent == layout.raw.resolve()


def test_a_record_in_neither_is_named(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no album"):
        a_layout(tmp_path).album_dir("missing")


def test_a_punched_take_does_not_hide_the_archive(tmp_path: Path) -> None:
    """A punch is written into raw for a record whose sides are in archive.

    A directory holding one punched track used to shadow the archive it came
    from, and every side of that record stopped resolving - which is exactly
    the set of records punching exists for.
    """
    layout = a_layout(tmp_path)
    a_side(layout.archive / "album")
    (layout.raw / "album").mkdir()
    (layout.raw / "album" / "punch-1.flac").write_bytes(b"")
    assert layout.album_dir("album").parent == layout.archive.resolve()


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


# ------------------------------------------- remembering and forgetting


def a_name(layout: F.Layout, slug: str = "album") -> Path:
    return F.remember(layout, slug, album="A", artist="B", date="2022")


def test_a_name_is_written_before_anything_is_cut(tmp_path: Path) -> None:
    """The names typed to start a rip lived in the browser and nowhere else, so
    a reload threw them away and the only trace of what a record was became its
    slug - which read backwards is a guess."""
    layout = a_layout(tmp_path)
    spec = F.read_spec(a_name(layout))
    assert (spec.album, spec.artist, spec.date) == ("A", "B", "2022")
    assert spec.sides == ()


def test_remembering_again_does_not_touch_the_boundaries(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    F.save(layout, "album", a_spec(), a_plan())
    F.remember(layout, "album", album="New", artist="Other")
    spec = F.read_spec(layout.spec_file("album"))
    assert spec.album == "New" and spec.artist == "Other"
    assert spec.sides == a_spec().sides
    assert (spec.lead, spec.tail, spec.date) == (
        a_spec().lead,
        a_spec().tail,
        "2022",
    )


def test_an_empty_name_does_not_erase_the_one_there(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    a_name(layout)
    F.remember(layout, "album", album="", artist="")
    spec = F.read_spec(layout.spec_file("album"))
    assert (spec.album, spec.artist) == ("A", "B")


def test_a_placeholder_is_forgotten_when_nothing_was_captured(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    where = a_name(layout)
    assert F.forget(layout, "album") is True
    assert not where.exists()


def test_a_spec_with_boundaries_is_never_forgotten(tmp_path: Path) -> None:
    """The one that matters. Every edge in a saved spec was set by somebody
    listening, and nothing in a back-out path is allowed to take them."""
    layout = a_layout(tmp_path)
    F.save(layout, "album", a_spec(), a_plan())
    layout.plan_file("album").unlink()
    assert F.forget(layout, "album") is False
    assert F.read_spec(layout.spec_file("album")).sides == a_spec().sides


def test_a_spec_with_a_release_is_never_forgotten(tmp_path: Path) -> None:
    """Choosing a release from the catalogue is a decision, not a placeholder."""
    layout = a_layout(tmp_path)
    a_name(layout)
    F.write_json(
        layout.spec_file("album"),
        {**json.loads(layout.spec_file("album").read_text()), "mbid": "x"},
    )
    assert F.forget(layout, "album") is False


def test_a_record_with_a_plan_is_never_forgotten(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    a_name(layout)
    F.write_json(layout.plan_file("album"), a_plan().to_dict())
    assert F.forget(layout, "album") is False


@pytest.mark.parametrize("where", ["raw", "archive"])
def test_a_side_on_disk_keeps_the_name(tmp_path: Path, where: str) -> None:
    layout = a_layout(tmp_path)
    a_name(layout)
    a_side(getattr(layout, where) / "album")
    assert F.forget(layout, "album") is False


@pytest.mark.parametrize(
    "name", [".side-a.capturing.wav", "punch-7.flac", "_punched/kept.flac"]
)
def test_any_other_take_keeps_the_name(tmp_path: Path, name: str) -> None:
    """Not a list of the names sides are known by. A partial is most of a side,
    a punch is a track somebody re-recorded, and a file under a record that
    this does not recognise is a reason to stop rather than to continue."""
    layout = a_layout(tmp_path)
    a_name(layout)
    take = layout.raw / "album" / name
    take.parent.mkdir(parents=True)
    take.write_bytes(b"")
    assert F.forget(layout, "album") is False


def test_a_capture_log_is_not_a_take(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    a_name(layout)
    (layout.raw / "album").mkdir()
    (layout.raw / "album" / ".side-a.capturing.log").write_text("")
    assert F.forget(layout, "album") is True


def test_an_unreadable_spec_is_kept_rather_than_deleted(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    layout.spec_file("album").write_text("{")
    assert F.forget(layout, "album") is False
    assert layout.spec_file("album").is_file()


def test_the_emptied_album_directory_goes_with_the_name(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    a_name(layout)
    (layout.raw / "album").mkdir()
    assert F.forget(layout, "album") is True
    assert not (layout.raw / "album").exists()


def test_both_documents_are_written_with_the_same_name(tmp_path: Path) -> None:
    """Every path a record is filed under is built from one or the other, so
    written together but not equal sends the archive gate and the import to
    Unknown Artist while the record sits in the library under its real name."""
    layout = a_layout(tmp_path)
    F.save(layout, "album", replace(a_spec(), album="", artist=""), a_plan())
    spec = F.read_spec(layout.spec_file("album"))
    plan = F.read_plan(layout.plan_file("album"))
    assert (spec.album, spec.artist) == (plan.album, plan.artist) == ("A", "B")


def test_a_name_only_the_spec_has_reaches_the_plan(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    F.save(layout, "album", a_spec(), replace(a_plan(), album="", artist=""))
    assert F.read_plan(layout.plan_file("album")).album == "A"


def test_a_record_can_still_be_saved_before_it_is_named(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    blank = replace(a_spec(), album="", artist="", date="")
    F.save(layout, "album", blank, replace(a_plan(), album="", artist="", date=""))
    assert F.read_spec(layout.spec_file("album")).album == ""
