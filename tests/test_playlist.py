"""A playlist's exact text, name and answers, in core. ADR-063."""

from __future__ import annotations

import posixpath

import pytest

from ripdoctor.core import filing as F
from ripdoctor.core import playlist as P
from ripdoctor.core import tape as TP
from ripdoctor.core.naming import is_token
from tests.test_tape_filing import a_row, a_song, a_tape, from_a_release

LIB = "/music/library"


def here(number: int, path: str) -> P.Entry:
    return P.Entry(number, path)


# ------------------------------------------------------------------ the exact text


def test_the_text_is_the_header_then_relative_lines_in_list_order_with_lf() -> None:
    done = P.render(
        "Mix: Vol. 1, Side A",
        [here(1, f"{LIB}/B/Two/02 B.flac"), here(2, "A/One/01 A.flac")],
        LIB,
    )
    assert done.text == (
        "#EXTM3U\n#PLAYLIST:Mix: Vol. 1, Side A\n"
        "../B/Two/02 B.flac\n../A/One/01 A.flac\n"
    )
    assert done.songs == 2 and done.left_out == ()


@pytest.mark.parametrize(
    "inside",
    [
        "A Band/A Record/05 Song With Spaces.flac",
        "A Band/#1 Record/#5 Hash.flac",
        "A Band/100% Record/05 50%25 off.flac",
        ".hidden band/..dotted/.leading.flac",
        "Sigur Rós/Ágætis byrjun/01 Svefn-g-englar.flac",
        "日本語/アルバム/01 歌.flac",
    ],
)
def test_nasty_names_go_through_literally_and_resolve_from_the_playlist_directory(
    inside: str,
) -> None:
    done = P.render("Mix", [here(1, f"{LIB}/{inside}")], LIB)
    line = done.text.splitlines()[2]
    assert line == f"../{inside}"
    folder = f"{LIB}/{P.FOLDER}"
    assert posixpath.normpath(posixpath.join(folder, line)) == f"{LIB}/{inside}"


def test_non_ascii_survives_as_utf8_bytes_with_no_bom() -> None:
    data = P.render("Mixtâpe", [here(1, f"{LIB}/Rós/01 Ó.flac")], LIB).text.encode()
    assert data.startswith(b"#EXTM3U\n#PLAYLIST:Mixt\xc3\xa2pe\n")
    assert b"../R\xc3\xb3s/01 \xc3\x93.flac\n" in data


def test_a_file_beside_the_playlist_has_no_climb_and_a_hash_is_not_a_comment() -> None:
    done = P.render(
        "Mix", [here(1, f"{LIB}/Playlists/a.flac"), here(2, "Playlists/#b.flac")], LIB
    )
    assert done.text.splitlines()[2:] == ["a.flac", "./#b.flac"]


@pytest.mark.parametrize(
    "path",
    [
        "/music/other/01 A.flac",
        "/music/library-two/01 A.flac",
        "../01 A.flac",
        f"{LIB}/A/../../01 A.flac",
        LIB,
    ],
)
def test_a_path_outside_the_library_is_an_error(path: str) -> None:
    with pytest.raises(P.OutsideLibrary):
        P.render("Mix", [here(1, path)], LIB)


def test_a_song_with_no_line_is_named_by_number_and_reason() -> None:
    done = P.render(
        "Mix",
        [
            P.Entry(1, state=TP.SKIPPED, why="skipped"),
            here(2, "A/02.flac"),
            P.Entry(3, state=P.MISSING, why="not there"),
        ],
        LIB,
    )
    assert done.text == "#EXTM3U\n#PLAYLIST:Mix\n../A/02.flac\n"
    assert done.left_out == (
        P.Entry(1, state=TP.SKIPPED, why="skipped"),
        P.Entry(3, state=P.MISSING, why="not there"),
    )


def test_a_line_break_cannot_forge_a_line_in_a_name_or_a_path() -> None:
    done = P.render("Mix\n../evil", [here(1, "A/one\ntwo.flac")], LIB)
    assert done.text == "#EXTM3U\n#PLAYLIST:Mix ../evil\n"
    assert done.left_out[0].number == 1


