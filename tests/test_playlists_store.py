"""Writing a playlist into a library, atomically and only when it changed. ADR-063."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ripdoctor.core import playlist as P
from ripdoctor.core.naming import Unsafe
from ripdoctor.store import playlists as S
from ripdoctor.store import tapes as T
from tests.pool import a_layout

TEXT = "#EXTM3U\n#PLAYLIST:Mix\n../A/01 Ó.flac\n"
FILE = "Mix-0123456789ab.m3u8"


@pytest.fixture
def library(tmp_path: Path) -> str:
    root = tmp_path / "lib"
    root.mkdir()
    return str(root)


def write(library: str, text: str = TEXT, name: str = "Mix", file: str = FILE) -> bool:
    return S.write(library, name, file, text, 0o664, 0o775)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_file_lands_in_the_library_playlists_folder_as_utf8(library: str) -> None:
    assert write(library) is True
    assert (Path(library) / "Playlists" / FILE).read_bytes() == TEXT.encode("utf-8")


def test_a_umask_does_not_decide_the_modes_of_what_is_written(library: str) -> None:
    before = os.umask(0o077)
    try:
        write(library)
    finally:
        os.umask(before)
    folder = Path(library) / "Playlists"
    assert mode(folder) == 0o775 and mode(folder / FILE) == 0o664


def test_a_folder_already_there_keeps_its_own_mode(library: str) -> None:
    folder = Path(library) / "Playlists"
    folder.mkdir()
    folder.chmod(0o750)
    write(library)
    assert mode(folder) == 0o750


def test_a_library_that_is_not_there_is_an_error_and_not_a_new_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(OSError):
        write(str(tmp_path / "unmounted"))
    assert not (tmp_path / "unmounted").exists()


def test_unchanged_content_leaves_the_file_untouched(library: str) -> None:
    write(library)
    target = Path(library) / "Playlists" / FILE
    old = target.stat()
    os.utime(target, ns=(1, 1))
    assert write(library) is False
    assert target.stat().st_mtime_ns == 1 and target.stat().st_ino == old.st_ino


def test_changed_content_replaces_the_file_and_says_so(library: str) -> None:
    write(library)
    changed = TEXT + "../B/02.flac\n"
    assert write(library, changed) is True
    assert (Path(library) / "Playlists" / FILE).read_text() == changed


def test_a_write_that_fails_leaves_the_old_file_whole_and_no_litter(
    library: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(library)

    def refuse(_from: object, _to: object) -> None:
        raise OSError("no room")

    monkeypatch.setattr(S.os, "replace", refuse)
    with pytest.raises(OSError, match="no room"):
        write(library, TEXT + "../B/02.flac\n")
    folder = Path(library) / "Playlists"
    assert (folder / FILE).read_text() == TEXT
    assert [p.name for p in folder.iterdir()] == [FILE]


def test_a_file_that_says_it_is_another_playlist_is_never_overwritten(
    library: str,
) -> None:
    write(library, "#EXTM3U\n#PLAYLIST:Someone Else\n../a.flac\n", "Someone Else")
    with pytest.raises(S.Taken):
        write(library)
    held = (Path(library) / "Playlists" / FILE).read_text()
    assert "Someone Else" in held


@pytest.mark.parametrize("file", ["../escape.m3u8", ".hidden.m3u8", "a/b.m3u8"])
def test_a_filename_that_would_leave_the_folder_is_refused(
    library: str, file: str
) -> None:
    with pytest.raises(Unsafe):
        write(library, file=file)


def test_a_symlinked_folder_leading_out_of_the_library_is_refused(
    library: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(library) / "Playlists").symlink_to(outside)
    with pytest.raises(Unsafe):
        write(library)
    assert not list(outside.iterdir())


def test_what_is_remembered_comes_back_and_starts_as_nothing(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = T.create(layout, "Mix", 1.0)
    assert S.read(layout, tape.id) == P.State()
    known = P.State(
        FILE, 2, (P.Entry(3, state=P.MISSING, why="gone"),), P.WRITTEN, "", 9.0
    )
    S.save(layout, tape.id, known)
    assert S.read(layout, tape.id) == known
    assert T.read(layout, tape.id) == tape
