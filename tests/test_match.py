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
    primary: str = "Album",
    credit: tuple[str, ...] = (),
    recording_title: str = "",
    disambiguation: str = "",
    recording: str = "",
) -> M.Edition:
    track = M.CatalogueTrack(
        title,
        3,
        1,
        length,
        recording or f"rec-{release}",
        recording_title,
        disambiguation,
        credit,
    )
    return M.Edition(
        release,
        group,
        album,
        artist,
        date,
        status,
        (track,),
        live,
        primary=primary,
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


def test_of_several_editions_within_the_limit_the_earliest_wins() -> None:
    got = M.propose(
        wanted(),
        [edition("near", date="2001"), edition("early", length=202.5, date="1980")],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "early"


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


def test_two_release_groups_that_fit_settle_on_the_earlier_and_offer_both() -> None:
    got = M.propose(
        wanted(),
        [edition("b", group="gb", date="1999"), edition("a", group="ga", date="1980")],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "a"
    assert [c.release for c in got.candidates] == ["a", "b"]


def test_the_album_the_list_names_settles_two_groups() -> None:
    got = M.propose(
        wanted(),
        [edition("a", group="ga", album="The Hits"), edition("b", group="gb")],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "b"


def test_editions_with_no_group_are_not_assumed_to_share_one() -> None:
    got = M.propose(wanted(), [edition("a", group=""), edition("b", group="")])
    assert [c.release for c in got.candidates] == ["a", "b"]


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
    assert got.candidates[0].number == 3


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
        ("Song (Feat of Clay)", "Song"),
        ("Song (Ft Bob)", "Song"),
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
        ("Sun, Sea & Stars", "Sun, Sea & Stars"),
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


@pytest.mark.parametrize("listed", ["A Band", "Various Artists", "various artists"])
def test_various_artists_never_agrees_with_anyone(listed: str) -> None:
    got = M.propose(wanted(artist=listed), [edition(artist="Various Artists")])
    assert got.status == M.REVIEW
    assert "Various Artists" in got.reason


@pytest.mark.parametrize(
    ("listed", "credited"),
    [
        ("Sun, Moon & Stars", "Sun"),
        ("Mr. X", "Planet X"),
        ("X", "Another X"),
        ("Bob and the Hats", "The Hats"),
        ("A Band", "A Band & Another Band"),
        ("Another Band, A Band", "A Band"),
        ("A Band", ""),
        ("", "A Band"),
        ("", ""),
        (",", ";"),
        ("AC/DC", "AC"),
        ("Foo", "Foo Travellers"),
        ("Ma\u00df", "Mass"),
    ],
)
def test_a_shared_word_or_an_empty_name_is_no_agreement(
    listed: str, credited: str
) -> None:
    got = M.propose(wanted(artist=listed), [edition(artist=credited)])
    assert got.status == M.REVIEW
    assert "credits" in got.reason


@pytest.mark.parametrize(
    ("listed", "credited"),
    [
        ("A BAND", "a band"),
        ("Hill and Dale", "Hill & Dale"),
        ("A Band (feat. A Guest)", "A Band"),
        ("AC/DC", "AC/DC"),
    ],
)
def test_the_same_names_or_a_listed_lead_agree(listed: str, credited: str) -> None:
    got = M.propose(wanted(artist=listed), [edition(artist=credited)])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "credit"),
    [
        ("A Band", ("A Band", "Another Band")),
        ("Another Band, A Band", ("A Band", "Another Band")),
        ("A Band feat. A Guest", ("A Band", "A Guest")),
        ("A Band, A Guest", ("A Band", "A Guest")),
        ("A Band & Another Band", ("A Band", "Another Band")),
        ("The Hats", ("Hats",)),
        ("Hats", ("The Hats",)),
        ("the hats", ("The Hats",)),
    ],
)
def test_the_lead_on_a_credit_of_several_agrees_and_the_is_ignored(
    listed: str, credit: tuple[str, ...]
) -> None:
    got = M.propose(wanted(artist=listed), [edition(credit=credit)])
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    ("listed", "credit"),
    [
        ("A Guest, A Band", ("A Band",)),
        ("A Band & Another Band", ("A Band",)),
        ("Sun", ("Sun, Moon & Stars",)),
        ("The Hats", ("The Hat Band",)),
    ],
)
def test_a_credit_without_the_lead_is_held(
    listed: str, credit: tuple[str, ...]
) -> None:
    got = M.propose(wanted(artist=listed), [edition(credit=credit)])
    assert got.status == M.REVIEW
    assert "credits" in got.reason


def test_a_band_with_ft_in_its_name_is_not_cut_at_it() -> None:
    assert M.search_artists("Six Ft Under") == ("Six Ft Under",)
    got = M.propose(wanted(artist="Six Ft Under"), [edition(artist="Six")])
    assert got.status == M.REVIEW


@pytest.mark.parametrize(
    ("listed", "release"),
    [
        ("A Record", "A Record (Instrumentals)"),
        ("A Record", "A Record: The Karaoke Edition"),
        ("A Record", "A Record (Karaoke Version)"),
        ("A Record", "A Record: A Tribute"),
        ("A Record", "A Record (Live Edition)"),
        ("A Record", "A Record - Remixed"),
        ("A Record", "A Record (Special Karaoke Edition)"),
        ("A Record", "A Record (Deluxe Instrumental Edition)"),
        ("Live 1970", "Live 1985"),
    ],
)
def test_a_release_that_reads_as_another_version_is_never_accepted(
    listed: str, release: str
) -> None:
    got = M.propose(wanted(album=listed), [edition(album=release)])
    assert got.status == M.REVIEW
    assert release in got.reason
    assert got.choice is not None and got.choice.release_title == release


@pytest.mark.parametrize(
    ("listed", "release"),
    [
        ("A Record", "A Record (Original Version)"),
        ("A Record", "A Record (1999)"),
        ("Hits", "Hits Vol. 2"),
        ("A Record (Deluxe)", "B Record (Deluxe)"),
        ("Ma\u00df", "Mass"),
        ("(Deluxe Edition)", "(Expanded Edition)"),
    ],
)
def test_a_different_album_from_the_list_is_a_note_and_not_a_block(
    listed: str, release: str
) -> None:
    got = M.propose(wanted(album=listed), [edition(album=release)])
    assert got.status == M.MATCHED
    assert listed in got.note and release in got.note
    assert got.reason == ""


def test_no_album_on_the_list_is_no_note() -> None:
    got = M.propose(wanted(album=""), [edition()])
    assert (got.status, got.note) == (M.MATCHED, "")


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
    ["Compilation", "Soundtrack", "Karaoke", "DJ-mix", "Demo"],
)
def test_a_release_of_another_kind_is_a_persons_to_judge(kind: str) -> None:
    named = "A Record (Deluxe Edition)"
    got = M.propose(wanted(), [edition(album=named, secondary=(kind,))])
    assert got.status == M.REVIEW
    assert kind in got.reason


