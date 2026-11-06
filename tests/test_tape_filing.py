"""What filing a tape decides, in core. ADR-063."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from ripdoctor.core import filing as F
from ripdoctor.core import match as M
from ripdoctor.core import tape as TP


def from_a_release(
    release: str, number: int, title: str, recording: str = ""
) -> M.Proposal:
    return M.Proposal(
        M.MATCHED,
        choice=M.Choice(
            release=release,
            group="g",
            release_title=f"Record {release}",
            album_artist="A Band",
            date="1975",
            status="Official",
            track_title=title,
            number=number,
            disc=1,
            recording=recording or f"rec-{title}",
        ),
    )


def a_song(number: int, title: str, **kw: object) -> TP.Song:
    fields: dict[str, object] = {
        "artist": "A Band",
        "album": "A Record",
        "length": 200.0,
        "settled_by": M.PERSON,
        **kw,
    }
    return TP.Song(number, title, **fields)  # type: ignore[arg-type]


def a_tape(*songs: TP.Song) -> TP.Tape:
    return TP.Tape(id="mix", name="Mix", songs=songs)


def a_row(title: str = "One", length: float | None = 200.0, **kw: object) -> F.Row:
    fields: dict[str, object] = {
        "artist": "A Band",
        "album": "Elsewhere",
        "path": "/lib/one.flac",
        **kw,
    }
    return F.Row(title, length=length, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "row"),
    [
        ("A Band", "a band"),
        ("=Foo", "=foo"),
        (":Foo(", "x :FOO( y"),
        ("=~foo", "=~FOO"),
        ("Odd,", "Odd,"),
        ("Sun, Moon & Stars", "SUN, MOON & STARS"),
        ("\u00c9CHO", "\u00e9cho"),
        ("\u00c9ric", "\u00c9RIC"),
        ("a\\:b.$^*", "A\\:B.$^*"),
    ],
)
def test_a_query_term_finds_the_value_whatever_its_case_and_is_never_beets_syntax(
    value: str, row: str
) -> None:
    """SQLite folds ASCII alone, so a substring query missed an accented capital."""
    field, _, query = F.term("artist", value).partition("::")
    assert field == "artist"
    assert query.startswith("(?i)")
    assert re.search(query, row)
    assert "," not in query
    assert "\\:" not in query and ":" not in query


def test_a_query_term_is_the_value_and_not_a_pattern_of_it() -> None:
    query = F.term("artist", "A.B").partition("::")[2]
    assert not re.search(query, "AxB")


# ------------------------------------------------------------------- the plan


def test_songs_group_into_one_run_per_release_whatever_the_list_order() -> None:
    tape = a_tape(
        a_song(1, "One", proposal=from_a_release("r1", 4, "One")),
        a_song(2, "Two", proposal=from_a_release("r2", 2, "Two")),
        a_song(3, "Three", proposal=from_a_release("r1", 7, "Three")),
    )
    planned = F.runs(tape)
    assert [r.release for r in planned] == ["r1", "r2"]
    assert [(t.song.number, t.number) for t in planned[0].tracks] == [(1, 4), (3, 7)]


def test_a_pinned_track_takes_the_catalogue_s_title_and_number() -> None:
    song = a_song(1, "one (remaster)", proposal=from_a_release("r1", 4, "One"))
    track = F.runs(a_tape(song))[0].tracks[0]
    assert (track.number, track.title) == (4, "One")


def test_from_list_songs_of_one_album_share_one_run_however_their_case_differs() -> (
    None
):
    tape = a_tape(
        a_song(1, "One"),
        a_song(2, "Two", artist="A BAND", album="a record"),
        a_song(3, "Three", album="Another Record"),
    )
    planned = F.runs(tape)
    assert [(r.release, r.artist, r.album) for r in planned] == [
        ("", "A Band", "A Record"),
        ("", "A Band", "Another Record"),
    ]
    assert [t.number for t in planned[0].tracks] == [1, 2]


def test_a_retry_plans_only_what_is_unfinished_and_keeps_its_from_list_number() -> None:
    tape = a_tape(
        a_song(1, "One", filing=F.FILED),
        a_song(2, "Two", filing=F.FAILED),
        a_song(3, "Three", filing=F.ALREADY, album="Finished"),
    )
    planned = F.runs(tape)
    assert len(planned) == 1
    assert [(t.song.number, t.number) for t in planned[0].tracks] == [(2, 2)]


def test_a_song_not_confirmed_is_still_to_do() -> None:
    assert F.runs(a_tape(a_song(1, "One", filing=F.UNKNOWN)))


def test_a_skipped_song_is_in_no_run() -> None:
    assert F.runs(a_tape(a_song(1, "One", skip=True))) == ()


# ------------------------------------------------------------------ the pre-check


def test_a_row_with_the_recording_id_means_owned_whatever_it_is_called() -> None:
    said = F.precheck(a_song(1, "One"), [a_row("Something Else", None)], [])
    assert (said.action, said.path) == (F.ALREADY, "/lib/one.flac")


def test_owned_by_cleaned_title_artist_and_length() -> None:
    said = F.precheck(
        a_song(1, "One - 2011 Remaster", artist="A Band feat. Somebody"),
        [],
        [a_row("ONE", 202.0)],
    )
    assert said.action == F.ALREADY
    assert "title, artist and length" in said.note


def a_pinned_song() -> TP.Song:
    return a_song(1, "One", proposal=from_a_release("r1", 1, "One"))


def test_a_row_with_another_recording_id_never_owns_a_pinned_song() -> None:
    row = a_row("One", recording="rec-Live", album="Live Somewhere")
    assert F.precheck(a_pinned_song(), [], [row]).action == F.FILE


def test_a_row_with_no_recording_id_is_for_a_person_to_confirm_on_a_pinned_song() -> (
    None
):
    said = F.precheck(a_pinned_song(), [], [a_row("One")])
    assert said.action == F.FAILED
    assert "/lib/one.flac" in said.note and "rec-One" in said.note
    assert "listen" in said.note and "skip" in said.note


def test_a_song_with_no_id_of_its_own_is_owned_whatever_id_the_row_has() -> None:
    said = F.precheck(a_song(1, "One"), [], [a_row("One", recording="rec-Any")])
    assert said.action == F.ALREADY


def test_a_live_album_s_same_title_is_refused_by_its_length() -> None:
    said = F.precheck(a_song(1, "One"), [], [a_row("One", 260.0, album="Live")])
    assert said.action == F.FILE


def test_a_title_by_another_artist_is_not_owned() -> None:
    said = F.precheck(a_song(1, "One"), [], [a_row("One", artist="A Band Tribute Act")])
    assert said.action == F.FILE


def test_two_credits_that_each_end_in_a_bracket_are_not_the_same_artist() -> None:
    row = a_row("One", artist="Someone Else (Live Band)")
    said = F.precheck(a_song(1, "One", artist="A Band (Trio)"), [], [row])
    assert said.action == F.FILE


def test_a_title_match_with_no_length_to_check_is_flagged_not_assumed() -> None:
    said = F.precheck(a_song(1, "One"), [], [a_row("One", None)])
    assert said.action == F.FAILED
    assert "no length to compare" in said.note
    assert "/lib/one.flac" in said.note


def test_a_row_whose_file_is_gone_is_stale_not_owned() -> None:
    gone = a_row(present=False)
    for said in (
        F.precheck(a_song(1, "One"), [gone], []),
        F.precheck(a_song(1, "One"), [], [gone]),
    ):
        assert said.action == F.FAILED
        assert "stale" in said.note


def test_a_row_not_in_the_library_is_not_owned_though_its_file_is_there() -> None:
    astray = a_row(home=False)
    for said in (
        F.precheck(a_song(1, "One"), [astray], []),
        F.precheck(a_song(1, "One"), [], [astray]),
    ):
        assert said.action == F.FAILED
        assert "not in the library" in said.note


def test_one_present_row_among_stale_ones_is_owned() -> None:
    gone, here = a_row(present=False, path="/gone"), a_row(path="/here")
    assert F.precheck(a_song(1, "One"), [gone, here], []).path == "/here"


def test_a_from_list_song_with_no_album_is_refused_rather_than_given_one() -> None:
    said = F.precheck(a_song(1, "One", album=""), [], [])
    assert said.action == F.FAILED
    assert "no album" in said.note


def test_a_song_with_no_album_but_a_release_files() -> None:
    song = a_song(1, "One", album="", proposal=from_a_release("r1", 1, "One"))
    assert F.precheck(song, [], []).action == F.FILE


@pytest.mark.parametrize(
    ("said", "seconds"),
    [
        ("3:45", 225.0),
        ("71:02", 4262.0),
        ("225.5", 225.5),
        ("0:00", None),
        ("", None),
        ("nan", None),
        ("1:02:03", None),
    ],
)
def test_beets_lengths_read_as_seconds_or_as_not_known(
    said: str, seconds: float | None
) -> None:
    assert F.row_length(said) == seconds


# -------------------------------------------------------------------- the arrival


def test_a_pinned_song_arrives_by_its_recording_id_alone() -> None:
    song = a_song(1, "One", proposal=from_a_release("r1", 1, "One"))
    assert F.arrived(song, [a_row("Whatever")]) == "/lib/one.flac"


def twins() -> tuple[TP.Song, F.Row, F.Row]:
    first = a_row("One", 100.0, album="A Record", path="/lib/1.flac", track=1)
    second = a_row("One", 200.0, album="A Record", path="/lib/2.flac", track=2)
    return a_song(2, "One"), first, second


def test_the_same_title_twice_on_an_album_is_told_apart_by_track_number() -> None:
    song, first, second = twins()
    assert F.arrivals(song, [first, second], track=2) == ["/lib/2.flac"]
    assert F.arrivals(song, [first, second], track=1) == ["/lib/1.flac"]


def test_a_release_with_no_recording_id_tells_a_title_apart_by_track_number() -> None:
    proposal = from_a_release("r1", 2, "One")
    empty = replace(proposal.choice, recording="")  # type: ignore[arg-type]
    song = a_song(2, "One", proposal=replace(proposal, choice=empty))
    _, first, second = twins()
    first = replace(first, length=200.0)
    assert F.arrivals(song, [first, second], track=2) == [second.path]
    assert F.arrivals(song, [first], track=2) == [first.path]


def test_a_from_list_song_is_never_given_a_row_of_another_track_number() -> None:
    song, first, _ = twins()
    assert F.arrivals(song, [first], track=2) == []


def test_a_row_another_song_claimed_is_not_an_arrival() -> None:
    song, first, second = twins()
    assert F.arrivals(song, [first, second], taken={second.path}, track=0) == [
        first.path
    ]


def test_with_no_track_numbers_the_same_title_is_told_apart_by_length() -> None:
    song = a_song(1, "One", length=100.0)
    short = a_row("One", 100.0, album="A Record", path="/lib/s.flac")
    long = a_row("One", 300.0, album="A Record", path="/lib/l.flac")
    assert F.arrivals(song, [short, long]) == [short.path]


def test_rows_nothing_tells_apart_are_all_named_and_none_chosen() -> None:
    song = a_song(1, "One")
    rows = [a_row("One", album="A Record", path=f"/lib/{n}.flac") for n in "ab"]
    assert F.arrivals(song, rows) == ["/lib/a.flac", "/lib/b.flac"]
    assert F.arrived(song, rows) == ""


def test_a_row_another_song_on_the_tape_answers_for_is_not_owned_by_this_one() -> None:
    assert (
        F.precheck(a_song(2, "One"), [], [a_row()], {"/lib/one.flac"}).action == F.FILE
    )
    said = F.precheck(a_pinned_song(), [a_row()], [], {"/lib/one.flac"})
    assert said.action == F.FAILED and "another song" in said.note


def test_track_numbers_are_the_catalogue_s_or_a_place_among_the_album_s_songs() -> None:
    tape = a_tape(
        a_song(1, "One", proposal=from_a_release("r1", 4, "One")),
        a_song(2, "Two"),
        a_song(3, "Three", skip=True),
        a_song(4, "Four"),
    )
    assert F.track_numbers(tape) == {1: 4, 2: 1, 4: 2}


def test_a_row_without_its_file_is_not_an_arrival() -> None:
    song = a_song(1, "One", proposal=from_a_release("r1", 1, "One"))
    assert F.arrived(song, [a_row(present=False)]) == ""


def test_a_row_not_in_the_library_is_not_an_arrival_and_says_where_it_went() -> None:
    song = a_song(1, "One", proposal=from_a_release("r1", 1, "One"))
    row = a_row(home=False, path="/elsewhere/one.flac")
    assert F.arrived(song, [row]) == ""
    assert F.astray(song, [row]) == "/elsewhere/one.flac"
    assert F.astray(song, [row], before={row.path}) == ""
    assert F.astray(song, [a_row()]) == ""


def test_a_release_with_no_recording_id_is_read_back_by_title() -> None:
    proposal = from_a_release("r1", 1, "One")
    empty = replace(proposal.choice, recording="")  # type: ignore[arg-type]
    song = a_song(1, "One", proposal=replace(proposal, choice=empty))
    assert F.arrived(song, [a_row("Two")]) == ""
    assert F.arrived(song, [a_row("One")]) == "/lib/one.flac"


def test_a_from_list_song_arrives_by_title_and_album() -> None:
    song = a_song(1, "One")
    assert F.arrived(song, [a_row("One", album="Elsewhere")]) == ""
    assert F.arrived(song, [a_row("One", album="A Record")]) == "/lib/one.flac"


def test_a_row_that_was_there_before_the_import_is_not_the_arrival() -> None:
    song = a_song(1, "One")
    row = a_row("One", album="A Record")
    assert F.arrived(song, [row], before={row.path}) == ""


def test_a_second_album_row_is_said_when_the_album_is_there_already() -> None:
    run = F.Run("", "A Band", "A Record", ())
    assert F.second_row(run, []) == ""
    assert F.second_row(run, [a_row(album="A Record Revisited")]) == ""
    assert "one more album entry" in F.second_row(run, [a_row(album="a record")])


# ---------------------------------------------------------------- the song's state


def test_failed_may_be_retried_and_finished_stays_finished() -> None:
    tape = F.recorded(a_tape(a_song(1, "One")), 1, F.FAILED, note="beets refused")
    assert (tape.songs[0].filing, tape.songs[0].filing_note) == (
        F.FAILED,
        "beets refused",
    )
    tape = F.recorded(tape, 1, F.FILED, "/lib/one.flac")
    assert (tape.songs[0].path, tape.songs[0].filing_note) == ("/lib/one.flac", "")
    with pytest.raises(ValueError, match="already filed"):
        F.recorded(tape, 1, F.FAILED)


def test_not_knowing_keeps_the_mark_against_a_re_import_and_an_answer_clears_it() -> (
    None
):
    tape = a_tape(a_song(1, "One", import_begun=True))
    tape = F.recorded(tape, 1, F.UNKNOWN, note="locked")
    assert tape.songs[0].import_begun
    assert (
        F.recorded(tape, 1, F.FAILED, note="x", keep_begun=True).songs[0].import_begun
    )
    tape = F.recorded(tape, 1, F.FAILED, note="beets did not take it")
    assert not tape.songs[0].import_begun


def test_with_no_id_an_interrupted_import_is_found_by_the_list_or_cut_length() -> None:
    cut = TP.CutRecord(TP.CUT, "01 One.flac", 10.0, 15.0)
    song = a_song(1, "One", length=30.0, cut=cut)
    here = a_row("One", 5.0, album="A Record")
    listed = a_row("One", 31.0, album="A Record", path="/lib/listed.flac")
    stranger = a_row("One", 599.0, album="A Record", path="/lib/stranger.flac")
    assert F.recovered(song, [stranger]) == []
    assert F.recovered(song, [stranger, here]) == ["/lib/one.flac"]
    assert F.recovered(song, [stranger, listed]) == ["/lib/listed.flac"]
    assert F.recovered(song, [a_row("One", None, album="A Record")]) == []


def test_an_interrupted_import_with_an_id_is_found_by_it_alone() -> None:
    assert F.recovered(a_pinned_song(), [a_row("Whatever", None)]) == ["/lib/one.flac"]


def test_an_outcome_survives_the_document() -> None:
    tape = F.recorded(a_tape(a_song(1, "One")), 1, F.ALREADY, "/lib/one.flac", "by id")
    song = TP.Song.from_dict(tape.songs[0].to_dict())
    assert (song.filing, song.path, song.filing_note) == (
        F.ALREADY,
        "/lib/one.flac",
        "by id",
    )


def test_outcomes_put_failures_first_and_name_a_skip() -> None:
    tape = a_tape(
        a_song(1, "One", filing=F.FILED),
        a_song(2, "Two", skip=True),
        a_song(3, "Three", filing=F.FAILED),
        a_song(4, "Four", filing=F.ALREADY),
        a_song(5, "Five"),
        a_song(6, "Six", filing=F.UNKNOWN),
    )
    assert [(o["number"], o["state"]) for o in F.outcomes(tape)] == [
        (3, F.FAILED),
        (6, F.UNKNOWN),
        (5, TP.PENDING),
        (1, F.FILED),
        (4, F.ALREADY),
        (2, TP.SKIPPED),
    ]


def test_a_song_skipped_after_it_was_filed_still_says_filed() -> None:
    assert F.status(a_song(1, "One", filing=F.FILED, skip=True)) == F.FILED


def test_all_filed_counts_owned_songs_and_ignores_skips() -> None:
    assert not F.all_filed(a_tape(a_song(1, "One")))
    assert F.all_filed(
        a_tape(
            a_song(1, "One", filing=F.FILED),
            a_song(2, "Two", filing=F.ALREADY),
            a_song(3, "Three", skip=True),
        )
    )
    assert not F.all_filed(a_tape(a_song(1, "One", skip=True)))


def test_a_row_only_a_person_can_vouch_for_is_named_on_the_verdict() -> None:
    said = F.precheck(a_pinned_song(), [], [a_row("One", path="/lib/x.flac")])
    assert (said.action, said.path) == (F.FAILED, "")
    assert said.unconfirmed == "/lib/x.flac"


def test_a_later_outcome_forgets_the_row_that_was_waiting_on_a_person() -> None:
    tape = a_tape(a_song(1, "One"))
    asked = F.recorded(tape, 1, F.FAILED, note="?", unconfirmed="/lib/x.flac")
    assert asked.songs[0].unconfirmed == "/lib/x.flac"
    assert not F.recorded(asked, 1, F.FAILED, note="other").songs[0].unconfirmed


def test_a_person_owning_a_copy_is_finished_and_theirs() -> None:
    begun = a_song(1, "One", settled_by=M.CATALOGUE, import_begun=True)
    tape = F.recorded(
        a_tape(begun), 1, F.FAILED, unconfirmed="/lib/x.flac", keep_begun=True
    )
    song = F.person_owns(tape, 1).songs[0]
    assert (song.filing, song.path) == (F.ALREADY, "/lib/x.flac")
    assert song.settled_by == M.PERSON
    assert not song.unconfirmed and not song.import_begun
    assert F.outcomes(F.person_owns(tape, 1))[0]["settled_by"] == M.PERSON


@pytest.mark.parametrize("state", [TP.PENDING, F.FAILED])
def test_a_song_sent_to_beets_with_no_answer_yet_keeps_its_release(state: str) -> None:
    """A server dying mid-import left this open: another choice would look for another
    recording, find no row, and import a second copy of the song."""
    song = a_song(1, "One", filing=state, import_begun=True)
    assert "press File again" in F.choice_refusal(song)
    assert not F.choice_refusal(replace(song, import_begun=False))


# ------------------------------------------------------------- moving by hand


def three_placed(**filing: str) -> TP.Tape:
    """100 s songs at 0-100, 102-202 and 204-304."""
    gaps = (TP.Gap(100.0, 102.0, 100.0, 102.0), TP.Gap(202.0, 204.0, 202.0, 204.0))
    songs = tuple(
        a_song(n, f"Song {n}", length=100.0, filing=filing.get(f"s{n}", TP.PENDING))
        for n in (1, 2, 3)
    )
    tape = TP.Tape(id="mix", name="Mix", duration=310.0, songs=songs)
    return TP.place(tape, TP.Music(0.0, 304.0, gaps))


def test_a_move_by_hand_is_the_person_s_and_nothing_afterwards_moves_it() -> None:
    tape = TP.set_edges(three_placed(), 2, 101.0, 202.5)
    song = tape.songs[1]
    assert (song.start, song.end, song.set_by) == (101.0, 202.5, TP.EAR)
    again = TP.place(tape, TP.music_of(tape))
    beside = TP.set_edges(again, 1, 0.5, 100.5)
    for later in (again, beside):
        assert (later.songs[1].start, later.songs[1].end) == (101.0, 202.5)


@pytest.mark.parametrize(
    ("number", "start", "end", "said"),
    [
        (2, 99.0, 202.0, "overlaps song 1, which ends at 1:40"),
        (2, 102.0, 205.0, "overlaps song 3, which starts at 3:24"),
        (1, 0.0, 150.0, "overlaps song 2, which starts at 1:42"),
        (3, 150.0, 304.0, "overlaps song 2, which ends at 3:22"),
        (3, 204.0, 311.0, "does not fit"),
        (1, -1.0, 100.0, "does not fit"),
        (2, 203.0, 202.0, "does not fit"),
    ],
)
def test_a_move_by_hand_past_a_neighbour_or_the_ends_is_refused(
    number: int, start: float, end: float, said: str
) -> None:
    with pytest.raises(TP.BadEdge, match=said):
        TP.set_edges(three_placed(), number, start, end)


def test_an_edge_left_alone_is_never_what_a_move_is_refused_for() -> None:
    tape = three_placed()
    overlapping = replace(tape.songs[1], start=99.0)
    tape = replace(tape, songs=(tape.songs[0], overlapping, tape.songs[2]))
    assert TP.set_edges(tape, 2, 99.0, 201.0).songs[1].end == 201.0


def test_the_edge_moving_away_from_its_partner_can_always_go_first() -> None:
    """Song 1 really ends at 90, inside what was taken for it: its own end goes back,
    and then song 2's start can come to it, where dragging that first is refused."""
    gaps = (
        TP.Gap(90.0, 92.0, 90.0, 92.0),
        TP.Gap(100.0, 102.0, 100.0, 102.0),
    )
    songs = (
        a_song(1, "One", length=100.0),
        a_song(2, "Two", length=100.0),
    )
    tape = TP.place(
        TP.Tape(id="mix", name="Mix", duration=210.0, songs=songs),
        TP.Music(0.0, 202.0, gaps),
    )
    assert tape.songs[1].start == 102.0
    with pytest.raises(TP.BadEdge, match="overlaps song 1"):
        TP.set_edges(tape, 2, 92.0, 202.0)
    moved = TP.set_edges(tape, 1, 0.0, 90.0)
    assert moved.songs[1] == tape.songs[1]
    assert TP.set_edges(moved, 2, 92.0, 202.0).songs[1].start == 92.0


