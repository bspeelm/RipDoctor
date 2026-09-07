"""Prepared sides: measured once, kept, and invalidated by the file itself.

Nothing here runs ffmpeg. The fake leaves the files a real encode would, which
is the part that matters: a cache entry whose metadata outlives what it
describes is a 404 in the middle of a page rather than a rebuild.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core.envelope import FormatError
from ripdoctor.store import cache as C
from ripdoctor.store.files import Layout
from tests.pool import SECONDS, WINDOWS, a_layout, a_runner


def built(
    tmp_path: Path, runner: FakeRunner | None = None
) -> tuple[C.Prepared, Layout]:
    layout = a_layout(tmp_path)
    return C.build(runner or a_runner(), layout, "album", "a", dwell=0.0), layout


# --------------------------------------------------------------- building


def test_a_side_is_measured_and_encoded_once(tmp_path: Path) -> None:
    prepared, layout = built(tmp_path)
    assert prepared.windows == WINDOWS
    assert prepared.duration == SECONDS
    assert C.envelope_path(layout, "album", "a").is_file()
    assert C.preview_path(layout, "album", "a").is_file()
    assert C.meta_path(layout, "album", "a").is_file()


def test_the_duration_comes_from_decoding_not_the_header(tmp_path: Path) -> None:
    """A capture ended with a signal reports no duration at all."""
    runner = a_runner()
    built(tmp_path, runner)
    assert any("-progress" in " ".join(c) for c in runner.calls)


def test_nothing_is_rebuilt_for_a_side_already_prepared(tmp_path: Path) -> None:
    prepared, layout = built(tmp_path)
    quiet = a_runner()
    again = C.build(quiet, layout, "album", "a", dwell=0.0)
    assert again == prepared
    assert quiet.calls == [], "a prepared side was measured again"


def test_a_re_rip_invalidates_what_was_cached(tmp_path: Path) -> None:
    """Keyed to the source file, so nobody has to remember to clear it."""
    _prepared, layout = built(tmp_path)
    source = layout.side_file("album", "a")
    source.write_bytes(b"fLaC" + b"\x00" * 9000)
    assert C.prepared(layout, "album", "a") is None


def test_metadata_that_outlives_its_files_is_not_trusted(tmp_path: Path) -> None:
    """Otherwise a prepared side becomes a 404 in the middle of a page."""
    _prepared, layout = built(tmp_path)
    C.preview_path(layout, "album", "a").unlink()
    assert C.prepared(layout, "album", "a") is None


def test_unreadable_metadata_is_a_rebuild_not_a_crash(tmp_path: Path) -> None:
    _prepared, layout = built(tmp_path)
    C.meta_path(layout, "album", "a").write_text("{not json")
    assert C.prepared(layout, "album", "a") is None


def test_progress_is_reported_as_it_goes(tmp_path: Path) -> None:
    """Preparing a side takes seconds per lane, and a page with nothing on it
    for twelve seconds looks broken."""
    layout = a_layout(tmp_path)
    said: list[str] = []
    C.build(a_runner(), layout, "album", "a", progress=said.append, dwell=0.0)
    assert len(said) >= 3 and said[-1] == "done"


# ------------------------------------------------------------- refusals


def test_an_envelope_that_will_not_line_up_is_refused(tmp_path: Path) -> None:
    """Every cut taken from one is wrong, and nothing about the plan looks
    wrong until somebody listens. ADR-014.
    """
    layout = a_layout(tmp_path)
    runner = a_runner(windows=WINDOWS // 2)
    with pytest.raises(RuntimeError, match="will not line up"):
        C.build(runner, layout, "album", "a", dwell=0.0)
    assert C.prepared(layout, "album", "a") is None


def test_a_side_still_being_written_is_not_measured(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    source = layout.side_file("album", "a")

    def grow(_seconds: float) -> None:
        with source.open("ab") as f:
            f.write(b"\x00" * 100)

    assert C.is_growing(source, 0.0, sleep=grow)
    with pytest.raises(C.StillRecording):
        C.build(a_runner(), layout, "album", "a", dwell=0.0, sleep=grow)


def test_a_file_that_is_not_there_is_not_growing(tmp_path: Path) -> None:
    assert not C.is_growing(tmp_path / "absent", 0.0, sleep=lambda _s: None)


def test_a_preview_that_encoded_to_nothing_is_refused(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    with pytest.raises(RuntimeError, match="produced nothing"):
        C.build(a_runner(encodes=False), layout, "album", "a", dwell=0.0)


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    """A half-written envelope under the real name is one the next view reads
    as a whole side."""
    _prepared, layout = built(tmp_path)
    assert not list(C.dir_for(layout, "album").glob("*.tmp"))


# --------------------------------------------------------------- reading


def test_the_three_lanes_come_back(tmp_path: Path) -> None:
    _prepared, layout = built(tmp_path)
    lanes = C.lanes_of(layout, "album", "a")
    assert len(lanes.full) == len(lanes.band) == WINDOWS
    assert lanes.window == C.WINDOW


def test_reading_a_side_that_was_never_prepared_says_so(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    with pytest.raises(C.NotPrepared):
        C.lanes_of(layout, "album", "a")


def test_a_foreign_file_in_the_cache_is_refused(tmp_path: Path) -> None:
    _prepared, layout = built(tmp_path)
    C.envelope_path(layout, "album", "a").write_bytes(b"ID3" + b"\x00" * 100)
    with pytest.raises(FormatError):
        C.lanes_of(layout, "album", "a")


# -------------------------------------------------------------- clearing


def test_forgetting_one_side_leaves_the_rest(tmp_path: Path) -> None:
    _prepared, layout = built(tmp_path)
    (C.dir_for(layout, "album") / "b.env").write_bytes(b"")
    assert C.forget(layout, "album", "a") == 3
    assert (C.dir_for(layout, "album") / "b.env").is_file()


def test_forgetting_a_record_clears_all_of_it(tmp_path: Path) -> None:
    _prepared, layout = built(tmp_path)
    assert C.forget(layout, "album") == 3
    assert C.prepared(layout, "album", "a") is None


# ----------------------------------------------------------------- argv


def test_the_preview_is_a_seekable_ogg_not_the_capture() -> None:
    """The browser has to seek a twenty-minute side, and the capture reports no
    duration at all."""
    argv = C.preview_argv("/raw/side-a.flac", "/cache/a.opus.tmp")
    assert "libopus" in argv and argv[argv.index("-f") + 1] == "ogg"


def test_the_stamp_changes_when_the_file_does(tmp_path: Path) -> None:
    target = tmp_path / "side.flac"
    target.write_bytes(b"a" * 100)
    first = C.stamp_of(target)
    target.write_bytes(b"a" * 200)
    assert C.stamp_of(target) != first


def test_the_metadata_is_json_a_browser_can_read(tmp_path: Path) -> None:
    prepared, layout = built(tmp_path)
    data = json.loads(C.meta_path(layout, "album", "a").read_text())
    assert data["windows"] == prepared.windows and data["window_ms"] == 50


def test_a_measurement_written_before_the_rate_was_recorded_still_counts(
    tmp_path: Path,
) -> None:
    """The pool this adopted holds measurements from a version that did not
    write a rate. Nothing reads it, and rebuilding an archive to add a number
    would be hours of ffmpeg for nothing - so it read as a 500 instead."""
    _prepared, layout = built(tmp_path)
    meta = C.meta_path(layout, "album", "a")
    without = {k: v for k, v in json.loads(meta.read_text()).items() if k != "rate"}
    meta.write_text(json.dumps(without))
    again = C.prepared(layout, "album", "a")
    assert again is not None and again.rate == 0


def test_metadata_missing_something_else_rebuilds_rather_than_raising(
    tmp_path: Path,
) -> None:
    _prepared, layout = built(tmp_path)
    meta = C.meta_path(layout, "album", "a")
    meta.write_text(json.dumps({"slug": "album", "side": "a"}))
    assert C.prepared(layout, "album", "a") is None