@pytest.mark.parametrize(
    ("kind", "song"), [("Remix", "A Song (Club Mix)"), ("Mixtape/Street", "A Song")]
)
def test_a_remix_or_mixtape_release_ranks_as_the_record_it_is(
    kind: str, song: str
) -> None:
    ep = edition("ep", title=song, album="B Record", group="b", primary="EP")
    lp = edition(
        "lp", title=song, album="C Record", group="c", secondary=(kind,), date="1999"
    )
    got = M.propose(wanted(song), [replace(ep, date="1980"), lp])
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "lp"


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


def on_one_release(*tracks: tuple[str, float | None]) -> M.Edition:
    made = tuple(
        M.CatalogueTrack(title, at, 1, length)
        for at, (title, length) in enumerate(tracks, 1)
    )
    return M.Edition("r", "g", "A Record", "A Band", "1990", "Official", made)


@pytest.mark.parametrize(("first", "second"), [(198.0, 201.0), (198.0, 202.0)])
def test_two_tracks_a_list_cannot_tell_apart_go_to_review(
    first: float, second: float
) -> None:
    got = M.propose(
        wanted("Intro"), [on_one_release(("Intro", first), ("Intro", second))]
    )
    assert got.status == M.REVIEW
    assert "2 tracks" in got.reason


