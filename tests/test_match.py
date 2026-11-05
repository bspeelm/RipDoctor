"""Which release a song belongs to, and how doubt is stated. ADR-063."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ripdoctor.core import match as M
from ripdoctor.core import tape as TP


def wanted(title: str = "A Song", **kw: object) -> M.Wanted:
    fields: dict[str, object] = {
        "artist": "A Band",
        "album": "A Record",
        "length": 200.0,
    }
    return M.Wanted(title=title, **{**fields, **kw})  # type: ignore[arg-type]


def edition(
    release: str = "r1",
    *,
    title: str = "A Song",
    length: float | None = 200.0,
    group: str = "g1",
    album: str = "A Record",
    artist: str = "A Band",
    date: str = "1990",
    status: str = "Official",
    live: bool = False,
    secondary: tuple[str, ...] = (),
) -> M.Edition:
    track = M.CatalogueTrack(title, 3, 1, length, f"rec-{release}")
    return M.Edition(
        release,
        group,
        album,
        artist,
        date,
        status,
        (track,),
        live,
        primary="Album",
        secondary=secondary,
    )


def test_an_official_edition_with_the_title_and_length_is_accepted() -> None:
    got = M.propose(wanted(), [edition()])
    assert got.status == M.MATCHED
    assert got.choice is not None
    assert (got.choice.release, got.choice.group) == ("r1", "g1")
    assert (got.choice.release_title, got.choice.album_artist) == ("A Record", "A Band")
    assert got.choice.date == "1990"
    assert (got.choice.track_title, got.choice.number, got.choice.disc) == (
        "A Song",
        3,
        1,
    )
    assert (got.choice.length, got.choice.recording) == (200.0, "rec-r1")


@pytest.mark.parametrize(
    "title",
    [
        "A Song - Remastered",
        "A Song - 2011 Remaster",
        "A Song \u2013 Remastered 2009",
        "A Song (Remastered)",
        "A Song [2011 Digital Remaster]",
    ],
)
def test_a_remaster_suffix_on_the_list_does_not_stop_a_match(title: str) -> None:
    assert M.propose(wanted(title), [edition()]).status == M.MATCHED


def test_a_remaster_suffix_on_the_catalogue_does_not_stop_a_match() -> None:
    got = M.propose(wanted(), [edition(title="A Song (2011 Remaster)")])
    assert got.status == M.MATCHED


def test_only_the_last_dash_of_a_title_can_begin_a_remaster_suffix() -> None:
    got = M.propose(wanted("Part - Two - Remastered"), [edition(title="Part")])
    assert got.status == M.REVIEW


def test_of_several_editions_the_nearest_length_wins() -> None:
    got = M.propose(
        wanted(),
        [edition("early", length=202.5, date="1980"), edition("near", date="2001")],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "near"


def test_of_equally_near_editions_the_earliest_date_wins() -> None:
    got = M.propose(
        wanted(),
        [edition("a-late", date="2001"), edition("z-early", date="1985-03")],
    )
    assert got.choice is not None and got.choice.release == "z-early"


def test_less_than_a_second_is_not_nearer_than_a_whole_second_list_can_say() -> None:
    got = M.propose(
        wanted(),
        [
            edition("a-late", length=200.1),
            edition("z-early", length=200.3, date="1985"),
        ],
    )
    assert got.choice is not None and got.choice.release == "z-early"


def test_two_release_groups_that_fit_go_to_review_and_name_both() -> None:
    got = M.propose(
        wanted(),
        [edition("a", group="ga", date="1980"), edition("b", group="gb", date="1999")],
    )
    assert got.status == M.REVIEW
    assert "1980" in got.reason and "1999" in got.reason
    assert got.choice is not None and got.choice.release == "a"
    assert len(got.candidates) == 2


def test_the_album_the_list_names_settles_two_groups() -> None:
    got = M.propose(
        wanted(),
        [edition("a", group="ga", album="The Hits"), edition("b", group="gb")],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "b"


def test_editions_with_no_group_are_not_assumed_to_share_one() -> None:
    got = M.propose(wanted(), [edition("a", group=""), edition("b", group="")])
    assert got.status == M.REVIEW


def test_no_length_within_three_seconds_goes_to_review_and_says_how_far() -> None:
    got = M.propose(wanted(), [edition(length=240.0)])
    assert got.status == M.REVIEW
    assert "off by 40 s" in got.reason


def test_a_length_exactly_at_the_limit_is_within_it() -> None:
    assert M.propose(wanted(), [edition(length=203.0)]).status == M.MATCHED


def test_a_catalogue_track_with_no_length_cannot_be_accepted() -> None:
    got = M.propose(wanted(), [edition(length=None)])
    assert got.status == M.REVIEW
    assert "no length" in got.reason


def test_a_close_title_goes_to_review_with_the_title_it_found() -> None:
    got = M.propose(wanted(), [edition(title="A Song (Radio Edit)")])
    assert got.status == M.REVIEW
    assert "A Song (Radio Edit)" in got.reason


def test_no_official_edition_goes_to_review_and_says_so() -> None:
    got = M.propose(wanted(), [edition(status="Bootleg")])
    assert got.status == M.REVIEW
    assert "no Official edition" in got.reason and "Bootleg" in got.reason


def test_an_official_edition_is_preferred_to_a_nearer_promotion() -> None:
    got = M.propose(
        wanted(),
        [edition("promo", status="Promotion"), edition("real", length=202.0)],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "real"


def test_no_release_leaves_the_lists_album_standing() -> None:
    got = M.propose(wanted(), [])
    assert (got.status, got.choice, got.candidates) == (M.NONE, None, ())
    assert "A Record" in got.reason


def test_releases_with_no_such_track_are_a_doubt_not_no_release() -> None:
    got = M.propose(wanted(), [edition(title="Something Else Entirely")])
    assert got.status == M.REVIEW
    assert got.choice is None and len(got.candidates) == 1
    assert got.candidates[0].number == 0


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("Cafe\u0301 del Sol", "Caf\u00e9 del Sol"),
        ("\u591c\u306e\u3046\u305f", "\u591c\u306e\u3046\u305f"),
        ("Don\u2019t  Stop", "Don't Stop"),
        ("Back \u2013 Again", "Back - Again"),
    ],
)
def test_canonical_and_typographic_variants_are_the_same_title(
    listed: str, catalogue: str
) -> None:
    got = M.propose(wanted(listed), [edition(title=catalogue)])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("Ma\u00df", "Mass"),
        ("STRASSE", "Stra\u00dfe"),
        ("Song \u2461", "Song 2"),
        ("x\u00b2", "x2"),
        ("Song \u2161", "Song II"),
        ("\uff26\uff35\uff2c\uff2c Width", "FULL Width"),
        ("Brand\u2122", "BrandTM"),
        ("\ufb01re", "fire"),
    ],
)
def test_a_title_equal_only_by_compatibility_folding_shows_a_person_both_forms(
    listed: str, catalogue: str
) -> None:
    got = M.propose(wanted(listed), [edition(title=catalogue)])
    assert got.status == M.REVIEW
    assert repr(listed) in got.reason and repr(catalogue) in got.reason


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("Song - Remastered", "Song"),
        ("Song", "Song - Remastered 2011"),
        ("Song", "Song (2012 Remaster)"),
        ("Song - Digitally Remastered", "Song"),
    ],
)
def test_a_suffix_wholly_of_remaster_words_and_years_is_stripped(
    listed: str, catalogue: str
) -> None:
    got = M.propose(wanted(listed), [edition(title=catalogue)])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("Song - Demo / Remastered 2009", "Song"),
        ("Song", "Song - Mono / Remastered"),
        ("Song (Remastered Club Mix)", "Song"),
        ("Song", "Song (Instrumental Remaster)"),
        ("Song - Karaoke Remaster", "Song"),
        ("Song - 2011", "Song"),
        ("Song (Remastered", "Song"),
    ],
)
def test_a_remaster_word_beside_any_other_word_is_a_different_recording(
    listed: str, catalogue: str
) -> None:
    got = M.propose(wanted(listed), [edition(title=catalogue)])
    assert got.status == M.REVIEW
    assert "close title" in got.reason


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("Song (With Strings)", "Song"),
        ("Stay (With You)", "Stay"),
        ("A Feat of Clay", "A Feat of Strength"),
        ("Six Ft Under", "Six Ft Deep"),
        ("(feat. Bob)", "(with Al)"),
        ("Song (feat. Bob)", "Song (feat. Al)"),
        ("Song feat. A Guest", "Song"),
        ("Song - feat. Bob (Club Mix)", "Song"),
        ("Song (feat. Bob - Club Mix)", "Song"),
        ("(feat. Bob)", "(feat. Al)"),
    ],
)
def test_only_a_guest_clause_in_brackets_or_after_a_dash_is_a_credit(
    listed: str, catalogue: str
) -> None:
    assert M.propose(wanted(listed), [edition(title=catalogue)]).status == M.REVIEW


def test_the_same_guest_however_it_is_written_is_the_same_title() -> None:
    got = M.propose(wanted("Song (feat. Bob)"), [edition(title="Song [ft. Bob]")])
    assert got.status == M.MATCHED


def test_a_guest_on_one_side_only_is_still_the_same_title() -> None:
    got = M.propose(wanted("Song (feat. Bob)"), [edition(title="Song")])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("Song", "Song (Live)"),
        ("Song (Live)", "Song"),
        ("Song Pt. 1", "Song Pt. 2"),
        ("Song (Club Mix)", "Song"),
        ("Song", "Song (Karaoke Version)"),
        ("Caf\u00e9", "Cafe"),
        ("\u2020", "\u00f7"),
        ("Song!", "Song?"),
    ],
)
def test_a_title_that_differs_in_any_other_way_is_a_persons_to_judge(
    listed: str, catalogue: str
) -> None:
    assert M.propose(wanted(listed), [edition(title=catalogue)]).status == M.REVIEW


def test_a_title_with_only_punctuation_is_matched_exactly_and_not_otherwise() -> None:
    assert M.propose(wanted("!!!"), [edition(title="!!!")]).status == M.MATCHED
    assert M.propose(wanted("!!!"), [edition(title="???")]).status == M.REVIEW


@pytest.mark.parametrize(
    "title",
    ["A Song (feat. A Guest)", "A Song [ft. A Guest]", "A Song - featuring A Guest"],
)
def test_a_feat_credit_in_the_title_is_not_part_of_the_title(title: str) -> None:
    assert M.propose(wanted(title), [edition()]).status == M.MATCHED


def test_a_feat_credit_in_the_artist_still_agrees_with_the_album_artist() -> None:
    got = M.propose(wanted(artist="A Band feat. A Guest"), [edition()])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "credited"),
    [
        ("A Band, Another Band", "A Band"),
        ("Another Band, A Band", "A Band & Another Band"),
        ("Earth, Water & Fire", "Earth, Water & Fire"),
    ],
)
def test_comma_separated_artists_agree_with_any_of_the_credit(
    listed: str, credited: str
) -> None:
    got = M.propose(wanted(artist=listed), [edition(artist=credited)])
    assert got.status == M.MATCHED


def test_an_artist_nobody_listed_goes_to_review_and_names_them() -> None:
    got = M.propose(wanted(), [edition(artist="Somebody Else")])
    assert got.status == M.REVIEW
    assert "Somebody Else" in got.reason


def test_a_compilation_does_not_have_to_credit_the_lists_artist() -> None:
    got = M.propose(wanted(), [edition(artist="Various Artists")])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "release"),
    [
        ("A Record", "A Record (Instrumentals)"),
        ("A Record", "A Record: The Karaoke Edition"),
        ("A Record", "A Record (Karaoke Version)"),
        ("A Record", "A Record: A Tribute"),
        ("A Record", "A Record (Live Edition)"),
        ("A Record", "A Record - Remixed"),
        ("A Record", "A Record (Original Version)"),
        ("A Record", "A Record (1999)"),
        ("Hits", "Hits Vol. 2"),
        ("Live 1970", "Live 1985"),
        ("A Record (Deluxe)", "B Record (Deluxe)"),
        ("Ma\u00df", "Mass"),
        ("", "A Record"),
        ("(Deluxe Edition)", "(Expanded Edition)"),
    ],
)
def test_a_release_that_is_not_the_lists_album_is_never_accepted(
    listed: str, release: str
) -> None:
    got = M.propose(wanted(album=listed), [edition(album=release)])
    assert got.status == M.REVIEW
    assert release in got.reason
    assert got.choice is not None and got.choice.release_title == release


@pytest.mark.parametrize(
    ("listed", "release"),
    [
        ("A Record", "A RECORD"),
        ("A Record", "A Record (Deluxe Edition)"),
        ("A Record", "A Record [Expanded]"),
        ("A Record", "A Record - 2011 Remastered"),
        ("A Record", "A Record: 25th Anniversary Edition"),
        ("A Record", "A Record (Bonus Tracks)"),
        ("A Record", "A Record (Special Edition)"),
        ("A Record", "A Record (Remastered) [Deluxe Edition]"),
        ("A Record (Deluxe Edition)", "A Record"),
        ("A Record (Deluxe Edition)", "A Record (Expanded Edition)"),
    ],
)
def test_an_album_may_differ_by_a_suffix_wholly_of_edition_words(
    listed: str, release: str
) -> None:
    got = M.propose(wanted(album=listed), [edition(album=release)])
    assert got.status == M.MATCHED


def test_an_exact_album_off_in_length_does_not_lose_to_a_karaoke_fit() -> None:
    got = M.propose(
        wanted(),
        [
            edition("real", length=215.0, group="a"),
            edition("sing", album="A Record (Karaoke Version)", group="k"),
        ],
    )
    assert got.status == M.REVIEW
    assert got.choice is not None and got.choice.release == "real"
    assert "off by 15 s" in got.reason


def test_the_exact_album_is_preferred_to_a_deluxe_edition_that_fits_as_well() -> None:
    got = M.propose(
        wanted(),
        [
            edition("big", album="A Record (Deluxe Edition)", group="d", date="1980"),
            edition("plain", date="1999"),
        ],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "plain"


def test_a_deluxe_edition_can_answer_when_the_exact_album_is_off_in_length() -> None:
    got = M.propose(
        wanted(),
        [
            edition("plain", length=215.0),
            edition("big", album="A Record (Deluxe Edition)", group="d"),
        ],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "big"


@pytest.mark.parametrize(
    "kind",
    [
        "Compilation",
        "Remix",
        "Soundtrack",
        "Karaoke",
        "Mixtape/Street",
        "DJ-mix",
        "Demo",
    ],
)
def test_a_release_of_another_kind_is_a_persons_to_judge(kind: str) -> None:
    named = "A Record (Deluxe Edition)"
    got = M.propose(wanted(), [edition(album=named, secondary=(kind,))])
    assert got.status == M.REVIEW
    assert kind in got.reason


@pytest.mark.parametrize(
    "kind", ["Compilation", "Remix", "Soundtrack", "Karaoke", "Mixtape/Street", "Demo"]
)
def test_a_release_of_another_kind_may_match_when_the_list_names_exactly_it(
    kind: str,
) -> None:
    got = M.propose(wanted(), [edition(secondary=(kind,))])
    assert got.status == M.MATCHED


def test_a_live_group_needs_the_lists_album_to_say_live_and_to_be_exact() -> None:
    held = edition(album="Live at a Hall", secondary=("Live",), live=True)
    assert M.propose(wanted(album="Live at a Hall"), [held]).status == M.MATCHED
    assert M.propose(wanted(album="Live at a Barn"), [held]).status == M.REVIEW
    plain = edition(secondary=("Live",), live=True)
    assert M.propose(wanted(), [plain]).status == M.REVIEW


def test_a_remaster_stripped_match_onto_a_live_album_is_not_accepted() -> None:
    got = M.propose(wanted("A Song - Remastered"), [edition(live=True)])
    assert got.status == M.REVIEW
    assert "live album" in got.reason


def test_a_release_titled_live_counts_as_a_live_album() -> None:
    got = M.propose(wanted(), [edition(album="Live at a Hall")])
    assert got.status == M.REVIEW


def test_a_list_that_asks_for_the_live_album_can_have_it() -> None:
    got = M.propose(
        wanted(album="Live at a Hall"), [edition(album="Live at a Hall", live=True)]
    )
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "catalogue"),
    [
        ("A Song - Live", "A Song"),
        ("A Song", "A Song (Live)"),
        ("A Song - Remastered Live 1977", "A Song"),
    ],
)
def test_live_on_only_one_side_is_not_an_exact_title(
    listed: str, catalogue: str
) -> None:
    got = M.propose(wanted(listed), [edition(title=catalogue)])
    assert got.status == M.REVIEW


def test_a_title_with_live_in_it_on_both_sides_is_exact() -> None:
    got = M.propose(wanted("Live Again"), [edition(title="Live Again")])
    assert got.status == M.MATCHED


def test_the_track_nearest_in_length_is_taken_when_a_release_repeats_a_title() -> None:
    tracks = (
        M.CatalogueTrack("A Song", 1, 1, 100.0),
        M.CatalogueTrack("A Song", 7, 2, 200.0),
    )
    got = M.propose(
        wanted(),
        [M.Edition("r", "g", "A Record", "A Band", "1990", "Official", tracks)],
    )
    assert got.choice is not None
    assert (got.choice.number, got.choice.disc) == (7, 2)


def test_what_to_ask_the_catalogue_for_drops_guests_then_tries_the_lead() -> None:
    assert M.search_artists("A Band") == ("A Band",)
    assert M.search_artists("A Band feat. A Guest") == ("A Band",)
    assert M.search_artists("A Band, Another Band") == (
        "A Band, Another Band",
        "A Band",
    )
    assert M.search_album("A Record (Remastered 2011)") == "A Record"


def test_songs_from_one_album_share_a_lookup_however_they_are_credited() -> None:
    one = M.lookup_key(wanted(artist="A BAND", album="A Record"))
    assert one == M.lookup_key(wanted(artist="A Band feat. Guest", album="a record"))
    assert one == M.lookup_key(
        wanted(artist="A Band, Guest", album="A Record - 2011 Remaster")
    )
    assert one != M.lookup_key(wanted(album="Another Record"))


def test_a_release_id_is_found_in_a_pasted_link() -> None:
    uuid = "12345678-90ab-cdef-1234-567890abcdef"
    assert M.release_id(f" https://catalogue.example/release/{uuid}#x ") == uuid
    assert M.release_id(uuid.upper()) == uuid
    with pytest.raises(ValueError, match="no release id"):
        M.release_id("../etc")


def test_a_persons_release_finds_the_track_like_the_title() -> None:
    assert M.track_on(wanted("A Song (Live)"), edition()).number == 3


def test_a_persons_release_with_no_such_track_lists_what_it_has() -> None:
    with pytest.raises(M.NoTrack, match="1 Another"):
        M.track_on(wanted(), edition(title="Another"))


def test_a_person_can_name_the_track_whatever_it_is_called() -> None:
    choice = M.track_on(wanted(), edition(title="Another"), 1)
    assert (choice.track_title, choice.number) == ("Another", 3)
    with pytest.raises(M.NoTrack, match="no track 2"):
        M.track_on(wanted(), edition(title="Another"), 2)


# ------------------------------------------------------------ the document


def a_tape() -> TP.Tape:
    songs = (
        TP.Song(1, "A Song", "A Band", "A Record", 200.0),
        TP.Song(2, "B Song", "A Band", "A Record", 180.0),
    )
    return replace(TP.named("mix", "Mix", 1.0), songs=songs)


def matched() -> M.Proposal:
    return M.propose(wanted(), [edition()])


def test_a_proposal_survives_the_document_through_json() -> None:
    tape = TP.with_proposals(
        a_tape(),
        {1: matched(), 2: M.propose(wanted("B Song"), [edition(length=None)])},
    )
    tape = TP.settle(tape, 2, None)
    text = json.dumps(tape.to_dict())
    assert TP.Tape.from_dict(json.loads(text)) == tape


def test_a_song_saved_before_it_was_ever_matched_still_reads() -> None:
    data = a_tape().songs[0].to_dict()
    del data["proposal"], data["settled_by"]
    assert TP.Song.from_dict(data) == a_tape().songs[0]


def test_only_an_accepted_match_is_settled_by_the_catalogue() -> None:
    tape = TP.with_proposals(
        a_tape(), {1: matched(), 2: M.Proposal(M.REVIEW, "why", None)}
    )
    assert [s.settled_by for s in tape.songs] == [M.CATALOGUE, ""]
    assert not TP.all_settled(tape)


def test_no_release_is_not_settled_until_a_person_says_so() -> None:
    tape = TP.with_proposals(a_tape(), {1: matched(), 2: M.propose(wanted(), [])})
    assert not TP.all_settled(tape)
    assert TP.all_settled(TP.settle(tape, 2, None))


def test_a_tape_with_no_songs_is_not_settled() -> None:
    assert not TP.all_settled(TP.named("mix", "Mix", 1.0))


def test_filing_from_the_list_keeps_the_candidates_to_change_ones_mind() -> None:
    tape = TP.settle(TP.with_proposals(a_tape(), {1: matched()}), 1, None)
    song = tape.songs[0]
    assert song.settled_by == M.PERSON
    assert song.proposal is not None
    assert song.proposal.choice is None and len(song.proposal.candidates) == 1


def test_a_person_can_settle_a_song_before_any_run() -> None:
    choice = M.track_on(wanted(), edition())
    song = TP.settle(a_tape(), 1, choice).songs[0]
    assert song.proposal is not None
    assert (song.proposal.status, song.settled_by) == (M.MATCHED, M.PERSON)


def test_a_persons_choice_is_not_overwritten_by_the_catalogue() -> None:
    choice = M.track_on(wanted(), edition("mine"))
    tape = TP.settle(a_tape(), 1, choice)
    again = TP.with_proposals(tape, {1: M.propose(wanted(), [edition("theirs")])})
    assert again.songs[0] == tape.songs[0]


def test_a_run_leaves_settled_songs_and_stated_doubts_alone() -> None:
    tape = TP.with_proposals(a_tape(), {1: matched(), 2: M.Proposal(M.REVIEW, "why")})
    assert TP.to_match(tape) == ()
    assert [s.number for s in TP.to_match(tape, again=True)] == [2]


def test_a_failed_lookup_is_picked_up_by_the_next_run() -> None:
    tape = TP.with_proposals(a_tape(), {1: matched(), 2: M.Proposal(M.FAILED, "why")})
    assert [s.number for s in TP.to_match(tape)] == [2]


def test_a_new_list_keeps_the_matches_of_songs_it_leaves_alone() -> None:
    tape = TP.with_proposals(a_tape(), {1: matched()})
    first, second = tape.songs
    moved = (
        TP.Song(1, second.title, second.artist, second.album, second.length),
        TP.Song(2, first.title, first.artist, first.album, first.length),
    )
    got = TP.relist(tape, moved).songs
    assert (got[0].proposal, got[1].proposal) == (None, first.proposal)
    assert got[1].settled_by == M.CATALOGUE
    assert got[1].number == 2


def test_a_new_list_forgets_the_match_of_a_song_it_changes() -> None:
    tape = TP.with_proposals(a_tape(), {1: matched()})
    changed = (TP.Song(1, "Other", "A Band", "A Record", 200.0), tape.songs[1])
    assert TP.relist(tape, changed).songs[0].proposal is None


def test_a_new_list_may_not_lose_a_persons_choice() -> None:
    tape = TP.settle(a_tape(), 1, None)
    with pytest.raises(TP.ChoiceSet, match="A Song"):
        TP.relist(tape, (tape.songs[1],))


def test_placing_boundaries_keeps_what_was_settled() -> None:
    tape = TP.settle(a_tape(), 1, None)
    placed = TP.place(tape, TP.Music(0.0, 380.0, ()))
    assert placed.songs[0].settled_by == M.PERSON


def test_a_boundary_doubt_and_a_release_doubt_are_kept_apart() -> None:
    doubt = M.Proposal(M.REVIEW, "which album")
    tape = TP.with_proposals(a_tape(), {1: doubt})
    tape = replace(
        tape, songs=(replace(tape.songs[0], reason="which edge"), tape.songs[1])
    )
    read = TP.Tape.from_dict(json.loads(json.dumps(tape.to_dict())))
    assert read.songs[0].reason == "which edge"
    assert read.songs[0].proposal is not None
    assert read.songs[0].proposal.reason == "which album"

    settled = TP.settle(read, 1, None).songs[0]
    assert settled.reason == "which edge"
    assert settled.proposal is not None and settled.proposal.reason == ""
