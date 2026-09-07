"""beets, for real.

Everything else about the importer runs against a scripted runner, which proves
the arguments this project builds and nothing about how beets answers them. The
two things that went wrong on a real install were both of that kind: a format
string beets passed through literally, and an import that declined without
saying so.

These need the real binary. `--require=needs_beets` fails the session if they
were filtered out, because a job meant to run them and running none of them
would otherwise report green.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ripdoctor.audio.runner import RealRunner
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack
from ripdoctor.integrations.importer import Beets

pytestmark = pytest.mark.needs_beets

HAVE_BEETS = shutil.which("beet") is not None
HAVE_FFMPEG = (
    shutil.which("ffmpeg") is not None and shutil.which("metaflac") is not None
)

if not HAVE_BEETS:  # pragma: no cover - the point of the marker
    pytest.skip("beet is not installed", allow_module_level=True)


ALBUM = "A Record"
ARTIST = "A Band"


def a_plan() -> Plan:
    return Plan(
        slug="album",
        album=ALBUM,
        artist=ARTIST,
        date="2022",
        sides=(
            PlanSide(
                file="side-a.flac",
                tracks=(
                    PlanTrack(number=1, title="One", start=1.0, end=9.0, cat=8.0),
                    PlanTrack(number=2, title="Two", start=10.0, end=19.0, cat=9.0),
                ),
            ),
        ),
    )


def a_config(tmp_path: Path) -> Beets:
    """A beets that touches nothing outside this test.

    Its own library database and its own music directory, and no plugins: what
    is being tested is this project's use of beets, not whichever plugins the
    machine running the tests happens to have enabled.
    """
    library = tmp_path / "music"
    library.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "beets.yaml"
    config.write_text(
        f"directory: {library}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "plugins: []\n"
        "import:\n"
        "  move: yes\n"
        "  quiet: yes\n"
        "  log: \n"
    )
    return Beets(config=str(config))


# ---------------------------------------------------------------- locate


def test_the_format_string_survives_real_beets(tmp_path: Path) -> None:
    """The separator is built in Python because beets passes an escape through
    literally: every line then fails to split and the album reads as empty."""
    beets = a_config(tmp_path)
    where, count = beets.locate(RealRunner(), str(tmp_path / "music"), ARTIST, ALBUM)
    assert (where, count) == (None, 0), "an empty library reported something"


def test_a_query_beets_cannot_parse_is_not_a_crash(tmp_path: Path) -> None:
    beets = a_config(tmp_path)
    assert beets.locate(RealRunner(), str(tmp_path), "", "a: b: c") == (None, 0)


def test_beets_is_actually_runnable(tmp_path: Path) -> None:
    """Guards the two above: both pass against a beets that errors on start."""
    beets = a_config(tmp_path)
    result = RealRunner().run(["beet", "--config", beets.config, "version"])
    assert result.ok and "beets version" in result.text


# ---------------------------------------------------------------- import


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and metaflac to make files")
@pytest.mark.needs_ffmpeg
def test_a_cut_record_goes_into_a_real_library(tmp_path: Path) -> None:
    """The whole point of the dependency, against the real thing."""
    review = tmp_path / "review"
    review.mkdir()
    from ripdoctor.core.naming import track_filename

    runner = RealRunner()
    for n, title in ((1, "One"), (2, "Two")):
        dest = review / track_filename(n, title)
        runner.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "flac",
                str(dest),
            ],
            timeout=120,
        ).require()
        runner.run(
            [
                "metaflac",
                f"--set-tag=ALBUM={ALBUM}",
                f"--set-tag=ALBUMARTIST={ARTIST}",
                f"--set-tag=ARTIST={ARTIST}",
                f"--set-tag=TITLE={title}",
                f"--set-tag=TRACKNUMBER={n}",
                str(dest),
            ],
            timeout=120,
        ).require()

    beets = a_config(tmp_path)
    library = str(tmp_path / "music")
    done = beets.apply(runner, a_plan(), str(review), library)

    assert done.tracks == 2, done.output
    assert done.where is not None and done.where.is_dir()
    assert len(list(done.where.glob("*.flac"))) == 2
    assert not list(review.glob("*.flac")), "beets left the tracks in review"

    where, count = beets.locate(runner, library, ARTIST, ALBUM)
    assert count == 2 and where == done.where
