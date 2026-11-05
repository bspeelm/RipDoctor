"""What a person's ear decides about a tape, in core. ADR-063."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ripdoctor.core import match as M
from ripdoctor.core import tape as TP


def gap(lo: float, hi: float) -> TP.Gap:
    return TP.Gap(lo, hi, lo, hi)


def a_tape(*lengths: float, gaps: tuple[TP.Gap, ...] = ()) -> TP.Tape:
    songs = tuple(
        TP.Song(n, f"Song {n}", "A Band", "A Record", length, settled_by=M.CATALOGUE)
        for n, length in enumerate(lengths, 1)
    )
    end = sum(lengths) + sum(g.hi - g.lo for g in gaps)
    tape = TP.Tape(id="mix", name="Mix", duration=end + 5.0, songs=songs)
    return TP.place(tape, TP.Music(0.0, end, gaps))


THREE = (gap(100.0, 102.0), gap(202.0, 204.0))


def three() -> TP.Tape:
    return a_tape(100.0, 100.0, 100.0, gaps=THREE)


def one_doubt() -> TP.Tape:
    """Song 2 is 20 s away from what the list says."""
    tape = three()
    long = replace(tape.songs[1], length=120.0)
    return TP.place(
        replace(tape, songs=(tape.songs[0], long, tape.songs[2])), TP.music_of(tape)
    )


# ---------------------------------------------------------------- setting by ear


def test_a_boundary_set_by_ear_is_kept_exactly_and_marked_as_the_person_s() -> None:
    tape = TP.set_edges(three(), 2, 101.5, 203.25)
    song = tape.songs[1]
    assert (song.start, song.end, song.set_by) == (101.5, 203.25, TP.EAR)


def test_an_ear_set_edge_is_not_moved_by_placing_again_or_by_a_neighbour_s_edit() -> (
    None
):
    tape = TP.set_edges(three(), 2, 101.5, 203.25)
    again = TP.place(tape, TP.music_of(tape))
    assert (again.songs[1].start, again.songs[1].end) == (101.5, 203.25)
    moved = TP.set_edges(again, 1, 0.5, 100.5)
    assert (moved.songs[1].start, moved.songs[1].end) == (101.5, 203.25)


def test_setting_an_edge_lays_the_found_neighbours_around_it() -> None:
    tape = TP.set_edges(three(), 2, 101.0, 250.0)
    assert tape.songs[2].start >= 250.0
    assert tape.songs[2].set_by == TP.FOUND


def test_a_found_neighbour_that_moved_is_no_longer_heard() -> None:
    tape = TP.hear(TP.hear(three(), 1), 3)
    tape = TP.set_edges(tape, 2, 101.0, 250.0)
    assert tape.songs[0].heard, "song 1 did not move"
    assert not tape.songs[2].heard, "what was heard is not where song 3 is now"


def test_an_edge_moved_into_the_music_puts_the_found_neighbour_in_doubt() -> None:
    tape = TP.set_edges(three(), 2, 103.0, 203.0)
    assert tape.songs[0].end == 103.0
    assert TP.doubtful(tape, tape.songs[0]), "it now spans the silence before 102"
    assert "song 1" in TP.cut_refusal(tape)


def test_an_edge_outside_the_tape_is_refused() -> None:
    tape = three()
    for start, end in ((-1.0, 50.0), (10.0, tape.duration + 1), (50.0, 50.0)):
        with pytest.raises(TP.BadEdge, match="does not fit"):
            TP.set_edges(tape, 1, start, end)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_an_edge_that_is_not_a_number_is_refused(bad: float) -> None:
    with pytest.raises(TP.BadEdge, match="number of seconds"):
        TP.set_edges(three(), 1, bad, 50.0)


def test_an_edge_on_a_tape_with_no_boundaries_found_is_refused() -> None:
    tape = TP.Tape(id="mix", name="Mix", duration=10.0, songs=three().songs)
    with pytest.raises(TP.BadEdge, match="find them first"):
        TP.set_edges(tape, 1, 1.0, 2.0)


def test_overlapping_a_neighbour_s_ear_set_edge_is_refused() -> None:
    tape = TP.set_edges(three(), 2, 101.0, 203.0)
    with pytest.raises(TP.BadEdge, match="overlaps song 2"):
        TP.set_edges(tape, 1, 0.0, 150.0)
    with pytest.raises(TP.BadEdge, match="overlaps song 2"):
        TP.set_edges(tape, 3, 150.0, 300.0)


def test_overlap_is_refused_across_a_song_between_them() -> None:
    tape = TP.set_edges(three(), 1, 0.0, 100.0)
    with pytest.raises(TP.BadEdge, match="overlaps song 1"):
        TP.set_edges(tape, 3, 50.0, 300.0)


def test_meeting_a_neighbour_s_ear_set_edge_exactly_is_not_an_overlap() -> None:
    tape = TP.set_edges(three(), 2, 101.0, 203.0)
    assert TP.set_edges(tape, 1, 0.0, 101.0).songs[0].end == 101.0


def test_an_ear_start_at_zero_still_bounds_the_songs_before_it() -> None:
    """Zero is a position, and was read as no position at all."""
    tape = TP.set_edges(three(), 2, 0.0, 100.0)
    assert tape.songs[0].end <= 0.0, "song 1 has nowhere left to be"
    assert "no room" in TP.cut_refusal(TP.hear(tape, 1))


# ---------------------------------------------------------------------- hearing


def test_heard_clears_the_doubt_but_keeps_found_as_the_origin() -> None:
    tape = one_doubt()
    assert TP.doubtful(tape, tape.songs[1])
    heard = TP.hear(tape, 2)
    song = heard.songs[1]
    assert not TP.doubtful(heard, song)
    assert song.heard
    assert song.set_by == TP.FOUND
    assert song.verdict == TP.DOUBT, "what was measured is still what was measured"


def test_heard_persists_through_the_document_and_an_unmoved_re_run() -> None:
    tape = TP.hear(one_doubt(), 2)
    back = TP.Tape.from_dict(json.loads(json.dumps(tape.to_dict())))
    assert back.songs[1].heard
    assert TP.place(back, TP.music_of(back)).songs[1].heard


def test_a_re_run_that_moves_a_heard_song_takes_the_hearing_away() -> None:
    tape = TP.hear(one_doubt(), 2)
    moved = TP.place(tape, TP.Music(0.0, 310.0, (gap(100.0, 102.0), gap(240.0, 242.0))))
    assert not moved.songs[1].heard
    assert TP.doubtful(moved, moved.songs[1])


def test_a_song_with_no_boundaries_or_set_by_ear_cannot_be_heard() -> None:
    bare = TP.Tape(id="m", name="M", songs=(TP.Song(1, "A", "B", "", 1.0),))
    with pytest.raises(TP.BadEdge, match="no boundaries"):
        TP.hear(bare, 1)
    with pytest.raises(TP.BadEdge, match="already set by ear"):
        TP.hear(TP.set_edges(three(), 1, 0.0, 100.0), 1)


# ---------------------------------------------------------------------- flagging


def test_only_the_doubtful_song_s_boundaries_are_flagged() -> None:
    flagged = [(s.number, edge) for s, edge in TP.flagged(one_doubt())]
    assert flagged == [(2, TP.START), (2, TP.END)]


def test_a_mismatch_flags_every_song_with_its_own_doubts_first() -> None:
    tape = replace(one_doubt(), mismatch="the list disagrees")
    assert [s.number for s in TP.ear_order(tape)] == [2, 1, 3]
    assert len(TP.flagged(tape)) == 6


def test_heard_skipped_and_ear_set_songs_are_not_flagged() -> None:
    tape = replace(one_doubt(), mismatch="the list disagrees")
    tape = TP.set_edges(TP.skip(TP.hear(tape, 1), 3), 2, 101.0, 203.0)
    assert TP.flagged(replace(tape, mismatch="still")) == ()


def test_skipped_songs_come_last_in_the_ear_check() -> None:
    tape = TP.skip(one_doubt(), 1)
    assert [s.number for s in TP.ear_order(tape)] == [2, 3, 1]


# --------------------------------------------------------------- the cut's gate


def test_nothing_refuses_a_tape_with_no_doubt_and_every_song_settled() -> None:
    assert TP.cut_refusal(three()) == ""


def test_an_unheard_doubt_refuses_the_cut_and_is_named() -> None:
    said = TP.cut_refusal(one_doubt())
    assert "song 2 (Song 2)" in said and "doubt" in said
    assert "song 1" not in said


def test_a_mismatch_refuses_the_cut_until_each_song_is_judged() -> None:
    tape = replace(three(), mismatch="the list disagrees")
    assert "song 1" in TP.cut_refusal(tape)
    judged = TP.hear(TP.hear(TP.hear(tape, 1), 2), 3)
    assert TP.cut_refusal(judged) == ""


def test_a_song_with_no_settled_release_refuses_the_cut() -> None:
    tape = three()
    open_song = replace(tape.songs[0], settled_by="")
    tape = replace(tape, songs=(open_song, *tape.songs[1:]))
    said = TP.cut_refusal(tape)
    assert "song 1 (Song 1): no settled release" in said


def test_hearing_a_doubt_leaves_an_unsettled_release_in_the_way() -> None:
    tape = one_doubt()
    open_song = replace(tape.songs[1], settled_by="")
    tape = TP.hear(replace(tape, songs=(tape.songs[0], open_song, tape.songs[2])), 2)
    assert "no settled release" in TP.cut_refusal(tape)


def test_a_skipped_song_needs_neither_an_ear_nor_a_release() -> None:
    tape = one_doubt()
    open_song = replace(tape.songs[1], settled_by="")
    tape = replace(tape, songs=(tape.songs[0], open_song, tape.songs[2]))
    assert TP.cut_refusal(TP.skip(tape, 2)) == ""


def test_a_tape_with_every_song_skipped_has_nothing_to_cut() -> None:
    tape = three()
    for n in (1, 2, 3):
        tape = TP.skip(tape, n)
    assert "nothing to cut" in TP.cut_refusal(tape)


def test_a_song_never_placed_refuses_the_cut() -> None:
    tape = TP.Tape(
        id="m", name="M", songs=(TP.Song(1, "A", "B", "", 1.0, settled_by=M.PERSON),)
    )
    assert "no boundaries yet" in TP.cut_refusal(tape)


def test_a_skip_survives_a_new_list_that_keeps_the_song() -> None:
    tape = TP.skip(three(), 2)
    kept = TP.relist(tape, tuple(replace(s, skip=False) for s in tape.songs[:2]))
    assert kept.songs[1].skip


# ---------------------------------------------------------------- what a cut is


def test_song_100_sorts_after_song_11_in_the_directory() -> None:
    songs = [TP.Song(n, "Same", "A", "", 1.0) for n in range(1, 101)]
    names = [TP.cut_name(s, 100) for s in songs]
    assert names[10].startswith("011 ")
    assert names[99].startswith("100 ")
    assert sorted(names) == names


def test_a_cut_name_stays_short_enough_in_bytes_for_a_long_title() -> None:
    song = TP.Song(1, "é" * 400, "A", "", 1.0)
    name = TP.cut_name(song, 2)
    assert len(name.encode()) < 220
    assert name.endswith(".flac")


def test_a_cut_is_out_of_date_once_its_boundary_moves() -> None:
    tape = three()
    song = tape.songs[0]
    name = TP.cut_name(song, 3)
    made = TP.CutRecord(TP.CUT, name, song.start or 0.0, song.end or 0.0)
    tape = TP.with_cut(tape, 1, made)
    assert TP.cut_status(tape.songs[0], 3, {name}) == TP.CUT
    moved = TP.set_edges(tape, 1, 0.5, 99.5)
    assert TP.cut_status(moved.songs[0], 3, {name}) == TP.OUT_OF_DATE
    assert not TP.is_current(moved.songs[0], 3)


def test_a_record_of_a_cut_whose_file_is_gone_reads_as_not_cut() -> None:
    song = three().songs[0]
    name = TP.cut_name(song, 3)
    made = replace(
        song, cut=TP.CutRecord(TP.CUT, name, song.start or 0.0, song.end or 0.0)
    )
    assert TP.cut_status(made, 3, set()) == TP.NOT_CUT


def test_a_cut_record_survives_the_document() -> None:
    tape = TP.with_cut(three(), 2, TP.CutRecord(TP.CUT_FAILED, "x.flac", 1.0, 2.0, "n"))
    back = TP.Tape.from_dict(json.loads(json.dumps(tape.to_dict())))
    assert back.songs[1].cut == tape.songs[1].cut


def test_all_cut_needs_every_song_but_the_skipped() -> None:
    tape = TP.skip(three(), 3)
    assert not TP.all_cut(tape, set())
    names = set()
    for song in tape.songs[:2]:
        name = TP.cut_name(song, 3)
        names.add(name)
        record = TP.CutRecord(TP.CUT, name, song.start or 0.0, song.end or 0.0)
        tape = TP.with_cut(tape, song.number, record)
    assert TP.all_cut(tape, names)