def test_a_file_declares_the_playlist_it_is_only_by_its_header() -> None:
    assert P.declares("#EXTM3U\n#PLAYLIST:Mix\n../a.flac\n", "Mix")
    assert not P.declares("#EXTM3U\n#PLAYLIST:Mixed\n", "Mix")


# -------------------------------------------------------------------- the filename


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        ".hidden",
        "..",
        "-rf",
        "a/b\\c",
        "Mix: Vol. 1, Side A.",
        "日本語",
        "x" * 500,
        "...",
    ],
)
def test_any_name_makes_one_visible_path_component(name: str) -> None:
    made = P.file_name(name, "key")
    assert is_token(made)
    assert made.endswith(".m3u8") and not made.startswith((".", "-"))
    assert "/" not in made and "\\" not in made and ".." not in made


def test_the_filename_is_stable_across_runs() -> None:
    assert P.file_name("Mix: Vol. 1", "mix@5.0") == "Mix-Vol.-1-b278475e2614.m3u8"


def test_distinct_names_that_clean_up_alike_never_share_a_file() -> None:
    names = ["A:B", "A,B", "A/B", "A B", "A-B", "A.B", "a:b", "A:B.", "日本語", "中文"]
    assert len({P.file_name(n, "key") for n in names}) == len(names)


def test_two_tapes_of_one_name_never_share_a_file() -> None:
    first = TP.Tape(id="mix", name="Mix", created=1.0)
    second = TP.Tape(id="mix-2", name="Mix", created=1.0)
    reborn = TP.Tape(id="mix", name="Mix", created=2.0)
    assert len({P.file_of(first), P.file_of(second), P.file_of(reborn)}) == 3


# -------------------------------------------------------- what filing recorded


def test_after_filing_a_recorded_path_counts_only_if_the_file_is_there_now() -> None:
    tape = a_tape(
        a_song(1, "One", filing=F.FILED, path="/l/1.flac"),
        a_song(2, "Two", filing=F.ALREADY, path="/l/2.flac"),
    )
    found = P.from_recorded(tape, {"/l/1.flac"})
    assert found[0] == P.Entry(1, "/l/1.flac")
    assert found[1].state == P.MISSING and "/l/2.flac" in found[1].why


def test_a_failed_pending_or_skipped_song_is_said_and_not_dropped() -> None:
    tape = a_tape(
        a_song(1, "One", filing=F.FAILED, filing_note="beets refused it"),
        a_song(2, "Two"),
        a_song(3, "Three", skip=True, filing=F.FILED, path="/l/3.flac"),
    )
    found = P.from_recorded(tape, {"/l/3.flac"})
    assert [e.state for e in found] == [P.NOT_FILED, P.NOT_FILED, TP.SKIPPED]
    assert "beets refused it" in found[0].why
    assert found[1].why == "not filed yet"


# ------------------------------------------------------------------- re-resolving


def held(path: str, recording: str = "", release: str = "", **kw: object) -> F.Row:
    return a_row(path=path, recording=recording, release=release, **kw)


def filed_song(**kw: object) -> TP.Song:
    fields = {"filing": F.FILED, "path": "/l/old.flac", **kw}
    return a_song(1, "One", proposal=from_a_release("rel", 1, "One"), **fields)


def test_a_recording_id_finds_the_song_wherever_it_went() -> None:
    found = P.resolve(filed_song(), [held("/l/new.flac", "rec-One", "rel")])
    assert found == P.Entry(1, "/l/new.flac")


def test_a_row_whose_file_is_gone_is_missing_and_names_the_row() -> None:
    found = P.resolve(filed_song(), [held("/l/x.flac", "rec-One", present=False)])
    assert found.state == P.MISSING and "/l/x.flac" in found.why


def test_no_row_at_all_is_missing_for_a_filed_song() -> None:
    found = P.resolve(filed_song(), [held("/l/x.flac", "rec-Other", title="Other")])
    assert found.state == P.MISSING and found.path == ""


