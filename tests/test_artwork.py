"""Cover art: proving it is one before touching anything.

Every test here corresponds to a way this went wrong once. The expensive one
was an album losing the art it had to an HTML error page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.integrations import artwork as ART

JPEG = b"\xff\xd8\xff" + b"\x00" * 4000
ERROR_PAGE = b"<html><body>404 not found</body></html>"


def probed(width: int = 1000, height: int = 1000) -> bytes:
    return json.dumps(
        {"streams": [{"width": width, "height": height, "codec_name": "mjpeg"}]}
    ).encode()


class Fetching:
    """A catalogue that answers from a script."""

    def __init__(self, **replies: bytes | Exception) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def get(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append(url)
        for key, reply in self.replies.items():
            if key in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise OSError("404 not found")


def a_runner(*, image: bytes | None = None) -> FakeRunner:
    fake = FakeRunner()
    return fake.expect("ffprobe", stdout=image if image is not None else probed())


# --------------------------------------------------------------- probing


def test_a_real_image_reports_its_dimensions() -> None:
    found = ART.probe(a_runner(), JPEG)
    assert found is not None and found.width == 1000 and str(found) == "1000x1000"


def test_an_error_page_is_not_an_image() -> None:
    """The archive answers 200 with a 170-byte HTML error page. It decodes to
    no stream, so it has no dimensions, so it is not an image."""
    fake = FakeRunner().expect("ffprobe", returncode=1, stderr=b"invalid data")
    assert ART.probe(fake, ERROR_PAGE) is None


def test_output_that_is_not_json_is_not_an_image() -> None:
    assert ART.probe(a_runner(image=b"not json"), JPEG) is None


@pytest.mark.parametrize(("edge", "ok"), [(499, False), (500, True), (3000, True)])
def test_the_size_floor_is_what_it_says(edge: int, ok: bool) -> None:
    assert ART.Image(edge, edge).big_enough is ok


# ------------------------------------------------------------ candidates


def test_both_the_release_and_its_group_are_asked() -> None:
    """The archive 404s on a release while the group has art at 316x316. Both
    are asked, and the size is reported rather than assumed."""
    fetcher = Fetching(**{"release/": JPEG, "release-group/": JPEG})
    found = ART.candidates(
        fetcher, a_runner(image=probed(316, 316)), mbid="aaa", release_group="bbb"
    )
    assert [c.source for c in found] == ["release", "release-group"]
    assert all(c.ok for c in found)
    assert not any(c.as_dict()["big_enough"] for c in found), "316x316 passed"


def test_a_release_with_no_art_is_reported_not_raised() -> None:
    fetcher = Fetching(**{"release-group/": JPEG})
    found = ART.candidates(fetcher, a_runner(), mbid="aaa", release_group="bbb")
    assert not found[0].ok and found[1].ok
    assert "404" in found[0].why


def test_an_error_page_candidate_says_what_it_actually_was() -> None:
    fetcher = Fetching(**{"release/": ERROR_PAGE})
    fake = FakeRunner().expect("ffprobe", returncode=1)
    found = ART.candidates(fetcher, fake, mbid="aaa")
    assert not found[0].ok
    assert "error page" in found[0].why and str(len(ERROR_PAGE)) in found[0].why


def test_an_album_page_yields_the_original_upload_not_a_thumbnail() -> None:
    html = b'<img src="https://f4.bcbits.com/img/a1234567_16.jpg">'
    fetcher = Fetching(**{"bandcamp.com": html, "f4.bcbits.com": JPEG})
    found = ART.candidates(
        fetcher,
        a_runner(image=probed(3000, 3000)),
        page="https://x.bandcamp.com/album/y",
    )
    assert found[0].ok and found[0].url.endswith("a1234567_0.jpg")


def test_a_page_with_no_cover_says_so() -> None:
    fetcher = Fetching(**{"bandcamp.com": b"<html>nothing here</html>"})
    found = ART.candidates(fetcher, a_runner(), page="https://x.bandcamp.com/album/y")
    assert not found[0].ok and "no cover image" in found[0].why


def test_nothing_asked_returns_nothing() -> None:
    assert ART.candidates(Fetching(), a_runner()) == []


def test_text_searching_sources_are_not_consulted() -> None:
    """They return an exact match for anything whose title resembles the album,
    so a remix cover outranks the real one and nothing says so."""
    import inspect

    source = inspect.getsource(ART)
    for site in ("itunes", "albumart", "google", "bing"):
        assert site not in source.lower().replace("# ", "")


# -------------------------------------------------------------- installing


def an_album(tmp_path: Path, tracks: int = 2) -> Path:
    album = tmp_path / "A Band" / "A Record"
    album.mkdir(parents=True)
    for i in range(tracks):
        (album / f"{i + 1:02d} Track.flac").write_bytes(b"fLaC" + b"\x00" * 2000)
    return album


class Scaling(FakeRunner):
    """An ffmpeg that leaves behind the cover it was asked to write."""

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if args[0] == "ffmpeg" and args[-1].endswith(".jpg"):
            Path(args[-1]).write_bytes(JPEG)
        return super().run(argv, stdin=stdin, timeout=timeout)


def installing(image: bytes | None = None) -> FakeRunner:
    return Scaling().expect("ffprobe", stdout=image if image else probed())


def test_art_is_written_and_embedded_in_every_track(tmp_path: Path) -> None:
    """A player set to read embedded art only sees nothing from a cover.jpg,
    and embedding survives a file being moved."""
    album = an_album(tmp_path)
    done = ART.install(installing(), album, JPEG)
    assert (album / "cover.jpg").is_file()
    assert done.embedded == 2 and done.tracks == 2 and not done.failed


def test_an_error_page_is_refused_before_anything_is_touched(
    tmp_path: Path,
) -> None:
    """The expensive failure: an album's art removed, and then the replacement
    turning out to be a 170-byte HTML page."""
    album = an_album(tmp_path)
    (album / "cover.jpg").write_bytes(b"the existing cover")
    fake = Scaling().expect("ffprobe", returncode=1)
    with pytest.raises(ValueError, match="not a decodable image"):
        ART.install(fake, album, ERROR_PAGE)
    assert (album / "cover.jpg").read_bytes() == b"the existing cover"


def test_something_too_small_is_refused_with_its_size(tmp_path: Path) -> None:
    album = an_album(tmp_path)
    with pytest.raises(ValueError, match="316x316"):
        ART.install(installing(probed(316, 316)), album, JPEG)


def test_no_download_is_left_behind(tmp_path: Path) -> None:
    album = an_album(tmp_path)
    ART.install(installing(), album, JPEG)
    assert not list(album.glob(".cover-download"))


def test_a_cover_is_fitted_inside_a_square_not_squashed_into_one() -> None:
    """scale=N:N on its own squashes anything that is not square, and that
    looks like a mistake in every player that shows it."""
    argv = ART.scale_argv("/in.jpg", "/out.jpg", 1400)
    graph = argv[argv.index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in graph


# --------------------------------------------------------------- current


def test_what_a_record_already_has_is_reportable(tmp_path: Path) -> None:
    album = an_album(tmp_path)
    (album / "cover.jpg").write_bytes(JPEG)
    fake = a_runner().expect("--list", stdout=b"", stderr=b"")
    found = ART.current(fake, album)
    assert found["cover"]["name"] == "cover.jpg"  # type: ignore[index]
    assert len(found["tracks"]) == 2  # type: ignore[arg-type]


def test_a_record_that_is_not_there_reports_nothing(tmp_path: Path) -> None:
    assert ART.current(a_runner(), tmp_path / "absent")["cover"] is None