@pytest.mark.parametrize("filing", [F.FILED, F.ALREADY, F.UNKNOWN])
def test_a_song_the_library_holds_or_may_hold_is_not_moved_by_hand(filing: str) -> None:
    tape = three_placed(s2=filing)
    with pytest.raises(TP.BadEdge, match=r"song 2 .*its boundaries stay"):
        TP.set_edges(tape, 2, 102.5, 202.0)


@pytest.mark.parametrize("filing", [TP.PENDING, F.FAILED])
def test_a_song_lost_at_beets_keeps_its_edges_or_the_old_copy_would_pass_for_the_new(
    filing: str,
) -> None:
    tape = three_placed()
    song = replace(tape.songs[1], filing=filing, import_begun=True)
    tape = replace(tape, songs=(tape.songs[0], song, tape.songs[2]))
    with pytest.raises(TP.BadEdge, match=r"song 2 .*press File again"):
        TP.set_edges(tape, 2, 102.5, 202.0)
    assert TP.set_edges(tape, 1, 0.0, 90.0).songs[1] == song


def test_a_song_whose_filing_failed_can_still_be_moved() -> None:
    tape = three_placed(s2=F.FAILED)
    assert TP.set_edges(tape, 2, 102.5, 202.0).songs[1].start == 102.5


