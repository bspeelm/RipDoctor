"""A bought download is a zip someone else built. ADR-062."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ripdoctor.store import unpack as U


def a_zip(path: Path, names: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in names.items():
            zf.writestr(name, body)
    return path


def test_the_tracks_and_the_cover_come_out_flat(tmp_path: Path) -> None:
    src = a_zip(
        tmp_path / "album.zip",
        {
            "Artist - Album/01 One.flac": b"a",
            "Artist - Album/02 Two.flac": b"b",
            "Artist - Album/cover.jpg": b"c",
        },
    )
    got = U.unpack(src, tmp_path / "out")

    assert got.tracks == ("01 One.flac", "02 Two.flac")
    assert got.art == ("cover.jpg",)
    assert sorted(p.name for p in got.where.iterdir()) == [
        "01 One.flac",
        "02 Two.flac",
        "cover.jpg",
    ]


def test_a_path_in_the_archive_cannot_reach_outside_the_directory(
    tmp_path: Path,
) -> None:
    """Only the base name is ever used, so the classic escape is not a case to
    handle - it cannot be expressed."""
    src = a_zip(tmp_path / "evil.zip", {"../../escaped.flac": b"a"})
    got = U.unpack(src, tmp_path / "out")

    assert got.tracks == ("escaped.flac",)
    assert (tmp_path / "out" / "escaped.flac").is_file()
    assert not (tmp_path / "escaped.flac").exists()


def test_the_resource_forks_in_a_mac_zip_are_not_tracks(tmp_path: Path) -> None:
    """Their names match the real ones, so taken as audio they double it."""
    src = a_zip(
        tmp_path / "mac.zip",
        {
            "01 One.flac": b"a",
            "__MACOSX/._01 One.flac": b"junk",
            "._02 Two.flac": b"junk",
        },
    )
    assert U.unpack(src, tmp_path / "out").tracks == ("01 One.flac",)


def test_something_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "album.zip"
    bad.write_bytes(b"fLaC" + b"\x00" * 100)
    with pytest.raises(U.NotAnAlbum, match="not a zip"):
        U.inspect(bad)


def test_a_zip_with_no_audio_is_refused(tmp_path: Path) -> None:
    src = a_zip(tmp_path / "art.zip", {"cover.jpg": b"c", "notes.txt": b"t"})
    with pytest.raises(U.NotAnAlbum, match="no audio"):
        U.inspect(src)


def test_two_files_with_the_same_name_are_refused_rather_than_merged(
    tmp_path: Path,
) -> None:
    """Flattening is what makes the escape impossible, and this is its cost:
    two discs in folders can collide, and silently keeping one would lose a
    track."""
    src = a_zip(
        tmp_path / "two.zip",
        {"Disc 1/01 One.flac": b"a", "Disc 2/01 One.flac": b"b"},
    )
    with pytest.raises(U.NotAnAlbum, match="both called"):
        U.unpack(src, tmp_path / "out")


def test_too_many_files_is_refused(tmp_path: Path) -> None:
    src = a_zip(
        tmp_path / "many.zip",
        {f"{i:03d}.flac": b"a" for i in range(U.MAX_FILES + 1)},
    )
    with pytest.raises(U.NotAnAlbum, match="more than"):
        U.inspect(src)


def test_inspect_names_what_is_inside_without_writing_anything(
    tmp_path: Path,
) -> None:
    src = a_zip(tmp_path / "a.zip", {"d/01.flac": b"a", "d/cover.png": b"c"})
    tracks, art = U.inspect(src)

    assert tracks == ("d/01.flac",) and art == ("d/cover.png",)
    assert list(tmp_path.iterdir()) == [src]
