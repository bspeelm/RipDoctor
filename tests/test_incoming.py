"""A file arriving in pieces, before it is a side.

Two of these are load-bearing: an upload in flight is invisible to the album
listing, and a name can still be forgotten while one is going on. Everything
downstream leans on both.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ripdoctor.core.naming import Unsafe
from ripdoctor.store import files as F
from ripdoctor.store import incoming as IN
from ripdoctor.store.files import Layout


def a_pool(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "vinyl")
    layout.ensure()
    return layout


def started(layout: Layout, slug: str = "album", size: int = 9) -> None:
    IN.begin(layout, slug, name="album.flac", size=size, now=1000.0)


# --------------------------------------------------------------- where it is


def test_the_scratch_is_under_work_and_not_under_raw(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    part = IN.part_file(layout, "album")
    assert layout.work in part.parents
    assert layout.raw not in part.parents


def test_an_arriving_upload_is_invisible_to_the_album_listing(
    tmp_path: Path,
) -> None:
    """A record exists when a side file does. A file still arriving is not one,
    and must not be listed as a record that can be opened."""
    layout = a_pool(tmp_path)
    started(layout)
    IN.append(layout, "album", 0, b"fLaC" + b"\x00" * 5)
    assert layout.albums() == []
    assert not (layout.raw / "album").exists()


def test_a_name_can_still_be_forgotten_while_an_upload_is_in_flight(
    tmp_path: Path,
) -> None:
    """forget refuses while any file sits under the album directory, so a
    scratch kept there would pin a name belonging to nothing, permanently.
    This is the direct proof the scratch lives somewhere else."""
    layout = a_pool(tmp_path)
    F.remember(layout, "album", album="A Record", artist="A Band", date="")
    started(layout)
    IN.append(layout, "album", 0, b"fLaC")
    assert F.forget(layout, "album") is True


def test_a_slug_that_is_not_a_token_is_refused(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    with pytest.raises(Unsafe):
        IN.part_file(layout, "../escape")


# ------------------------------------------------------------- arriving


def test_pieces_assemble_in_order(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout)
    assert IN.append(layout, "album", 0, b"one") == 3
    assert IN.append(layout, "album", 3, b"two") == 6
    assert IN.part_file(layout, "album").read_bytes() == b"onetwo"


def test_a_piece_at_the_wrong_offset_is_refused_and_says_where_it_is(
    tmp_path: Path,
) -> None:
    """Two tabs, or a retried chunk. The answer carries the real offset so the
    client can continue rather than start again."""
    layout = a_pool(tmp_path)
    started(layout)
    IN.append(layout, "album", 0, b"one")
    with pytest.raises(IN.Mismatch) as caught:
        IN.append(layout, "album", 0, b"one")
    assert caught.value.have == 3


def test_beginning_again_discards_what_was_there(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout)
    IN.append(layout, "album", 0, b"stale")
    started(layout)
    assert IN.state(layout, "album") is not None
    assert IN.state(layout, "album").have == 0  # type: ignore[union-attr]


# ---------------------------------------------------------------- asking


def test_an_interrupted_upload_says_how_much_is_there(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout, size=600)
    IN.append(layout, "album", 0, b"x" * 142)
    held = IN.state(layout, "album")
    assert held is not None
    assert held.have == 142 and held.total == 600 and held.name == "album.flac"


def test_nothing_arriving_is_nothing_rather_than_an_error(tmp_path: Path) -> None:
    assert IN.state(a_pool(tmp_path), "album") is None


def test_an_unreadable_note_reads_as_nothing_arriving(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout)
    IN.note_file(layout, "album").write_text("{ truncated")
    assert IN.state(layout, "album") is None


# ---------------------------------------------------------------- placing


def test_placing_moves_rather_than_copies(tmp_path: Path) -> None:
    """A rename is why the first moment a side can be observed it is whole."""
    layout = a_pool(tmp_path)
    IN.done_file(layout, "album").parent.mkdir(parents=True, exist_ok=True)
    IN.done_file(layout, "album").write_bytes(b"fLaC")
    where = IN.place(layout, "album")
    assert where == layout.raw / "album" / "side-a.flac"
    assert where.read_bytes() == b"fLaC"
    assert not IN.done_file(layout, "album").exists()
    assert layout.albums() == ["album"]


def test_clearing_removes_every_trace(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout)
    IN.append(layout, "album", 0, b"x" * 20)
    assert IN.clear(layout, "album") > 0
    assert IN.state(layout, "album") is None
    assert not IN.part_file(layout, "album").exists()


# ---------------------------------------------------------------- sweeping


def aged(layout: Layout, slug: str, at: float) -> None:
    """Move a scratch file's mtime, which is what staleness is measured on."""
    os.utime(IN.part_file(layout, slug), (at, at))


def test_uploads_nobody_came_back_to_are_swept(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout, "old")
    IN.append(layout, "old", 0, b"abandoned")
    aged(layout, "old", 1000.0)
    assert IN.sweep(layout, keep="album", now=1000.0 + IN.STALE_SECONDS + 1) == ["old"]
    assert IN.state(layout, "old") is None


def test_the_upload_in_hand_is_never_swept(tmp_path: Path) -> None:
    layout = a_pool(tmp_path)
    started(layout)
    IN.append(layout, "album", 0, b"arriving")
    aged(layout, "album", 1000.0)
    assert IN.sweep(layout, keep="album", now=1000.0 + IN.STALE_SECONDS + 1) == []
    assert IN.state(layout, "album") is not None


def test_an_upload_still_being_written_to_is_left_alone(tmp_path: Path) -> None:
    """The mtime moves with every piece, so a slow but live upload keeps
    resetting its own clock rather than being swept out from under itself."""
    layout = a_pool(tmp_path)
    started(layout, "other")
    IN.append(layout, "other", 0, b"recent")
    aged(layout, "other", 1000.0)
    assert IN.sweep(layout, keep="album", now=1000.0 + IN.STALE_SECONDS - 1) == []


def test_sweeping_an_empty_pool_is_not_an_error(tmp_path: Path) -> None:
    assert IN.sweep(a_pool(tmp_path), keep="album", now=1000.0) == []
