"""A tape's list and the placing of its songs, with nothing but values. ADR-063."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from ripdoctor.core import gaps as G
from ripdoctor.core import tape as TP
from ripdoctor.core.envelope import DB_MIN, Envelope

HEADER = "playlist,owned,position,title,artist,album,duration\n"


def a_list(*rows: str) -> str:
    return HEADER + "".join(f"{r}\n" for r in rows)


def one(row: str) -> TP.Song:
    return TP.parse_list(a_list(row))[0]


def a_tape(*lengths: float) -> TP.Tape:
    songs = tuple(
        TP.Song(n, f"Song {n}", "A Band", "A Record", length)
        for n, length in enumerate(lengths, 1)
    )
    return TP.Tape(id="t", name="Mix", songs=songs)


def gap(lo: float, hi: float) -> TP.Gap:
    return TP.Gap(lo, hi, lo, hi)


def lane(segments: list[tuple[float, float]], window: float = 0.05) -> Envelope:
    levels: list[float] = []
    for seconds, db in segments:
        levels.extend([db] * round(seconds / window))
    return Envelope(tuple(levels), window)


# ------------------------------------------------------------------- the list


def test_a_title_with_a_comma_and_quotes_survives_the_csv() -> None:
    song = one('Mix,yes,1,"Song, ""Two"" Parts",A Band,"Record, The",3:45')
    assert song.title == 'Song, "Two" Parts'
    assert song.album == "Record, The"
    assert song.length == 225.0


def test_a_literal_quote_inside_an_unquoted_title_is_kept() -> None:
    assert one('Mix,,1,12" Mix,A Band,A Record,3:00').title == '12" Mix'


def test_crlf_line_endings_and_a_byte_order_mark_are_read() -> None:
    text = "﻿" + a_list("Mix,,1,One,A Band,A Record,1:00").replace("\n", "\r\n")
    assert TP.parse_list(text)[0].title == "One"


def test_edge_and_double_spaces_go_and_accents_compose() -> None:
    song = one("Mix,,1,  Café   Song ,  A  Band , A Record ,1:00")
    assert song.title == "Café Song"
    assert song.artist == "A Band"
    assert song.album == "A Record"


def test_a_long_duration_reads() -> None:
    assert one("Mix,,1,Suite,A Band,A Record,56:30").length == 3390.0


def test_blank_lines_are_not_songs() -> None:
    assert len(TP.parse_list(a_list("", "Mix,,1,One,A Band,R,1:00", ""))) == 1


def test_the_owned_column_changes_nothing() -> None:
    assert one("Mix,yes,1,One,A,R,1:00") == one("Mix,no,1,One,A,R,1:00")


@pytest.mark.parametrize("bad", ["3:75", "abc", "", "0:00", "3.5", "-1:00"])
def test_a_bad_duration_names_the_row_and_the_fix(bad: str) -> None:
    with pytest.raises(TP.BadList, match=r"row 2: .*write it like 3:45"):
        one(f"Mix,,1,One,A Band,R,{bad}")


@pytest.mark.parametrize(
    "huge",
    [10**400, float("inf"), "9" * 400 + ":00:00", "9" * 5000 + ":00:00"],
)
def test_a_length_too_large_to_hold_was_a_crash_and_is_now_refused(
    huge: object,
) -> None:
    with pytest.raises(TP.BadList, match="song 1: length"):
        TP.songs_from([{"title": "O", "artist": "A", "length": huge}])


def test_rows_are_sorted_by_position() -> None:
    songs = TP.parse_list(a_list("M,,2,Two,A,R,1:00", "M,,1,One,A,R,1:00"))
    assert [(s.number, s.title) for s in songs] == [(1, "One"), (2, "Two")]


def test_a_gap_in_positions_names_the_row() -> None:
    with pytest.raises(TP.BadList, match="row 3: positions jump from 1 to 3"):
        TP.parse_list(a_list("M,,1,One,A,R,1:00", "M,,3,Three,A,R,1:00"))


def test_a_gap_in_positions_names_what_is_missing() -> None:
    with pytest.raises(TP.BadList, match="position 2 is missing"):
        TP.parse_list(a_list("M,,1,One,A,R,1:00", "M,,3,Three,A,R,1:00"))
    with pytest.raises(TP.BadList, match="positions 2 to 4 are missing"):
        TP.parse_list(a_list("M,,1,One,A,R,1:00", "M,,5,Five,A,R,1:00"))


def test_positions_that_do_not_begin_at_one_were_renumbered_and_are_now_refused() -> (
    None
):
    with pytest.raises(TP.BadList, match=r"row 2: .*positions 1 to 2 are missing"):
        TP.parse_list(a_list("M,,3,Three,A,R,1:00", "M,,4,Four,A,R,1:00"))
    with pytest.raises(TP.BadList, match="begin at 2 - position 1 is missing"):
        TP.parse_list(a_list("M,,2,Two,A,R,1:00"))


def test_a_repeated_column_took_the_last_silently_and_is_now_refused() -> None:
    text = HEADER.strip() + ",title\nM,,1,One,A,R,1:00,Two\n"
    with pytest.raises(TP.BadList, match="row 1: more than one title column"):
        TP.parse_list(text)


def test_blank_columns_from_a_spreadsheet_are_not_repeated_columns() -> None:
    text = HEADER.strip() + ",,\nM,,1,One,A,R,1:00,,\n"
    assert TP.parse_list(text)[0].title == "One"


def test_a_repeated_position_is_refused() -> None:
    with pytest.raises(TP.BadList, match="position 1 is also on row 2"):
        TP.parse_list(a_list("M,,1,One,A,R,1:00", "M,,1,Two,A,R,1:00"))


def test_two_playlists_in_one_file_are_told_to_split() -> None:
    with pytest.raises(TP.BadList, match=r"2 playlists .*split it"):
        TP.parse_list(a_list("Mix A,,1,One,A,R,1:00", "Mix B,,2,Two,A,R,1:00"))


def test_an_unquoted_comma_says_to_quote_it() -> None:
    with pytest.raises(TP.BadList, match=r"row 2: 8 values .*double quotes"):
        one("Mix,,1,One, Two,A Band,R,1:00")


def test_an_open_quote_is_a_refusal_rather_than_a_swallowed_file() -> None:
    with pytest.raises(TP.BadList, match="double quotes"):
        TP.parse_list(a_list('M,,1,"One,A,R,1:00', "M,,2,Two,A,R,1:00"))


def test_a_missing_column_is_named() -> None:
    with pytest.raises(TP.BadList, match="no duration column"):
        TP.parse_list("playlist,owned,position,title,artist,album\nM,,1,O,A,R\n")


@pytest.mark.parametrize("text", ["", HEADER])
def test_a_list_without_songs_is_refused(text: str) -> None:
    with pytest.raises(TP.BadList):
        TP.parse_list(text)


def test_a_list_by_hand_is_cleaned_and_checked_the_same_way() -> None:
    songs = TP.songs_from(
        [{"title": " One  Two ", "artist": "A", "album": "R", "length": "1:00"}]
    )
    assert songs == (TP.Song(1, "One Two", "A", "R", 60.0),)
    with pytest.raises(TP.BadList, match=r"song 1: .*title and an artist"):
        TP.songs_from([{"title": "", "artist": "A", "length": 60}])
    with pytest.raises(TP.BadList, match="song 1: length"):
        TP.songs_from([{"title": "O", "artist": "A", "length": float("nan")}])


# ------------------------------------------------------------------ the placing


def test_exact_gaps_set_every_boundary_and_agree() -> None:
    placed = TP.place(
        a_tape(100, 200, 50),
        TP.Music(0.0, 354.0, (gap(100.0, 102.0), gap(302.0, 304.0))),
    )
    assert [(s.start, s.end, s.verdict) for s in placed.songs] == [
        (0.0, 100.0, TP.AGREES),
        (102.0, 302.0, TP.AGREES),
        (304.0, 354.0, TP.AGREES),
    ]
    assert not placed.mismatch
    assert not placed.inside


def test_a_silent_passage_inside_a_song_stays_inside_it() -> None:
    """One song, 224 s on the list, measured as 9.4 s and then 214.6 s."""
    music = TP.Music(
        0.0,
        230.0 + 224.0 + 2.0 + 2.0 + 100.0 + 2.0,
        (gap(230.0, 232.0), gap(241.4, 243.4), gap(458.0, 460.0)),
    )
    placed = TP.place(a_tape(230, 224, 100), music)
    second = placed.songs[1]
    assert (second.start, second.end) == (232.0, 458.0)
    assert second.delta == 2.0
    assert second.verdict == TP.DOUBT
    assert "4:01 to 4:03" in second.reason
    assert "226.0 s with the silence and 224.0 s without" in second.reason
    assert placed.inside == ((2, 241.4, 243.4),)
    assert not placed.mismatch


def test_a_merged_song_is_doubt_even_when_its_music_matches_the_list() -> None:
    placed = TP.place(a_tape(224), TP.Music(0.0, 229.0, (gap(9.4, 14.4),)))
    assert placed.songs[0].delta == 5.0
    assert placed.songs[0].verdict == TP.DOUBT
    assert "0:09 to 0:14" in placed.songs[0].reason


def test_two_real_songs_listed_as_one_were_agreed_and_are_now_doubt() -> None:
    """Really 100 s then 200 s, on a list that says 300 s."""
    music = TP.Music(0.0, 404.0, (gap(100.0, 102.0), gap(302.0, 304.0)))
    placed = TP.place(a_tape(300, 100), music)
    assert [s.verdict for s in placed.songs] == [TP.DOUBT, TP.AGREES]
    assert (placed.songs[0].start, placed.songs[0].end) == (0.0, 302.0)
    assert "1:40 to 1:42" in placed.songs[0].reason
    assert not placed.songs[1].reason


def test_a_song_missing_from_the_list_merged_silently_and_is_now_doubt() -> None:
    music = TP.Music(0.0, 404.0, (gap(120.0, 122.0), gap(202.0, 204.0)))
    placed = TP.place(a_tape(200, 200), music)
    assert [s.verdict for s in placed.songs] == [TP.DOUBT, TP.AGREES]
    assert "2:00 to 2:02" in placed.songs[0].reason
    assert "missing from the list" in placed.songs[0].reason


def test_two_groupings_that_both_fit_were_agreed_and_now_doubt_both_songs() -> None:
    """199|3|198 against 200, 200: the middle piece could belong to either."""
    music = TP.Music(0.0, 404.0, (gap(199.0, 201.0), gap(204.0, 206.0)))
    placed = TP.place(a_tape(200, 200), music)
    assert [s.verdict for s in placed.songs] == [TP.DOUBT, TP.DOUBT]
    assert "could end at 3:24" in placed.songs[0].reason
    assert "could start at 3:26" in placed.songs[1].reason
    assert "2 groupings" in placed.mismatch


def test_a_cut_between_two_whole_pieces_is_doubt_if_a_grouping_moves_it() -> None:
    """100|3|4|50|48 against 100, 5, 100: the 3 s piece may end the first song."""
    music = TP.Music(
        0.0,
        209.0,
        (gap(100.0, 101.0), gap(104.0, 105.0), gap(109.0, 110.0), gap(160.0, 161.0)),
    )
    placed = TP.place(a_tape(100, 5, 100), music)
    assert placed.songs[0].end == 100.0
    assert placed.songs[0].verdict == TP.DOUBT
    assert "could end at 1:44" in placed.songs[0].reason
    assert "3 groupings" in placed.mismatch


def test_a_song_that_lands_on_a_gap_past_another_is_doubt_when_walking() -> None:
    """Too few gaps for the list, and the first song steps over one of them."""
    music = TP.Music(0.0, 500.0, (gap(50.0, 52.0), gap(100.0, 102.0)))
    placed = TP.place(a_tape(100, 100, 100, 100), music)
    assert placed.songs[0].end == 100.0
    assert placed.songs[0].verdict == TP.DOUBT
    assert "0:50 to 0:52" in placed.songs[0].reason


def test_fewest_doubts_outranks_smallest_error_when_grouping() -> None:
    """99|5|104|100 against 100, 100, 100.

    Merging the first two is 8 s off in all across two doubts. Merging the middle
    two is 10 s off, in one. The one doubt wins.
    """
    music = TP.Music(
        0.0, 311.0, (gap(99.0, 100.0), gap(105.0, 106.0), gap(210.0, 211.0))
    )
    placed = TP.place(a_tape(100, 100, 100), music)
    assert placed.songs[0].end == 99.0
    assert (placed.songs[1].start, placed.songs[1].end) == (100.0, 210.0)


def test_a_song_set_by_ear_across_a_silence_is_the_person_s_call() -> None:
    tape = a_tape(224)
    ear = replace(tape.songs[0], start=0.0, end=229.0, set_by=TP.EAR)
    placed = TP.place(
        replace(tape, songs=(ear,)), TP.Music(0.0, 229.0, (gap(9.4, 14.4),))
    )
    assert placed.songs[0].verdict == TP.AGREES
    assert not placed.songs[0].reason
    assert placed.inside == ((1, 9.4, 14.4),)


def test_a_reason_says_how_far_a_song_is_from_the_list() -> None:
    placed = TP.place(a_tape(100), TP.Music(0.0, 90.0, ()))
    assert "10.0 s shorter" in placed.songs[0].reason
    long_tape = TP.place(a_tape(100), TP.Music(0.0, 3 * 3600.0 + 5.0, ()))
    assert "longer" in long_tape.songs[0].reason


def test_a_silence_past_an_hour_is_named_with_its_hours() -> None:
    music = TP.Music(0.0, 7300.0, (gap(3700.0, 3702.0),))
    assert "1:01:40 to 1:01:42" in TP.place(a_tape(7298), music).songs[0].reason


def test_a_placement_that_fits_gives_no_reason() -> None:
    placed = TP.place(a_tape(100), TP.Music(0.0, 100.0, ()))
    assert placed.songs[0].reason == ""


def test_a_song_outside_the_tolerance_is_doubt_and_its_neighbours_are_not() -> None:
    off = TP.LENGTH_TOLERANCE + 0.5
    placed = TP.place(
        a_tape(100, 200, 50),
        TP.Music(0.0, 354.0 + off, (gap(100.0, 102.0), gap(302.0 + off, 304.0 + off))),
    )
    assert [s.verdict for s in placed.songs] == [TP.AGREES, TP.DOUBT, TP.AGREES]
    assert placed.songs[1].delta == off
    assert not placed.mismatch


def test_a_song_exactly_at_the_tolerance_still_agrees() -> None:
    placed = TP.place(a_tape(100), TP.Music(0.0, 100.0 + TP.LENGTH_TOLERANCE, ()))
    assert placed.songs[0].verdict == TP.AGREES


def test_a_length_is_never_scaled_to_fit_the_recording() -> None:
    placed = TP.place(a_tape(100, 100), TP.Music(0.0, 404.0, (gap(200.0, 204.0),)))
    assert [s.delta for s in placed.songs] == [100.0, 100.0]
    assert {s.length for s in placed.songs} == {100.0}


def test_a_list_out_of_order_is_a_mismatch_and_is_never_reordered() -> None:
    music = TP.Music(0.0, 604.0, (gap(300.0, 302.0), gap(502.0, 504.0)))
    placed = TP.place(a_tape(100, 200, 300), music)
    assert [s.title for s in placed.songs] == ["Song 1", "Song 2", "Song 3"]
    assert [s.start for s in placed.songs] == [0.0, 302.0, 504.0]
    assert [s.verdict for s in placed.songs] == [TP.DOUBT, TP.AGREES, TP.DOUBT]
    assert "nothing has been reordered" in placed.mismatch


def test_no_gaps_at_all_still_returns_a_flagged_placement() -> None:
    placed = TP.place(a_tape(100, 200, 50), TP.Music(0.0, 350.0, ()))
    assert [(s.start, s.end) for s in placed.songs] == [
        (0.0, 100.0),
        (100.0, 300.0),
        (300.0, 350.0),
    ]
    assert {s.verdict for s in placed.songs} == {TP.DOUBT}
    assert "only 1 stretches" in placed.mismatch


def test_one_missing_gap_leaves_the_songs_it_cannot_ground_in_doubt() -> None:
    placed = TP.place(
        a_tape(100, 200, 50, 80),
        TP.Music(0.0, 434.0, (gap(300.0, 302.0), gap(352.0, 354.0))),
    )
    assert [s.verdict for s in placed.songs] == [
        TP.DOUBT,
        TP.DOUBT,
        TP.AGREES,
        TP.AGREES,
    ]
    assert placed.songs[0].end == 100.0
    assert placed.songs[1].end == 300.0
    assert placed.mismatch


def test_a_recording_too_short_for_its_list_has_no_invented_time() -> None:
    placed = TP.place(a_tape(100, 100, 100), TP.Music(0.0, 150.0, ()))
    assert all(s.end is not None and s.end <= 150.0 for s in placed.songs)
    assert placed.mismatch


def test_a_recording_with_no_music_is_a_mismatch_not_a_crash() -> None:
    placed = TP.place(a_tape(100, 100), TP.Music(0.0, 0.0, ()))
    assert placed.mismatch
    assert {s.verdict for s in placed.songs} == {TP.DOUBT}


def test_a_boundary_set_by_ear_is_never_moved_by_placing() -> None:
    tape = a_tape(100, 200, 50)
    ear = replace(tape.songs[1], start=101.0, end=299.0, set_by=TP.EAR)
    tape = replace(tape, songs=(tape.songs[0], ear, tape.songs[2]))
    placed = TP.place(tape, TP.Music(0.0, 354.0, (gap(90.0, 103.0), gap(298.0, 304.0))))
    held = placed.songs[1]
    assert (held.start, held.end, held.set_by) == (101.0, 299.0, TP.EAR)
    assert (placed.songs[0].start, placed.songs[0].end) == (0.0, 90.0)
    assert (placed.songs[2].start, placed.songs[2].end) == (304.0, 354.0)


def test_placing_twice_leaves_an_ear_set_boundary_where_it_was() -> None:
    tape = a_tape(100, 200)
    ear = replace(tape.songs[0], start=1.5, end=99.5, set_by=TP.EAR)
    tape = replace(tape, songs=(ear, tape.songs[1]))
    music = TP.Music(0.0, 302.0, (gap(100.0, 102.0),))
    again = TP.place(TP.place(tape, music), music)
    assert (again.songs[0].start, again.songs[0].end) == (1.5, 99.5)


def test_a_new_list_is_refused_rather_than_losing_an_ear_set_boundary() -> None:
    tape = a_tape(100)
    tape = replace(tape, songs=(replace(tape.songs[0], set_by=TP.EAR),))
    with pytest.raises(TP.EarSet):
        TP.relist(tape, a_tape(90).songs)


def test_a_new_list_drops_what_was_found_for_the_old_one() -> None:
    placed = TP.place(a_tape(100), TP.Music(0.0, 200.0, ()))
    assert placed.mismatch
    fresh = TP.relist(placed, a_tape(200).songs)
    assert not fresh.mismatch
    assert fresh.music_end is None
    assert fresh.songs[0].start is None


def test_a_hundred_songs_place_in_order_and_sort_in_order() -> None:
    gaps = tuple(gap(n * 202.0 - 2.0, n * 202.0) for n in range(1, 100))
    placed = TP.place(a_tape(*[200.0] * 100), TP.Music(0.0, 100 * 202.0 - 2.0, gaps))
    assert {s.verdict for s in placed.songs} == {TP.AGREES}
    assert [s.number for s in placed.songs] == list(range(1, 101))

    names = [TP.file_number(s.number, 100) for s in placed.songs]
    assert names == sorted(names)
    assert names[10] == "011"
    assert names[99] == "100"


def test_a_short_list_keeps_the_two_digit_number_a_listing_expects() -> None:
    assert TP.file_number(7, 12) == "07"


# --------------------------------------------------------------------- the gaps


def test_gaps_are_taken_at_a_fixed_level_whatever_the_music_is() -> None:
    env = lane([(10, -20.0), (2, -70.0), (10, -20.0)])
    found = TP.gaps_of(env)
    assert found.start == 0.0
    assert found.end == 22.0
    assert len(found.gaps) == 1
    assert found.gaps[0].lo == pytest.approx(10.0)

    quiet = TP.gaps_of(lane([(10, -50.0), (2, -70.0), (10, -50.0)]))
    assert quiet.gaps == found.gaps


def test_a_passage_above_the_fixed_level_is_not_a_gap() -> None:
    assert not TP.gaps_of(lane([(10, -20.0), (2, -55.0), (10, -20.0)])).gaps


def test_lead_and_tail_silence_are_where_the_music_starts_and_stops() -> None:
    found = TP.gaps_of(lane([(3, DB_MIN), (10, -20.0), (4, DB_MIN)]))
    assert (found.start, found.end) == (3.0, 13.0)
    assert not found.gaps


@pytest.mark.parametrize("silence", [DB_MIN, float("-inf"), float("nan")])
def test_digital_silence_reads_as_a_gap_however_it_was_reported(silence: float) -> None:
    found = TP.gaps_of(lane([(10, -20.0), (2, silence), (10, -20.0)]))
    assert len(found.gaps) == 1


@pytest.mark.parametrize("silence", [DB_MIN, float("-inf")])
def test_a_recording_of_nothing_but_silence_has_no_music(silence: float) -> None:
    assert TP.gaps_of(lane([(5, silence)])) == TP.Music(0.0, 0.0, ())


def test_an_empty_lane_has_no_music() -> None:
    assert TP.gaps_of(Envelope((), 0.05)) == TP.Music(0.0, 0.0, ())


def test_the_threshold_the_album_flow_gets_from_a_call_is_unchanged() -> None:
    env = lane([(10, -20.0), (2, -60.0), (10, -20.0)])
    found = G.find(env, below=G.BELOW)
    assert found.threshold == -36.0
    assert [(round(g.lo, 2), round(g.hi, 2)) for g in found] == [(10.0, 11.95)]
    assert G.find(env, above=G.ABOVE).threshold == -48.0


def a_song(seconds: float, fade: float, floor: float = DB_MIN) -> list[tuple]:
    """Music, then `fade` seconds under the finding level, stepping toward `floor`."""
    return [(seconds - fade, -20.0), (fade / 2, -62.0), (fade / 2, floor + 4.0)]


FADE = 4.1
LONG_FADE = 8.55
LAST_FADE = 3.5
DIGITAL_ZERO = [(2.0, DB_MIN)]
LOW_NOISE = [(1.0, -70.0), (1.0, -69.0)]
SIX_MEASURED = [180.2, 159.4, 159.1, 226.7, 184.6, 198.8]
SIX_LISTED = [180.0, 160.0, 159.0, 226.0, 184.0, 198.0]


def a_faded_tape(measured: list[float], noisy: int = -1, wide: int = -1) -> Envelope:
    """Every song fades under the finding level; gap `noisy` never reaches zero,
    and gap `wide` is too long for the list to have counted it by accident."""
    segments: list[tuple] = list(DIGITAL_ZERO)
    for at, seconds in enumerate(measured):
        fade = LONG_FADE if at == 0 else FADE
        if at == len(measured) - 1:
            segments += [*a_song(seconds, LAST_FADE), *DIGITAL_ZERO]
        elif at == noisy:
            segments += [*a_song(seconds, fade, -70.0), *LOW_NOISE]
        elif at == wide:
            segments += [*a_song(seconds, fade), *DIGITAL_ZERO * 3]
        else:
            segments += [*a_song(seconds, fade), *DIGITAL_ZERO]
    return lane(segments)


def test_a_fade_under_the_finding_level_stays_with_its_song() -> None:
    music = TP.gaps_of(a_faded_tape(SIX_MEASURED, noisy=2))
    placed = TP.place(a_tape(*SIX_LISTED), music)
    assert [s.delta for s in placed.songs] == pytest.approx(
        [m - n for m, n in zip(SIX_MEASURED, SIX_LISTED, strict=True)], abs=0.11
    )
    assert {s.verdict for s in placed.songs} == {TP.AGREES}


def test_a_song_with_a_silence_inside_is_placed_whole_and_flagged() -> None:
    measured = [435.9, 9.5, 214.7, 248.1, 378.8, 250.1, 437.8, 418.4, 411.1, 325.2]
    measured += [331.6, 398.1]
    listed = [436.0, 224.0, 247.0, 379.0, 250.0, 437.0, 418.0, 411.0, 325.0, 331.0]
    listed += [398.0]
    placed = TP.place(a_tape(*listed), TP.gaps_of(a_faded_tape(measured, wide=1)))
    assert [s.verdict for s in placed.songs] == [TP.AGREES, TP.DOUBT, *[TP.AGREES] * 9]
    merged = placed.songs[1]
    assert merged.delta == pytest.approx(9.5 + 6.0 + 214.7 - 224.0, abs=0.11)
    assert re.search(r"224\.[1-3] s without", merged.reason)
    others = [s for s in placed.songs if s is not merged]
    wanted = [
        m - n for m, n in zip([435.9, *measured[3:]], [436.0, *listed[2:]], strict=True)
    ]
    assert [s.delta for s in others] == pytest.approx(wanted, abs=0.11)
    assert not placed.mismatch


def test_a_gap_reports_where_it_was_found_and_where_it_is_quiet() -> None:
    first = TP.gaps_of(a_faded_tape(SIX_MEASURED)).gaps[0]
    assert first.lo == pytest.approx(2.0 + SIX_MEASURED[0] - LONG_FADE, abs=0.06)
    assert first.quiet_lo == pytest.approx(2.0 + SIX_MEASURED[0], abs=0.06)
    assert first.quiet_lo - first.lo == pytest.approx(LONG_FADE, abs=0.11)


def test_a_gap_of_low_noise_is_judged_against_its_own_floor() -> None:
    music = TP.gaps_of(lane([(10, -20.0), (2, -65.0), *LOW_NOISE, (10, -20.0)]))
    assert music.gaps[0].lo == pytest.approx(10.0)
    assert music.gaps[0].quiet == pytest.approx((12.0, 14.0))


def test_the_first_and_last_songs_keep_their_fades_too() -> None:
    env = lane([(3, DB_MIN), (1, -80.0), (10, -20.0), (LAST_FADE, -70.0), (4, DB_MIN)])
    music = TP.gaps_of(env)
    assert (music.found_start, music.found_end) == (4.0, 14.0)
    assert music.start == 3.0
    assert music.end == 14.0 + LAST_FADE


def test_a_tape_that_runs_to_both_ends_has_no_outer_silence_to_walk() -> None:
    music = TP.gaps_of(lane([(10, -20.0)]))
    assert (music.start, music.end) == (music.found_start, music.found_end) == (0, 10)


# --------------------------------------------------------------- the dictionary


def test_a_document_from_before_quiet_interiors_still_reads() -> None:
    placed = TP.place(a_tape(100, 200), TP.Music(0.0, 302.0, (gap(100.0, 102.0),)))
    written = placed.to_dict()
    for added in ("quiet", "found_start", "found_end"):
        del written[added]
    assert TP.Tape.from_dict(written) == placed


def test_a_tape_survives_being_written_down() -> None:
    placed = TP.place(a_tape(100, 200), TP.Music(0.0, 302.0, (gap(100.0, 102.0),)))
    assert TP.Tape.from_dict(placed.to_dict()) == placed


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Mix: Vol. 2, Summer", "Mix-Vol.-2-Summer"),
        ("Mix..Up", "Mix.Up"),
        ("...", "tape"),
        ("日本語", "tape"),
        ("A" * 500, "A" * 100),
    ],
)
def test_a_tape_id_is_safe_for_any_name(name: str, expected: str) -> None:
    assert TP.tape_id(name) == expected
