"""Turning names into safe path components, and validating them. ADR-025."""

from __future__ import annotations

import re
import unicodedata

# Alphanumerics, dot, underscore and hyphen, and the first character may not be
# a dot or a hyphen: one hides the file, the other is read as an option by
# anything that later passes the name to a command. A leading underscore is
# allowed, because it does neither and marks a directory as not being a record.
_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

MAX_COMPONENT = 200

# Punctuation a track title may keep in a filename. Everything else becomes an
# underscore rather than being dropped, so two titles differing only in
# punctuation do not collide.
_TITLE_KEEP = " -_.,'!&()"


class Unsafe(ValueError):
    """A name was rejected rather than repaired."""


def token(s: str) -> str:
    """Validate one path component.

    Raises rather than sanitising: silently rewriting a bad name hides an
    attack, and silently rewriting a good one corrupts it.
    """
    if not isinstance(s, str):
        raise Unsafe(f"not a string: {s!r}")
    if not s or len(s) > MAX_COMPONENT:
        raise Unsafe(f"bad length: {s!r}")
    if ".." in s or not _SAFE.match(s):
        raise Unsafe(f"bad path component: {s!r}")
    return s


def is_token(s: str) -> bool:
    try:
        token(s)
    except Unsafe:
        return False
    return True


def slug(artist: str, album: str) -> str:
    """A directory name for one record.

    Three hyphens separate artist from album, so the pair can still be read
    apart by eye on a record whose title contains a hyphen.
    """
    joined = f"{artist.strip()}---{album.strip()}" if artist.strip() else album.strip()
    out = _slugify(joined)
    if not out:
        raise Unsafe(f"nothing usable in {artist!r} / {album!r}")
    return out[:MAX_COMPONENT]


# Letters that survive NFKD because they are not accented forms of anything -
# ligatures and letters in their own right. Decomposition leaves them intact and
# the ASCII pass then deletes them, so "Agaetis" becomes "Agtis" and a word is
# lost. Spelled out instead.
_TRANSLITERATE = str.maketrans(
    {
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ø": "o",
        "Ø": "O",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "TH",
        "ß": "ss",
        "ł": "l",
        "Ł": "L",
        "đ": "d",
        "Đ": "D",
        "ı": "i",
        "№": "No",
    }
)


def _slugify(text: str) -> str:
    # Accents fold away; the letters above have to be spelled out first.
    folded = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE))
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    swapped = re.sub(r"[\s/\\]+", "-", ascii_only)
    kept = re.sub(r"[^A-Za-z0-9._-]", "", swapped)
    collapsed = re.sub(r"-{4,}", "---", kept)
    return collapsed.strip("-._")


def safe_filename(title: str) -> str:
    """A track title, made safe to write.

    Disallowed characters become underscores rather than disappearing, so two
    titles differing only in punctuation do not collide in one directory.
    """
    out = "".join(
        c if (c.isalnum() or c in _TITLE_KEEP) else "_" for c in title
    ).strip()
    # A name of only separators, or one that would hide the file, is not usable.
    if not out.strip("_. -") or out.startswith("."):
        return "untitled"
    return out[:MAX_COMPONENT]


def track_filename(number: int, title: str, suffix: str = ".flac") -> str:
    """`01 Title.flac` - numbered so a directory listing is the running order."""
    return f"{number:02d} {safe_filename(title)}{suffix}"
