"""Where a tape lives, and that a restart finds it. ADR-063."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core import tape as TP
from ripdoctor.core.naming import Unsafe
from ripdoctor.store import incoming as IN
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout
from tests.pool import a_layout, frames


def a_tape(layout: Layout) -> TP.Tape:
    return T.create(layout, "Mix: Vol. 1", 1000.0)


def test_a_tape_reads_back_what_was_saved(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    songs = (TP.Song(1, "One", "A Band", "A Record", 60.0),)
    saved = TP.relist(tape, songs)
    T.save(layout, saved)
    assert T.read(layout, tape.id) == saved


def test_a_name_with_awkward_punctuation_keeps_it_in_the_document(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    assert tape.name == "Mix: Vol. 1"
    assert tape.id == "Mix-Vol.-1"
    assert T.dir_for(layout, tape.id).parent == (layout.work / T.WHERE).resolve()


def test_two_names_that_sanitise_alike_get_their_own_tape(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    first = T.create(layout, "Mix: One", 1.0)
    second = T.create(layout, "Mix, One", 2.0)
    assert first.id != second.id
    assert T.read(layout, first.id).name == "Mix: One"
    assert T.read(layout, second.id).name == "Mix, One"


def test_a_write_that_died_leaves_the_last_document_and_is_recovered(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    doc = T.dir_for(layout, tape.id) / T.DOCUMENT
    doc.with_name(doc.name + ".tmp").write_text('{"id": "half a docu')

    assert T.read(layout, tape.id) == tape
    assert T.listed(layout).tapes == (tape,)

    T.save(layout, tape)
    assert not doc.with_name(doc.name + ".tmp").exists()
    assert T.read(layout, tape.id) == tape


def test_a_write_failing_at_the_last_instant_leaves_the_document_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    doc = T.dir_for(layout, tape.id) / T.DOCUMENT
    before = doc.read_text()

    def dies(*_a: object) -> None:
        raise OSError("power went")

    monkeypatch.setattr("os.replace", dies)
    with pytest.raises(OSError, match="power went"):
        T.save(layout, TP.relist(tape, (TP.Song(1, "One", "A", "R", 60.0),)))
    assert doc.read_text() == before


def test_a_blank_name_left_a_stuck_directory_and_now_leaves_nothing(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    with pytest.raises(TP.BadList):
        T.create(layout, "  ", 1.0)
    assert not (layout.work / T.WHERE).exists()


def test_a_document_that_cannot_be_read_is_named_rather_than_dropped(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    good = a_tape(layout)
    bad = T.create(layout, "Other", 1.0)
    (T.dir_for(layout, bad.id) / T.DOCUMENT).write_text(json.dumps({"id": "x"}))
    found = T.listed(layout)
    assert found.tapes == (good,)
    assert found.unreadable == (bad.id,)


def test_nothing_is_listed_before_anything_exists(tmp_path: Path) -> None:
    assert T.listed(a_layout(tmp_path)) == T.Listing((), ())


def test_removing_takes_the_tape_and_its_upload(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    IN.begin(layout, T.key_of(tape.id), name="mix.wav", size=4, now=1.0)
    T.remove(layout, tape.id, 2000.0)
    assert not T.dir_for(layout, tape.id).exists()
    assert IN.state(layout, T.key_of(tape.id)) is None
    with pytest.raises(FileNotFoundError):
        T.read(layout, tape.id)


def test_removing_destroyed_the_only_audio_and_now_moves_it_aside(
    tmp_path: Path,
) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    T.audio_file(layout, tape.id).write_bytes(b"fLaC")
    moved = T.remove(layout, tape.id, 0.0)
    assert moved.name == f"{tape.id}-19700101T000000Z"
    assert moved.parent == (layout.work / T.REMOVED).resolve()
    assert (moved / T.AUDIO).read_bytes() == b"fLaC"
    assert json.loads((moved / T.DOCUMENT).read_text())["name"] == tape.name
    assert T.listed(layout).tapes == ()


def test_a_tape_removed_twice_in_one_second_keeps_both(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    first = T.remove(layout, a_tape(layout).id, 0.0)
    second = T.remove(layout, a_tape(layout).id, 0.0)
    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_a_tape_id_that_escapes_the_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Unsafe):
        T.dir_for(a_layout(tmp_path), "../raw")


def test_a_tape_key_cannot_be_a_record_slug() -> None:
    assert T.key_of("mix").startswith("_")


def an_aged_upload(layout: Layout, key: str, now: float) -> None:
    IN.begin(layout, key, name="mix.wav", size=4, now=1.0)
    aged = now - IN.STALE_SECONDS - 1
    os.utime(IN.part_file(layout, key), (aged, aged))


def test_an_album_upload_swept_a_tape_part_and_now_leaves_it(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    key, now = T.key_of(tape.id), 10.0 * IN.STALE_SECONDS
    an_aged_upload(layout, key, now)
    an_aged_upload(layout, "old-record", now)
    assert IN.sweep(layout, keep="a-record", now=now) == ["old-record"]
    assert IN.state(layout, key) is not None


def test_a_tape_upload_lasts_as_long_as_its_tape(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    key, now = T.key_of(tape.id), 10.0 * IN.STALE_SECONDS
    an_aged_upload(layout, key, now)
    assert T.sweep(layout) == []
    assert IN.state(layout, key) is not None


def test_a_tape_upload_with_no_tape_is_swept(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    key = T.key_of("gone")
    an_aged_upload(layout, key, 10.0 * IN.STALE_SECONDS)
    an_aged_upload(layout, "a-record", 10.0 * IN.STALE_SECONDS)
    assert T.sweep(layout) == [key]
    assert IN.state(layout, key) is None
    assert IN.state(layout, "a-record") is not None


def test_sweeping_before_any_upload_is_not_an_error(tmp_path: Path) -> None:
    assert T.sweep(a_layout(tmp_path)) == []


def a_measuring_runner(windows: int = 40) -> FakeRunner:
    return (
        FakeRunner()
        .expect("sample_rate", stdout=b"48000\n")
        .expect("astats", stdout=frames(windows))
    )


def test_measuring_is_kept_and_not_repeated(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    T.audio_file(layout, tape.id).write_bytes(b"fLaC")
    runner = a_measuring_runner()

    env = T.measure(runner, layout, tape.id, 2.0)
    assert len(env) == 40
    assert env.window == 0.05
    made = len(runner.calls)
    assert any("astats" in " ".join(c) for c in runner.calls)

    T.measure(runner, layout, tape.id, 2.0)
    assert len(runner.calls) == made


def test_an_envelope_that_does_not_line_up_is_not_kept(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    T.audio_file(layout, tape.id).write_bytes(b"fLaC")
    with pytest.raises(RuntimeError, match="would not line up"):
        T.measure(a_measuring_runner(), layout, tape.id, 200.0)
    assert not (T.dir_for(layout, tape.id) / T.ENVELOPE).exists()


def test_new_audio_takes_the_old_measurements_with_it(tmp_path: Path) -> None:
    layout = a_layout(tmp_path)
    tape = a_tape(layout)
    (T.dir_for(layout, tape.id) / T.ENVELOPE).write_bytes(b"old")
    done = IN.done_file(layout, T.key_of(tape.id))
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_bytes(b"fLaC")
    assert T.take_audio(layout, tape.id).read_bytes() == b"fLaC"
    assert not (T.dir_for(layout, tape.id) / T.ENVELOPE).exists()
    assert not done.exists()


def test_an_attempt_at_filing_never_reuses_a_directory(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    tape = T.create(layout, "Mix", 0.0)
    first = T.fresh_filing(layout, tape.id, "attempt")
    second = T.fresh_filing(layout, tape.id, "attempt")
    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_clearing_copies_leaves_every_other_song_s(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    tape = T.create(layout, "Mix", 0.0)
    for name in ("one", "two"):
        where = T.fresh_filing(layout, tape.id, name)
        (where / "01 One.flac").write_bytes(b"a")
    (where / "02 Two.flac").write_bytes(b"b")
    T.clear_copies(layout, tape.id, {"01 One.flac"})
    assert [p.name for p in T.filing_dir(layout, tape.id).rglob("*.flac")] == [
        "02 Two.flac"
    ]
    T.clear_copies(layout, tape.id, {"02 Two.flac"})
    assert not T.filing_dir(layout, tape.id).exists()
