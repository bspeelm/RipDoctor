"""The base importer: tagging and placing without beets.

This exists so the archive step can be satisfied on an install that has no
beets. Without it a first-time user reaches the last step of the workflow and
cannot complete it, and raw sides accumulate with nowhere to go.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner, ToolFailed
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack
from ripdoctor.integrations import tagger as T


def a_plan(n: int = 2, artist: str = "Punch Brothers", album: str = "Hell") -> Plan:
    return Plan(
        slug="s",
        album=album,
        artist=artist,
        date="2022",
        sides=(
            PlanSide(
                file="side-a.flac",
                tracks=tuple(
                    PlanTrack(
                        number=i + 1,
                        title=f"Track {i + 1}",
                        start=0.0,
                        end=10.0,
                        cat=10.0,
                    )
                    for i in range(n)
                ),
            ),
        ),
    )


def cut_tracks(tmp_path: Path, plan: Plan) -> Path:
    review = tmp_path / "review"
    review.mkdir()
    for side in plan.sides:
        for t in side.tracks:
            (review / f"{t.number:02d} {t.title}.flac").write_bytes(b"fLaC")
    return review


# ---------------------------------------------------------------- tags


def test_every_tag_a_player_needs_is_written() -> None:
    plan = a_plan()
    tags = T.tags_for(plan, plan.sides[0].tracks[0], total=2)
    assert tags[T.TAG_TITLE] == "Track 1"
    assert tags[T.TAG_ALBUM] == "Hell"
    assert tags[T.TAG_TRACK] == "1" and tags[T.TAG_TOTAL] == "2"
    assert tags[T.TAG_ALBUMARTIST] == "Punch Brothers", (
        "without an album artist, a compilation scatters across the library"
    )


def test_a_missing_date_is_omitted_rather_than_written_empty() -> None:
    plan = Plan(slug="s", album="A", artist="B", sides=a_plan().sides)
    assert T.TAG_DATE not in T.tags_for(plan, plan.sides[0].tracks[0], 1)


def test_each_tag_is_removed_before_it_is_set() -> None:
    """Otherwise a second run leaves two values on one tag.

    Players then show whichever they read first, which is not predictable.
    """
    argv = T.write_tags_argv("a.flac", {"TITLE": "One", "ALBUM": "Two"})
    assert argv.index("--remove-tag=TITLE") < argv.index("--set-tag=TITLE=One")
    assert argv.count("--set-tag=TITLE=One") == 1


def test_a_value_containing_an_equals_sign_survives() -> None:
    """Track titles contain all sorts of things."""
    argv = T.write_tags_argv("a.flac", {"TITLE": "E = mc2"})
    assert "--set-tag=TITLE=E = mc2" in argv


def test_a_failed_tag_write_is_raised() -> None:
    fake = FakeRunner().expect("metaflac", returncode=1, stderr=b"not a FLAC file")
    with pytest.raises(ToolFailed, match="not a FLAC"):
        T.write_tags(fake, "a.flac", {"TITLE": "x"})


# ----------------------------------------------------------------- art


def test_art_is_checked_before_the_old_art_is_removed(tmp_path: Path) -> None:
    """The order that once left files with no art at all.

    Removing first and importing second strips what a file has and then fails
    if the new image is bad. Nothing puts it back.
    """
    fake = FakeRunner()
    bad = tmp_path / "tiny.jpg"
    bad.write_bytes(b"\xff\xd8" + b"\x00" * 10)  # a 170-byte error page, in effect

    with pytest.raises(ValueError, match="not a usable image"):
        T.embed_art(fake, "a.flac", str(bad), 1400, 1400)
    assert not fake.calls, "the old art was removed before the new one was checked"


def test_a_usable_image_replaces_the_old_one(tmp_path: Path) -> None:
    art = tmp_path / "cover.jpg"
    art.write_bytes(b"\xff\xd8" + b"\x00" * 4000)
    fake = FakeRunner()
    T.embed_art(fake, "a.flac", str(art), 1400, 1400)

    assert any("--remove" in c for c in fake.calls)
    spec = next(a for c in fake.calls for a in c if "import-picture" in a)
    assert "3|image/jpeg||1400x1400x24|" in spec, "wrong block type or dimensions"


def test_art_presence_is_reported_from_the_picture_block() -> None:
    with_art = FakeRunner().expect("--list", stdout=b"METADATA block #4\n  type: 6")
    without = FakeRunner().expect("--list", stdout=b"")
    assert T.has_art(with_art, "a.flac") is True
    assert T.has_art(without, "a.flac") is False


# ------------------------------------------------------------- placing


def test_a_record_lands_under_artist_and_album() -> None:
    where = T.album_dir("/music", "Punch Brothers", "Hell on Church Street")
    assert where == Path("/music/Punch Brothers/Hell on Church Street")


def test_a_name_that_would_escape_the_library_cannot() -> None:
    where = T.album_dir("/music", "../../etc", "x/y")
    assert "/music/" in str(where)
    assert ".." not in where.name and "/" not in where.name


def test_an_untitled_record_still_has_somewhere_to_go() -> None:
    where = T.album_dir("/music", "", "")
    assert where.parts[-2:] == ("Unknown Artist", "Unknown Album")


def test_tracks_are_tagged_and_moved(tmp_path: Path) -> None:
    plan = a_plan()
    review = cut_tracks(tmp_path, plan)
    fake = FakeRunner()

    moved = T.apply(fake, plan, str(review), str(tmp_path / "music"))

    assert len(moved) == 2
    for p in moved:
        assert p.dest.is_file(), "a track was not moved into the library"
        assert not p.source.exists(), "the cut copy was left behind"
    assert sum(1 for c in fake.calls if "--set-tag=TITLE=Track 1" in c) == 1


def test_imported_files_stay_readable_by_the_group(tmp_path: Path) -> None:
    """A library only its owner can read is one a share cannot serve.

    Nothing reports it, because playback still works for whoever owns the files.
    """
    plan = a_plan(n=1)
    review = cut_tracks(tmp_path, plan)
    moved = T.apply(FakeRunner(), plan, str(review), str(tmp_path / "music"))
    assert moved[0].dest.stat().st_mode & 0o060, "the group cannot read it"


def test_a_missing_cut_track_stops_before_moving_anything(tmp_path: Path) -> None:
    plan = a_plan(n=2)
    review = cut_tracks(tmp_path, plan)
    (review / "02 Track 2.flac").unlink()

    with pytest.raises(FileNotFoundError, match="no cut track"):
        T.apply(FakeRunner(), plan, str(review), str(tmp_path / "music"))


# ---------------------------------------------------- the archive gate


def test_the_gate_finds_nothing_before_anything_is_imported(tmp_path: Path) -> None:
    where, count = T.locate(str(tmp_path / "music"), "A", "B")
    assert where is None and count == 0


def test_the_gate_counts_what_arrived(tmp_path: Path) -> None:
    """What the archive step asks before clearing the raw sides.

    beets answers the same question its own way, which is why the gate takes an
    answer rather than a library.
    """
    plan = a_plan(n=3)
    review = cut_tracks(tmp_path, plan)
    library = tmp_path / "music"
    T.apply(FakeRunner(), plan, str(review), str(library))

    where, count = T.locate(str(library), plan.artist, plan.album)
    assert where is not None and count == 3


def test_the_gate_ignores_files_that_are_not_tracks(tmp_path: Path) -> None:
    plan = a_plan(n=1)
    review = cut_tracks(tmp_path, plan)
    library = tmp_path / "music"
    T.apply(FakeRunner(), plan, str(review), str(library))
    (T.album_dir(str(library), plan.artist, plan.album) / "cover.jpg").write_bytes(b"x")

    _, count = T.locate(str(library), plan.artist, plan.album)
    assert count == 1, "cover art was counted as a track"