def test_a_repeated_title_whose_lengths_separate_it_is_not_a_doubt() -> None:
    got = M.propose(
        wanted("Intro"), [on_one_release(("Intro", 215.0), ("Intro", 200.0))]
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.number == 2


def test_a_near_title_with_the_length_does_not_count_as_a_repeat() -> None:
    got = M.propose(wanted(), [on_one_release(("A Song", 200.0), ("A Song 2", 200.0))])
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.number == 1


def test_what_to_ask_the_catalogue_for_drops_guests_then_tries_the_lead() -> None:
    assert M.search_artists("A Band") == ("A Band",)
    assert M.search_artists("A Band feat. A Guest") == ("A Band",)
    assert M.search_artists("A Band, Another Band") == (
        "A Band, Another Band",
        "A Band",
    )
    assert M.search_album("A Record (Remastered 2011)") == "A Record"


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


# ------------------------------------------------------- what is accepted unasked


def test_a_recording_called_something_else_on_a_track_of_the_listed_name_is_held() -> (
    None
):
    got = M.propose(
        wanted("Harbour"),
        [edition(title="Harbour", recording_title="Harbour (with brass band)")],
    )
    assert got.status == M.REVIEW
    assert "close title" in got.reason


def test_a_track_called_something_else_for_a_recording_of_the_listed_name_is_held() -> (
    None
):
    got = M.propose(
        wanted("Harbour"),
        [edition(title="Harbour (brass band mix)", recording_title="Harbour")],
    )
    assert got.status == M.REVIEW


def test_a_remaster_suffix_on_either_name_of_the_track_does_not_hold_it() -> None:
    got = M.propose(
        wanted("Harbour"),
        [edition(title="Harbour - 2011 Remaster", recording_title="Harbour")],
    )
    assert got.status == M.MATCHED


@pytest.mark.parametrize(
    "said",
    [
        "live, 1999-05-01: a town hall",
        "acoustic",
        "instrumental",
        "demo",
        "extended remix",
        "radio edit",
        "karaoke",
        "studio sessions",
        "alternate take",
        "clean",
        "Dolby Atmos mix",
        "part of a DJ-mix",
        "music video",
    ],
)
def test_a_recording_the_catalogue_calls_another_version_is_held(said: str) -> None:
    got = M.propose(wanted(), [edition(disambiguation=said)])
    assert got.status == M.REVIEW
    assert said in got.reason


def test_the_lists_own_title_can_ask_for_that_version() -> None:
    got = M.propose(
        wanted("A Song (Acoustic)"),
        [edition(title="A Song (Acoustic)", disambiguation="acoustic")],
    )
    assert got.status == M.MATCHED


def test_a_disambiguation_that_says_no_other_version_does_not_hold_it() -> None:
    got = M.propose(wanted(), [edition(disambiguation="album version")])
    assert got.status == M.MATCHED


@pytest.mark.parametrize("kind", ["Compilation", "DJ-mix", "Live"])
@pytest.mark.parametrize("listed", ["", "The Wrong Album"])
def test_a_wrong_or_blank_album_never_lets_a_kept_out_release_in(
    kind: str, listed: str
) -> None:
    got = M.propose(wanted(album=listed), [edition(album="Best Of", secondary=(kind,))])
    assert got.status == M.REVIEW


def test_a_compilation_the_list_names_is_accepted() -> None:
    got = M.propose(
        wanted(album="Best Of"), [edition(album="Best Of", secondary=("Compilation",))]
    )
    assert got.status == M.MATCHED


def test_a_compilation_does_not_stop_an_album_being_accepted() -> None:
    got = M.propose(
        wanted(album=""),
        [
            edition("comp", album="Best Of", secondary=("Compilation",), date="1980"),
            edition("lp", album="Second", group="g2", date="1999"),
        ],
    )
    assert got.status == M.MATCHED
    assert got.choice is not None and got.choice.release == "lp"
    assert [c.release for c in got.candidates] == ["lp", "comp"]


def test_a_tie_comes_out_the_same_whatever_order_it_was_given_in() -> None:
    one = edition("a", group="ga", date="1999")
    two = edition("b", group="gb", date="1999")
    first = M.propose(wanted(album=""), [one, two])
    second = M.propose(wanted(album=""), [two, one])
    assert first.status == M.MATCHED
    assert first == second
    assert first.choice is not None and first.choice.release == "a"


def test_an_edition_in_the_library_comes_before_the_album_on_the_list() -> None:
    mine = edition("mine", album="Another Record", group="g2", date="2005")
    got = M.propose(
        wanted(),
        [edition("named"), mine],
        held=(M.Held("mine", "Another Record", "A Band", 13),),
    )
    assert got.choice is not None and got.choice.release == "mine"


def test_the_album_on_the_list_comes_before_an_earlier_album() -> None:
    got = M.propose(
        wanted(),
        [edition("early", album="First", group="g2", date="1970"), edition("named")],
    )
    assert got.choice is not None and got.choice.release == "named"


def test_an_album_comes_before_an_ep_and_an_ep_before_a_single() -> None:
    single = edition("s", album="S", group="gs", primary="Single", date="1970")
    ep = edition("e", album="E", group="ge", primary="EP", date="1980")
    lp = edition("a", album="A", group="ga", date="1990")
    got = M.propose(wanted(album=""), [single, ep, lp])
    assert got.choice is not None and got.choice.release == "a"
    assert [c.release for c in got.candidates] == ["a", "e", "s"]


def test_an_edition_with_no_date_comes_last() -> None:
    got = M.propose(
        wanted(album=""),
        [
            edition("nodate", album="X", group="gx", date=""),
            edition("dated", album="Y", group="gy", date="2020"),
        ],
    )
    assert got.choice is not None and got.choice.release == "dated"


def test_a_search_on_a_shorter_title_is_only_ever_a_set_of_candidates() -> None:
    got = M.propose(wanted(), [edition()], loose=True)
    assert got.status == M.REVIEW
    assert got.candidates


def test_options_are_capped_and_keep_the_chosen_recordings_single() -> None:
    albums = [
        edition(f"a{n}", album=f"A{n}", group=f"g{n}", date=f"19{n:02d}", recording="r")
        for n in range(20)
    ]
    single = edition(
        "s", album="S", group="gs", primary="Single", date="1950", recording="r"
    )
    got = M.propose(wanted(album=""), [*albums, single])
    assert len(got.candidates) == M.OFFERED + 1
    assert got.candidates[-1].release == "s"


def test_the_same_recording_on_an_album_is_offered_once_the_librarys_first() -> None:
    one = edition("one", date="1990", recording="r")
    two = edition("two", date="1991", recording="r")
    assert len(M.propose(wanted(), [one, two]).candidates) == 1
    held = (M.Held("two", "A Record", "A Band", 13),)
    offered = M.propose(wanted(), [one, two], held=held).candidates
    assert offered[0].release == "two"


def test_an_option_says_all_a_person_chooses_by() -> None:
    held = (M.Held("r1", "A Record", "A Band", 13),)
    made = M.propose(
        wanted(),
        [
            M.Edition(
                "r1",
                "g1",
                "A Record",
                "A Band",
                "1990-02-03",
                "Official",
                (M.CatalogueTrack("A Song", 3, 2, 202.5, "rec", tracks=13),),
                primary="EP",
                country="GB",
                format="CD",
            )
        ],
        held=held,
    )
    option = M.options(wanted(), made)[0]
    assert (option["release"], option["group"]) == ("r1", "g1")
    assert (option["release_title"], option["year"]) == ("A Record", "1990")
    assert (option["country"], option["format"]) == ("GB", "CD")
    assert (option["track_title"], option["disc"], option["number"]) == ("A Song", 2, 3)
    assert (option["tracks"], option["length"], option["delta"]) == (13, 202.5, 2.5)
    assert (option["kind"], option["in_library"]) == ("ep", True)


def test_a_track_by_another_name_is_offered_with_its_place_and_not_proposed() -> None:
    """An option with no track could not be chosen with no typing."""
    got = M.propose(wanted(), [edition(title="Something Else Entirely")])
    assert got.status == M.REVIEW
    assert got.choice is None
    assert (got.candidates[0].track_title, got.candidates[0].number) == (
        "Something Else Entirely",
        3,
    )


def test_a_title_loses_only_a_trailing_clause_when_shortened() -> None:
    assert M.base_title("A Song (Single Edit)") == "A Song"
    assert M.base_title("A Song [Extended]") == "A Song"
    assert M.base_title("A Song - Album Version") == "A Song"
    assert M.base_title("A Song (Club Remix [Radio Edit])") == "A Song"
    assert M.base_title("A Song") == "A Song"
    assert M.base_title("(Interlude)") == ""


def test_a_document_from_before_these_fields_still_reads() -> None:
    old = {"status": "review", "reason": "r", "choice": None, "candidates": []}
    made = M.Proposal.from_dict(old)
    assert (made.note, made.held, made.copy, made.library_note) == ("", (), None, "")


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


def identical_rows(count: int = 2) -> TP.Tape:
    songs = tuple(
        TP.Song(n, "A Song", "A Band", "A Record", 200.0) for n in range(1, count + 1)
    )
    return replace(TP.named("mix", "Mix", 1.0), songs=songs)


def fresh(tape: TP.Tape) -> tuple[TP.Song, ...]:
    return tuple(
        TP.Song(s.number, s.title, s.artist, s.album, s.length) for s in tape.songs
    )


def test_resaving_a_list_keeps_a_choice_on_the_first_of_two_identical_rows() -> None:
    tape = TP.settle(identical_rows(), 1, None)
    assert [s.settled_by for s in TP.relist(tape, fresh(tape)).songs] == [M.PERSON, ""]


def test_a_choice_on_the_second_identical_row_is_not_copied_to_the_first() -> None:
    tape = TP.settle(identical_rows(), 2, None)
    assert [s.settled_by for s in TP.relist(tape, fresh(tape)).songs] == ["", M.PERSON]


def test_each_of_two_identical_rows_keeps_its_own_choice_through_a_resave() -> None:
    mine = M.track_on(wanted(), edition("mine"))
    tape = TP.settle(TP.settle(identical_rows(), 1, mine), 2, None)
    got = TP.relist(tape, fresh(tape)).songs
    assert got[0].proposal is not None and got[0].proposal.choice == mine
    assert got[1].proposal is not None and got[1].proposal.choice is None


def test_identical_rows_keep_their_order_when_a_song_is_put_before_them() -> None:
    mine = M.track_on(wanted(), edition("mine"))
    tape = TP.settle(TP.settle(identical_rows(), 1, mine), 2, None)
    rows = (
        TP.Song(1, "New", "A Band", "A Record", 90.0),
        TP.Song(2, "A Song", "A Band", "A Record", 200.0),
        TP.Song(3, "A Song", "A Band", "A Record", 200.0),
    )
    got = TP.relist(tape, rows).songs
    assert got[0].settled_by == ""
    assert got[1].proposal is not None and got[1].proposal.choice == mine
    assert got[2].proposal is not None and got[2].proposal.choice is None


def test_dropping_the_unchosen_of_two_identical_rows_keeps_the_choice() -> None:
    tape = TP.settle(identical_rows(), 2, None)
    only = (TP.Song(1, "A Song", "A Band", "A Record", 200.0),)
    assert [s.settled_by for s in TP.relist(tape, only).songs] == [M.PERSON]


def test_dropping_one_of_two_chosen_identical_rows_is_refused() -> None:
    tape = TP.settle(TP.settle(identical_rows(), 1, None), 2, None)
    with pytest.raises(TP.ChoiceSet, match="A Song"):
        TP.relist(tape, (TP.Song(1, "A Song", "A Band", "A Record", 200.0),))


def test_a_person_s_choice_and_a_skip_on_identical_rows_each_stay_on_their_row() -> (
    None
):
    mine = M.track_on(wanted(), edition("mine"))
    tape = TP.skip(TP.settle(identical_rows(3), 1, mine), 2)
    got = TP.relist(tape, fresh(tape)).songs
    assert [s.settled_by for s in got] == [M.PERSON, "", ""]
    assert [s.skip for s in got] == [False, True, False]
    assert got[0].proposal is not None and got[0].proposal.choice == mine


def test_a_row_edited_above_identical_ones_leaves_each_of_them_its_own() -> None:
    tape = TP.skip(TP.settle(identical_rows(3), 2, None), 3)
    rows = (TP.Song(1, "Changed", "A Band", "A Record", 90.0), *fresh(tape)[1:])
    got = TP.relist(tape, rows).songs
    assert [s.settled_by for s in got] == ["", M.PERSON, ""]
    assert [s.skip for s in got] == [False, False, True]


def test_a_row_edited_between_identical_ones_leaves_each_of_them_its_own() -> None:
    tape = TP.skip(TP.settle(identical_rows(3), 1, None), 3)
    rows = (
        fresh(tape)[0],
        TP.Song(2, "Changed", "A Band", "A Record", 90.0),
        fresh(tape)[2],
    )
    got = TP.relist(tape, rows).songs
    assert [s.settled_by for s in got] == [M.PERSON, "", ""]
    assert [s.skip for s in got] == [False, False, True]


def test_a_row_changed_to_match_its_neighbours_leaves_them_their_places() -> None:
    tape = TP.settle(identical_rows(3), 3, None)
    between = TP.Song(2, "Different", "A Band", "A Record", 90.0)
    tape = replace(tape, songs=(tape.songs[0], between, tape.songs[2]))
    rows = fresh(TP.settle(identical_rows(3), 3, None))
    got = TP.relist(tape, rows).songs
    assert [s.settled_by for s in got] == ["", "", M.PERSON]


def test_a_choice_outranks_a_row_that_only_kept_its_place() -> None:
    tape = TP.settle(identical_rows(2), 2, None)
    rows = (fresh(tape)[0], TP.Song(2, "Changed", "A Band", "A Record", 90.0))
    got = TP.relist(tape, rows).songs
    assert [s.settled_by for s in got] == [M.PERSON, ""]


def test_a_new_row_identical_to_a_chosen_one_starts_unchosen() -> None:
    tape = TP.settle(identical_rows(1), 1, None)
    rows = fresh(identical_rows(2))
    assert [s.settled_by for s in TP.relist(tape, rows).songs] == [M.PERSON, ""]


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


def test_an_album_also_a_compilation_says_album_when_the_list_names_it() -> None:
    """A person read "compilation" on the album they asked for and took fright."""
    made = M.propose(wanted(), [edition(secondary=("Compilation",))])
    named = M.options(wanted(), made)
    assert [o["kind"] for o in named] == ["album"]
    other = M.options(replace(wanted(), album="Another Thing"), made)
    assert [o["kind"] for o in other] == ["compilation"]


@pytest.mark.parametrize(
    ("title", "said"),
    [
        ("Live Forever", "live, 1995"),
        ("Mixed Up", "club mix"),
        ("Editor", "radio edit"),
    ],
)
def test_a_word_in_a_title_excused_a_version_of_it_it_does_not_name(
    title: str, said: str
) -> None:
    got = M.propose(wanted(title), [edition(title=title, disambiguation=said)])
    assert got.status == M.REVIEW
    assert said in got.reason


@pytest.mark.parametrize(
    ("title", "said"),
    [
        ("Song (Live, 1995)", "live, 1995"),
        ("Song [Club Mix]", "club mix"),
        ("Song - Radio Edit", "radio edit"),
    ],
)
def test_a_title_s_version_part_still_excuses_the_version_it_names(
    title: str, said: str
) -> None:
    got = M.propose(wanted(title), [edition(title=title, disambiguation=said)])
    assert got.status == M.MATCHED


@pytest.mark.parametrize("title", ["Song (feat. X Remix)", "Song - feat. X Remix"])
def test_a_guest_clause_ate_the_version_and_graded_exact_against_the_plain_title(
    title: str,
) -> None:
    assert M.propose(wanted(title), [edition(title="Song")]).status == M.REVIEW
    assert M.propose(wanted("Song"), [edition(title=title)]).status == M.REVIEW
    assert M.propose(wanted(title), [edition(title=title)]).status == M.MATCHED


def test_a_guest_clause_with_no_version_in_it_is_still_only_a_credit() -> None:
    plain = M.propose(wanted("Song (feat. Somebody)"), [edition(title="Song")])
    assert plain.status == M.MATCHED


def test_a_guest_clause_is_never_a_name_or_part_of_a_search_for_who() -> None:
    assert M.search_artists("A Band (feat. X Remix)") == ("A Band",)
    assert M.lead_credited("A Band (feat. X Remix)", ("A Band",))
    assert M.search_title("Song (feat. X Remix)") == "Song (feat. X Remix)"
    assert M.search_title("Song (feat. Somebody)") == "Song"


def test_the_library_reads_a_guest_clause_holding_a_version_as_another_recording() -> (
    None
):
    from ripdoctor.core import filing as F

    song = TP.Song(1, "Song (feat. X Remix)", "A Band", "A Record", 200.0)
    row = F.Row("Song", "A Band", "Elsewhere", 200.0, "/lib/song.flac")
    assert not F.alike(song, row)
    assert F.alike(replace(song, title="Song (feat. Somebody)"), row)


def test_an_unnamed_remix_album_was_accepted_for_a_title_that_says_no_remix() -> None:
    remix = edition(album="Remixed", secondary=("Remix",))
    got = M.propose(wanted(album="A Record"), [remix])
    assert got.status == M.REVIEW
    assert "remix album" in got.reason


def test_a_remix_album_the_list_names_or_a_remix_it_says_is_still_accepted() -> None:
    named = edition(album="A Record", secondary=("Remix",))
    assert M.propose(wanted(), [named]).status == M.MATCHED
    said = edition(title="A Song (Club Mix)", album="Club Mixes", secondary=("Remix",))
    assert M.propose(wanted("A Song (Club Mix)"), [said]).status == M.MATCHED


@pytest.mark.parametrize(
    "name", ["Tyler, The Creator", "Sun, Moon & Stars", "Earth, Wind"]
)
def test_a_credit_holding_a_comma_went_to_review_against_itself(name: str) -> None:
    got = M.propose(wanted(artist=name), [edition(artist=name, credit=(name,))])
    assert got.status == M.MATCHED


def test_a_comma_credit_is_not_agreed_by_a_name_it_only_shares_a_word_with() -> None:
    got = M.propose(
        wanted(artist="Tyler, The Creator"),
        [edition(artist="Tyler Childers", credit=("Tyler Childers",))],
    )
    assert got.status == M.REVIEW
