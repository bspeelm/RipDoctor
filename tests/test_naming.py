"""Names: what is refused, and what is repaired.

The two are deliberately different. A path component supplied by a caller is
validated and refused - rewriting it would hide an attack, or corrupt a name
that was fine. A track title is repaired, because it comes from a catalogue and
has to become a filename whatever it contains.
"""

from __future__ import annotations

import pytest

from ripdoctor.core import naming as N

# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "good",
    [
        "a",
        "Orphan",
        "side-a",
        "Punch-Brothers---Hell-On-Church-Street",
        "album.2022",
        "hidden_track",
        "_clips-album",
        "1",
    ],
)
def test_real_names_are_accepted(good: str) -> None:
    assert N.token(good) == good
    assert N.is_token(good)


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ("..", "the traversal itself"),
        ("../etc", "traversal with a path"),
        ("a/../b", "traversal in the middle"),
        ("a/b", "a separator makes it two components"),
        ("/abs", "absolute"),
        (".hidden", "leading dot hides the file"),
        ("-rf", "leading hyphen reads as an option"),
        ("", "empty"),
        ("x" * 201, "longer than any real name"),
        ("a\x00b", "an embedded null"),
        ("a\nb", "an embedded newline"),
        ("a b", "a space is not a component"),
        ("café", "non-ascii is not validated, only slugs are folded"),
    ],
)
def test_dangerous_or_malformed_names_are_refused(bad: str, why: str) -> None:
    with pytest.raises(N.Unsafe):
        N.token(bad)
    assert not N.is_token(bad), why


def test_a_leading_underscore_is_allowed_and_a_leading_dot_is_not() -> None:
    """One marks a directory as not a record; the other hides it."""
    assert N.is_token("_clips-album")
    assert not N.is_token(".clips-album")
    assert not N.is_token("-clips-album")


def test_refusal_names_the_offending_value() -> None:
    """A rejection nobody can read is one that gets worked around."""
    with pytest.raises(N.Unsafe, match="drop-table"):
        N.token("../drop-table")


def test_a_non_string_is_refused_rather_than_coerced() -> None:
    with pytest.raises(N.Unsafe, match="not a string"):
        N.token(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ slugs


def test_a_slug_is_a_valid_token() -> None:
    """The point of the function: whatever goes in, the result is usable."""
    for artist, album in [
        ("Punch Brothers", "Hell on Church Street"),
        ("Sigur Rós", "( )"),
        ("Björk", "Post"),
        ("AC/DC", "Back in Black"),
        ("!!!", "Myth Takes"),
        ("Lana Del Rey", "Norman Fucking Rockwell!"),
        ("", "Untitled"),
    ]:
        s = N.slug(artist, album)
        assert N.is_token(s), f"{artist} / {album} -> {s!r}"


def test_the_artist_and_album_stay_readable_apart() -> None:
    s = N.slug("Punch Brothers", "Hell on Church Street")
    assert s == "Punch-Brothers---Hell-On-Church-Street".replace("Hell-On", "Hell-on")
    assert "---" in s
    artist, _, album = s.partition("---")
    assert artist == "Punch-Brothers"
    assert album == "Hell-on-Church-Street"


def test_a_hyphen_in_the_title_does_not_look_like_the_separator() -> None:
    """Why the separator is three hyphens rather than one."""
    s = N.slug("Sunn O)))", "White-Box")
    assert s.count("---") == 1
    assert s.partition("---")[2] == "White-Box"


def test_accents_are_folded_not_dropped() -> None:
    """Dropping them loses whole words on some records."""
    assert N.slug("Sigur Rós", "Ágætis byrjun") == "Sigur-Ros---Agaetis-byrjun"


def test_a_slash_becomes_a_separator_not_a_directory() -> None:
    assert "/" not in N.slug("AC/DC", "Back in Black")
    assert N.is_token(N.slug("AC/DC", "Back in Black"))


def test_a_name_with_nothing_usable_is_refused() -> None:
    with pytest.raises(N.Unsafe, match="nothing usable"):
        N.slug("", "...")
    with pytest.raises(N.Unsafe, match="nothing usable"):
        N.slug("///", "   ")


def test_a_very_long_title_is_truncated_to_something_usable() -> None:
    s = N.slug("A", "B" * 500)
    assert len(s) <= N.MAX_COMPONENT
    assert N.is_token(s)


# -------------------------------------------------------------- filenames


def test_a_title_is_repaired_rather_than_refused() -> None:
    """It comes from a catalogue and must become a file whatever it contains."""
    assert N.safe_filename("Church Street Blues") == "Church Street Blues"
    assert N.safe_filename("Don't Think Twice, It's All Right") == (
        "Don't Think Twice, It's All Right"
    )
    assert N.safe_filename("Bird's Lament (In Memory of Charlie Parker)") == (
        "Bird's Lament (In Memory of Charlie Parker)"
    )


def test_a_separator_in_a_title_cannot_create_a_directory() -> None:
    assert "/" not in N.safe_filename("AC/DC Medley")
    assert "\\" not in N.safe_filename("back\\slash")


def test_punctuation_is_replaced_rather_than_deleted() -> None:
    """Deleting it would collide a punctuated title with an unpunctuated one.

    Two *different* disallowed characters still collide with each other - both
    become an underscore - which is accepted. What must not happen is a title
    losing a character entirely and landing on a different track's name.
    """
    assert N.safe_filename("Song: Part One") == "Song_ Part One"
    assert N.safe_filename("Song Part One") == "Song Part One"
    assert N.safe_filename("Song: Part One") != N.safe_filename("Song Part One")


def test_a_title_that_would_hide_the_file_is_replaced() -> None:
    assert N.safe_filename(".hidden") == "untitled"
    assert N.safe_filename("///") == "untitled"
    assert N.safe_filename("   ") == "untitled"


def test_a_track_filename_sorts_into_running_order() -> None:
    names = [N.track_filename(n, f"Track {n}") for n in (1, 2, 10, 11)]
    assert names[0] == "01 Track 1.flac"
    assert names == sorted(names), "zero padding is what makes this true"


def test_a_track_filename_is_never_empty_or_hidden() -> None:
    assert N.track_filename(3, "") == "03 untitled.flac"
    assert not N.track_filename(4, ".hidden").startswith(".")
