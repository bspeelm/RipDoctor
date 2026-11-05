"""What filing a tape decides, in core. ADR-063."""

from __future__ import annotations

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
    assert "two album rows" in F.second_row(run, [a_row(album="a record")])


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
    )
    assert [(o["number"], o["state"]) for o in F.outcomes(tape)] == [
        (3, F.FAILED),
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