def test_a_failed_song_with_no_row_keeps_its_reason() -> None:
    song = filed_song(filing=F.FAILED, path="", filing_note="beets refused it")
    found = P.resolve(song, [])
    assert found.state == P.NOT_FILED and "beets refused it" in found.why


def test_one_recording_on_two_albums_is_settled_by_the_chosen_release() -> None:
    rows = [
        held("/l/comp.flac", "rec-One", "other"),
        held("/l/a.flac", "rec-One", "rel"),
    ]
    assert P.resolve(filed_song(), rows).path == "/l/a.flac"


def test_one_recording_on_two_other_albums_is_settled_by_the_proved_path() -> None:
    rows = [held("/l/old.flac", "rec-One", "x"), held("/l/b.flac", "rec-One", "y")]
    assert P.resolve(filed_song(), rows).path == "/l/old.flac"


def test_two_rows_nothing_can_tell_apart_are_ambiguous_and_both_named() -> None:
    rows = [held("/l/x.flac", "rec-One", "x"), held("/l/y.flac", "rec-One", "y")]
    found = P.resolve(filed_song(), rows)
    assert found.state == P.AMBIGUOUS and found.path == ""
    assert "/l/x.flac" in found.why and "/l/y.flac" in found.why


def test_a_song_owned_by_name_is_found_at_its_path_under_a_row_of_another_id() -> None:
    song = filed_song(filing=F.ALREADY, path="/l/owned.flac")
    assert P.resolve(song, [held("/l/owned.flac", "rec-Else")]).path == "/l/owned.flac"


def listed_song(**kw: object) -> TP.Song:
    fields = {"filing": F.FILED, "path": "/l/old.flac", "album": "A Record", **kw}
    return a_song(1, "One", **fields)


def test_with_no_id_title_artist_and_album_find_it() -> None:
    rows = [
        held("/l/live.flac", album="Live Somewhere"),
        held("/l/two.flac", title="Two", album="A Record"),
        held("/l/new.flac", album="A Record"),
    ]
    assert P.resolve(listed_song(), rows).path == "/l/new.flac"


def test_with_no_id_a_row_of_another_length_is_a_stranger_of_the_same_name() -> None:
    row = held("/l/x.flac", album="A Record", length=599.0)
    assert P.resolve(listed_song(), [row]).state == P.MISSING


def test_with_no_id_a_row_with_no_length_is_not_evidence() -> None:
    row = held("/l/x.flac", album="A Record", length=None)
    assert P.resolve(listed_song(), [row]).state == P.MISSING


def test_a_song_that_did_not_file_is_not_found_by_its_name() -> None:
    song = listed_song(filing=F.FAILED, path="", filing_note="beets refused it")
    found = P.resolve(song, [held("/l/x.flac", album="A Record")])
    assert found.state == P.NOT_FILED and "beets refused it" in found.why


def test_with_no_id_two_rows_of_one_album_are_ambiguous_and_never_guessed() -> None:
    rows = [held("/l/x.flac", album="A Record"), held("/l/y.flac", album="A Record")]
    assert P.resolve(listed_song(), rows).state == P.AMBIGUOUS


def test_with_no_id_a_row_with_no_album_is_not_this_song() -> None:
    assert P.resolve(listed_song(), [held("/l/x.flac", album="")]).state == P.MISSING


def test_a_skipped_song_is_never_asked_about_and_stays_skipped() -> None:
    song = filed_song(skip=True)
    assert P.resolve(song, [held("/l/x.flac", "rec-One")]).state == TP.SKIPPED
    assert P.to_ask(a_tape(song)) == ()


def test_one_query_asks_for_every_id_and_each_artist_an_id_cannot_find() -> None:
    tape = a_tape(
        filed_song(),
        a_song(2, "Two", artist="Band, The,", album="R"),
        a_song(3, "Three", artist="Band, The,", album="R"),
        a_song(
            4,
            "Four",
            artist="Owned",
            filing=F.ALREADY,
            proposal=from_a_release("rel", 4, "Four"),
        ),
    )
    assert P.to_ask(tape) == (
        "mb_trackid:rec-One",
        f"artist:{F.lead('Band, The,').rstrip(',')}",
        "mb_trackid:rec-Four",
        "artist:Owned",
    )