def test_a_move_beside_a_filed_song_leaves_it_and_may_not_pass_it() -> None:
    tape = three_placed(s3=F.FILED)
    assert TP.set_edges(tape, 2, 102.0, 190.0).songs[2] == tape.songs[2]
    with pytest.raises(TP.BadEdge, match=r"overlaps song 3.*stays where it is"):
        TP.set_edges(tape, 2, 102.0, 260.0)


def test_a_failed_song_beside_a_filed_one_could_only_stay_and_can_now_move() -> None:
    """Pulling its end into its own music took the start of the filed song with it, so
    it was refused, and the only answers were the silence between them or a skip."""
    tape = three_placed(s2=F.FAILED, s3=F.FILED)
    moved = TP.set_edges(tape, 2, 102.0, 195.0)
    assert moved.songs[1].end == 195.0
    assert moved.songs[2] == tape.songs[2]


def test_a_move_that_leaves_a_filed_neighbour_where_it_is_goes_through() -> None:
    tape = three_placed(s3=F.FILED)
    moved = TP.set_edges(tape, 2, 102.0, 202.5)
    assert (moved.songs[2].start, moved.songs[2].end) == (204.0, 304.0)


def test_a_move_for_a_song_not_on_the_list_is_refused() -> None:
    with pytest.raises(TP.BadEdge, match="no song 9"):
        TP.set_edges(three_placed(), 9, 1.0, 2.0)
