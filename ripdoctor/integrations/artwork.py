"""Cover art: find candidates, prove they are images, then install them.

Every check here exists because of a specific failure.

The archive's `/front` endpoint can answer 200 with a 170-byte HTML error page.
Removing a record's existing art and then discovering that is how an album ends
up with none, so nothing is removed until the replacement has been decoded and
measured.

The archive also 404s on a release while the release *group* has art at 316x316 -
too small to be worth having. Both are asked and the size is reported rather
than assumed.

Text-searching sources are deliberately not consulted. They return an exact
match for anything whose title merely resembles the album, so a remix cover
outranks the real one and nothing about the answer says so.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.audio.runner import Runner
from ripdoctor.integrations.musicbrainz import Fetcher, LookupFailed
from ripdoctor.integrations.tagger import embed_art, has_art

ARCHIVE = "https://coverartarchive.org"

# The floor an installed cover has to clear. Below this it is worse than what a
# player already shows for a record with none.
MIN_EDGE = 500

# What a cover is scaled to before embedding. Large enough to look right on a
# screen, small enough that it is not several megabytes in every track.
SCALE_TO = 1400

COVER = "cover.jpg"


@dataclass(frozen=True, slots=True)
class Image:
    width: int
    height: int
    codec: str = ""

    @property
    def edge(self) -> int:
        return min(self.width, self.height)

    @property
    def big_enough(self) -> bool:
        return self.edge >= MIN_EDGE

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True, slots=True)
class Candidate:
    source: str
    url: str
    ok: bool
    why: str = ""
    size: int = 0
    image: Image | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "url": self.url,
            "ok": self.ok,
            "why": self.why,
            "bytes": self.size,
            "width": self.image.width if self.image else 0,
            "height": self.image.height if self.image else 0,
            "big_enough": bool(self.image and self.image.big_enough),
        }


def probe_argv() -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name",
        "-of",
        "json",
        "-",
    ]


def probe(runner: Runner, data: bytes) -> Image | None:
    """Dimensions, or nothing at all.

    Nothing is how a 170-byte HTML error page announces itself: it decodes to
    no stream, so it has no dimensions, so it is not an image.
    """
    result = runner.run(probe_argv(), stdin=data, timeout=30)
    if not result.ok:
        return None
    try:
        stream = json.loads(result.text)["streams"][0]
        return Image(
            width=int(stream["width"]),
            height=int(stream["height"]),
            codec=str(stream.get("codec_name", "")),
        )
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _fetch(fetcher: Fetcher, runner: Runner, url: str, source: str) -> Candidate:
    try:
        data = fetcher.get(url, {"Accept": "image/*"})
    except (OSError, ValueError, LookupFailed) as e:
        return Candidate(source, url, ok=False, why=str(e)[:120])
    found = probe(runner, data)
    if found is None:
        return Candidate(
            source,
            url,
            ok=False,
            size=len(data),
            why=f"not a decodable image ({len(data)} bytes) - probably an error page",
        )
    return Candidate(source, url, ok=True, size=len(data), image=found)


# Bandcamp's `_0` suffix is the original upload, routinely 3000x3000; every
# other suffix is a thumbnail the site made.
_BCBITS = re.compile(r"https://f4\.bcbits\.com/img/a(\d+)_\d+\.jpg")


def candidates(
    fetcher: Fetcher,
    runner: Runner,
    *,
    mbid: str = "",
    release_group: str = "",
    page: str = "",
) -> list[Candidate]:
    """Everything worth offering, each one already proved to be an image."""
    found = []
    if mbid:
        quoted = urllib.parse.quote(mbid, safe="")
        found.append(
            _fetch(fetcher, runner, f"{ARCHIVE}/release/{quoted}/front", "release")
        )
    if release_group:
        quoted = urllib.parse.quote(release_group, safe="")
        found.append(
            _fetch(
                fetcher,
                runner,
                f"{ARCHIVE}/release-group/{quoted}/front",
                "release-group",
            )
        )
    if page:
        found.append(_from_page(fetcher, runner, page))
    return found


def _from_page(fetcher: Fetcher, runner: Runner, page: str) -> Candidate:
    try:
        html = fetcher.get(page, {"Accept": "text/html"}).decode("utf-8", "replace")
    except (OSError, ValueError, LookupFailed) as e:
        return Candidate("page", page, ok=False, why=str(e)[:120])
    found = _BCBITS.search(html)
    if not found:
        return Candidate("page", page, ok=False, why="no cover image on that page")
    return _fetch(
        fetcher,
        runner,
        f"https://f4.bcbits.com/img/a{found.group(1)}_0.jpg",
        "page",
    )


def scale_argv(source: str, dest: str, edge: int = SCALE_TO) -> list[str]:
    """Fit inside a square, never fill it.

    `scale=N:N` on its own squashes a cover that is not square, and a squashed
    cover looks like a mistake in every player that shows it.
    """
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-vf",
        f"scale={edge}:{edge}:force_original_aspect_ratio=decrease",
        "-q:v",
        "2",
        dest,
    ]


@dataclass(frozen=True, slots=True)
class Installed:
    cover: Path
    image: Image
    embedded: int
    failed: tuple[str, ...]
    tracks: int

    def as_dict(self) -> dict[str, object]:
        return {
            "cover": str(self.cover),
            "size": str(self.image),
            "embedded": self.embedded,
            "failed": list(self.failed),
            "tracks": self.tracks,
        }


def install(
    runner: Runner, album_dir: str | Path, data: bytes, *, edge: int = SCALE_TO
) -> Installed:
    """Verify, scale, write the cover, and embed it into every track.

    Verification happens before anything is written or removed. The recorded
    failure was stripping an album's art and then finding the replacement was
    an error page.

    Both the file and the embedded copies are written: a player configured for
    embedded art only sees nothing from a cover.jpg, and embedding survives a
    file being moved, which a sibling file does not.
    """
    found = probe(runner, data)
    if found is None:
        raise ValueError(f"not a decodable image ({len(data)} bytes)")
    if not found.big_enough:
        raise ValueError(f"refusing to install {found}: below {MIN_EDGE} px")

    directory = Path(album_dir)
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / ".cover-download"
    raw.write_bytes(data)
    cover = directory / COVER
    try:
        runner.run(scale_argv(str(raw), str(cover), edge), timeout=120).require()
    finally:
        raw.unlink(missing_ok=True)

    final = probe(runner, cover.read_bytes()) if cover.is_file() else None
    if final is None:
        raise RuntimeError("the scaled cover is not a valid image")

    tracks = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".flac")
    done, failed = 0, []
    for track in tracks:
        try:
            embed_art(runner, str(track), str(cover), final.width, final.height)
            done += 1
        except (ValueError, OSError) as e:
            failed.append(f"{track.name}: {e}")
    return Installed(cover, final, done, tuple(failed), len(tracks))


def current(runner: Runner, album_dir: str | Path) -> dict[str, object]:
    """What art this record already has, on disk and in the files."""
    directory = Path(album_dir)
    if not directory.is_dir():
        return {"cover": None, "tracks": []}
    cover = None
    for name in (COVER, "cover.png", "folder.jpg"):
        where = directory / name
        if where.is_file():
            found = probe(runner, where.read_bytes())
            cover = {
                "name": name,
                "bytes": where.stat().st_size,
                "size": str(found) if found else None,
            }
            break
    return {
        "cover": cover,
        "tracks": [
            {"file": p.name, "has_art": has_art(runner, str(p))}
            for p in sorted(directory.iterdir())
            if p.suffix.lower() == ".flac"
        ],
    }