def test_only_a_filed_song_has_its_recorded_path_follow_the_library() -> None:
    tape = a_tape(filed_song(), a_song(2, "Two", filing=F.FAILED))
    moved = P.with_paths(tape, [P.Entry(1, "/l/new.flac"), P.Entry(2, "/l/two.flac")])
    assert [s.path for s in moved.songs] == ["/l/new.flac", ""]


# ---------------------------------------------------------------------- the gate


def refusal(state: P.State, tape: TP.Tape | None = None, **kw: bool) -> str:
    return P.archive_refusal(
        tape or a_tape(a_song(1, "One", filing=F.FILED)),
        state,
        audio=kw.get("audio", True),
        playlist_there=kw.get("playlist_there", True),
    )


def test_the_gate_opens_when_the_playlist_is_written_and_nothing_is_unresolved() -> (
    None
):
    assert refusal(P.State(file="a.m3u8", songs=1)) == ""


def test_a_skipped_song_does_not_hold_the_gate() -> None:
    state = P.State("a.m3u8", 1, (P.Entry(1, state=TP.SKIPPED, why="skipped"),))
    assert refusal(state) == ""


def test_the_gate_names_every_reason_at_once() -> None:
    gone = (
        P.Entry(1, state=P.MISSING, why="not there"),
        P.Entry(7, state=P.AMBIGUOUS, why="two rows"),
    )
    said = refusal(P.State("a.m3u8", 1, gone), audio=False, playlist_there=False)
    assert "uncut file" in said and "a.m3u8 is gone" in said
    assert "song 1 (One): not there" in said and "song 7: two rows" in said


def test_a_song_whose_filing_did_not_finish_shuts_the_gate_though_it_has_a_line() -> (
    None
):
    tape = a_tape(
        a_song(1, "One", filing=F.FAILED, filing_note="beets refused it"),
        a_song(2, "Two"),
        a_song(3, "Three", filing=F.ALREADY),
        a_song(4, "Four", skip=True),
    )
    said = refusal(P.State("a.m3u8", 2), tape)
    assert "song 1 (One): did not file: beets refused it" in said
    assert "song 2 (Two): not filed yet" in said
    assert "Three" not in said and "Four" not in said


def test_a_song_left_out_and_unfinished_is_named_once() -> None:
    tape = a_tape(a_song(1, "One", filing=F.FAILED, filing_note="no"))
    gone = (P.Entry(1, state=P.NOT_FILED, why="did not file: no"),)
    assert refusal(P.State("a.m3u8", 0, gone), tape).count("song 1") == 1


def test_no_playlist_written_shuts_the_gate() -> None:
    assert "no playlist" in refusal(P.State())


def test_an_archived_tape_is_said_to_be_and_an_interrupted_clear_may_finish() -> None:
    state = P.State("a.m3u8", 1, archived={"file": "x"})
    assert "already archived" in refusal(state, audio=False)
    assert refusal(state, audio=True) == ""


def test_the_import_notes_say_what_changed_and_what_is_left_out() -> None:
    tape = a_tape(a_song(1, "One"))
    assert P.summary(tape, P.State("a", 1, outcome=P.UNCHANGED)) == []
    assert P.summary(tape, P.State("a", 2, outcome=P.WRITTEN)) == [
        "Mix: playlist rewritten, 2 songs"
    ]
    gone = (P.Entry(3, state=P.MISSING, why="not there"),)
    assert P.summary(tape, P.State("a", 2, gone, outcome=P.UNCHANGED)) == [
        "Mix: song 3 is not in its playlist: not there"
    ]
    told = P.State("a", 2, gone, outcome=P.UNKNOWN, note="beets could not answer")
    assert P.summary(tape, told) == ["Mix: beets could not answer"]


def test_a_state_comes_back_from_its_document_unchanged() -> None:
    state = P.State(
        "a", 1, (P.Entry(2, state=P.MISSING, why="gone"),), P.WRITTEN, "", 5.0
    )
    assert P.State.from_dict(state.to_dict()) == state
