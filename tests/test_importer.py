"""Two ways into a library, behind one interface.

beets is not installed here and never will be for a test. Everything it does
goes through the runner, which is the point of the seam.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack
from ripdoctor.integrations import importer as I

SEP = "\x1f"


def a_plan() -> Plan:
    return Plan(
        slug="album",
        album="A Record",
        artist="A Band",
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


def a_review(tmp_path: Path) -> str:
    from ripdoctor.integrations.tagger import placements

    review = tmp_path / "review"
    review.mkdir()
    for p in placements(a_plan(), str(review), str(tmp_path / "music")):
        p.source.write_bytes(b"fLaC" + b"\x00" * 2000)
    return str(review)


# ------------------------------------------------------------- choosing


def test_the_base_install_gets_the_tagger() -> None:
    assert I.choose(FakeRunner(), "tagger").name == "tagger"


def test_asking_for_beets_gets_beets_when_it_is_there() -> None:
    assert I.choose(FakeRunner(installed={"beet"}), "beets").name == "beets"


def test_asking_for_beets_without_it_falls_back_rather_than_refusing() -> None:
    """Being told to install something at the last step of a twenty-minute job
    is not a useful answer."""
    assert I.choose(FakeRunner(installed=set()), "beets").name == "tagger"


def test_each_one_knows_whether_it_can_run() -> None:
    assert I.Tagger().available(FakeRunner(installed={"metaflac"}))
    assert not I.Tagger().available(FakeRunner(installed=set()))
    assert I.Beets().available(FakeRunner(installed={"beet"}))
    assert not I.Beets().available(FakeRunner(installed=set()))


# --------------------------------------------------------------- tagger


def test_the_tagger_places_every_track(tmp_path: Path) -> None:
    review = a_review(tmp_path)
    done = I.Tagger().apply(FakeRunner(), a_plan(), review, str(tmp_path / "music"))
    assert done.tracks == 2 and done.where is not None
    assert len(list(done.where.glob("*.flac"))) == 2


def test_the_tagger_previews_where_things_would_go(tmp_path: Path) -> None:
    text = I.Tagger().preview(
        FakeRunner(), a_plan(), str(tmp_path), str(tmp_path / "music")
    )
    assert "->" in text and "A Record" in text


def test_the_tagger_finds_what_it_placed(tmp_path: Path) -> None:
    review = a_review(tmp_path)
    library = str(tmp_path / "music")
    I.Tagger().apply(FakeRunner(), a_plan(), review, library)
    where, count = I.Tagger().locate(FakeRunner(), library, "A Band", "A Record")
    assert where is not None and count == 2


# ---------------------------------------------------------------- beets


def beets_runner(*, listed: int = 2, leaves: int = 0, **kw: object) -> FakeRunner:
    rows = "\n".join(
        f"A Record{SEP}/music/A Band/A Record/{i + 1:02d} Track.flac"
        for i in range(listed)
    )
    return FakeRunner(installed={"beet"}, **kw).expect(  # type: ignore[arg-type]
        "ls", stdout=rows.encode()
    )


def test_the_release_is_pinned_rather_than_guessed(tmp_path: Path) -> None:
    """Freshly cut files carry no tags at all, so even the right release scores
    about half and a quiet import skips it without a word."""
    fake = beets_runner()
    I.Beets().preview(fake, a_plan(), str(tmp_path), "/music", mbid="aaa")
    argv = fake.argv_for("import")
    assert "--search-id" in argv and argv[argv.index("--search-id") + 1] == "aaa"


def test_with_no_release_it_imports_as_an_album_rather_than_matching(
    tmp_path: Path,
) -> None:
    fake = beets_runner()
    I.Beets().preview(fake, a_plan(), str(tmp_path), "/music")
    assert "-A" in fake.argv_for("import")


def test_the_preview_runs_with_nothing_on_its_input(tmp_path: Path) -> None:
    """beets has no dry run. Run with stdin closed it prints the whole
    candidate, asks for an answer, and dies without touching a file."""
    import inspect

    source = inspect.getsource(I.Beets.preview)
    assert 'stdin=b""' in source


def test_applying_answers_the_matcher(tmp_path: Path) -> None:
    """The identical command the preview ran, with the answer piped in."""
    review = tmp_path / "review"
    review.mkdir()
    fake = beets_runner()
    I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="aaa")
    assert "import" in fake.argv_for("import")


def test_a_path_beets_names_but_does_not_have_is_not_a_crash(
    tmp_path: Path,
) -> None:
    """Whatever went wrong there, the import itself succeeded."""
    review = tmp_path / "review"
    review.mkdir()
    done = I.Beets().apply(beets_runner(), a_plan(), str(review), "/music")
    assert done.tracks == 2 and not done.notes


def test_colour_codes_do_not_reach_the_page(tmp_path: Path) -> None:
    fake = beets_runner()
    fake.expect("import", stdout=b"\x1b[31mSkipping\x1b[0m the album")
    text = I.Beets().preview(fake, a_plan(), str(tmp_path), "/music")
    assert "\x1b" not in text and "Skipping the album" in text


def test_tracks_left_in_review_mean_it_did_not_import(tmp_path: Path) -> None:
    """beets leaves the files where they were when it declines, so what is
    still in review is the honest measure of what did not go in."""
    review = tmp_path / "review"
    review.mkdir()
    (review / "01 One.flac").write_bytes(b"fLaC")
    fake = beets_runner()
    fake.expect("import", stdout=b"Skipping album")
    with pytest.raises(I.ImportFailed, match="still in review"):
        I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="aaa")


def test_beets_is_asked_where_it_put_things(tmp_path: Path) -> None:
    """It owns the naming. A gate checking the path this project would have
    chosen would be checking the wrong directory on every install."""
    fake = beets_runner(listed=11)
    where, count = I.Beets().locate(fake, "/music", "A Band", "A Record", mbid="aaa")
    assert count == 11 and where == Path("/music/A Band/A Record")
    assert "mb_albumid:aaa" in fake.argv_for("ls")


def test_a_record_beets_does_not_have_is_absent_not_an_error() -> None:
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=b"")
    assert I.Beets().locate(fake, "/music", "A Band", "A Record") == (None, 0)


def test_the_separator_survives_the_format_string() -> None:
    """Written as an escape in the format string beets passes it through
    literally, every line fails to split, and the album reads as empty."""
    fake = beets_runner()
    I.Beets().locate(fake, "/music", "A Band", "A Record")
    assert SEP in fake.argv_for("ls")[fake.argv_for("ls").index("-f") + 1]


def test_an_imported_album_is_left_readable_by_the_group(tmp_path: Path) -> None:
    """beets rewrites every file as it writes tags, and the rewrite lands at
    0600 rather than inheriting the directory. Invisible while the player runs
    as the owning user, and broken for anything else."""
    placed = tmp_path / "music" / "A Band" / "A Record"
    placed.mkdir(parents=True)
    for i in range(2):
        f = placed / f"{i + 1:02d} Track.flac"
        f.write_bytes(b"fLaC")
        f.chmod(0o600)

    review = tmp_path / "review"
    review.mkdir()
    rows = "\n".join(f"A Record{SEP}{p}" for p in sorted(placed.glob("*.flac")))
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=rows.encode())
    done = I.Beets().apply(fake, a_plan(), str(review), str(tmp_path / "music"))
    assert done.tracks == 2
    for f in placed.glob("*.flac"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o664
    assert done.notes and "modes" in done.notes[0]


# ------------------------------------------------------- the one override


def test_the_prompt_is_turned_back_on(tmp_path: Path) -> None:
    """Under `quiet: yes` - what an unattended import is configured with -
    beets never asks. It applies anything above the match threshold and skips
    anything below it without a word, so there is no such thing as a preview:
    the command meant to show a candidate has already moved the files.
    """
    beets = I.Beets.with_override(tmp_path)
    written = (tmp_path / I.OVERRIDE_FILE).read_text()
    assert "quiet: no" in written and "timid: no" in written
    assert beets.config.endswith(I.OVERRIDE_FILE)


def test_the_cut_tracks_are_moved_rather_than_copied(tmp_path: Path) -> None:
    """beets copies by default, which wrote every track twice and left the
    originals in review. Since what is still in review is how a refused import
    is told from a finished one, a successful import reported itself failed.

    Not a preference being overridden: the review directory belongs to this
    project, and once a track is in the library there is nothing left for a
    copy of it to be for."""
    I.Beets.with_override(tmp_path)
    assert "move: yes" in (tmp_path / I.OVERRIDE_FILE).read_text()


def test_the_override_never_says_where_a_record_goes(tmp_path: Path) -> None:
    """A single `-c` adds to the user's configuration rather than replacing it.
    Two do not layer - the last simply wins - so there is exactly one.

    What it must never carry is the library. Where a record is filed is beets'
    own business and the person's own decision, and a second place naming it
    would be a second thing to keep in step. `ripdoctor doctor` reports when
    the two disagree instead."""
    written = I.Beets.with_override(tmp_path)
    fake = beets_runner()
    written.preview(fake, a_plan(), str(tmp_path), "/music", mbid="aaa")
    argv = fake.argv_for("import")
    assert argv.count("--config") == 1
    for setting in ("directory", "library", "plugins", "strong_rec_thresh"):
        assert setting not in (tmp_path / I.OVERRIDE_FILE).read_text()


def test_every_beets_command_carries_it(tmp_path: Path) -> None:
    """Including the listing: a query answered under a different configuration
    is a query against a different library."""
    beets = I.Beets.with_override(tmp_path)
    fake = beets_runner()
    beets.locate(fake, "/music", "A Band", "A Record")
    assert "--config" in fake.argv_for("ls")


def test_choosing_beets_without_somewhere_to_write_still_works() -> None:
    """The command line has no state directory in every context."""
    chosen = I.choose(FakeRunner(installed={"beet"}), "beets")
    assert chosen.name == "beets"


def test_a_record_with_no_name_is_not_every_record_in_the_library(
    tmp_path: Path,
) -> None:
    """`beet ls album:` matches them all, so the gate reported a record that
    had nothing to do with the one being imported - naming another album's
    directory as the one about to be written into."""
    beets = I.Beets.with_override(tmp_path)
    fake = beets_runner()
    assert beets.locate(fake, "/music", "", "") == (None, 0)
    with pytest.raises(AssertionError):
        fake.argv_for("ls")  # beets was never asked


class Moving(FakeRunner):
    """A beets that takes the tracks, the way `move: yes` does."""

    def __init__(self, review: Path, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.review = review

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if "import" in args:
            for f in self.review.glob("*.flac"):
                f.unlink()
        return super().run(argv, stdin=stdin, timeout=timeout)


def moving_beets(review: Path, listed: int = 2) -> Moving:
    rows = "\n".join(
        f"A Record{SEP}/music/A Band/A Record/{i + 1:02d} Track.flac"
        for i in range(listed)
    )
    fake = Moving(review, installed={"beet", "metaflac"})
    return fake.expect("ls", stdout=rows.encode())  # type: ignore[return-value]


def test_a_record_with_no_release_is_tagged_before_it_is_imported(
    tmp_path: Path,
) -> None:
    """With no release there is nothing to match against, so beets consults no
    catalogue - and a cut file carries no tags at all, which would file the
    record under nothing but its filenames. The plan is what is known."""
    review = Path(a_review(tmp_path))
    fake = moving_beets(review)
    I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="")
    written = [c for c in fake.calls if c[0] == "metaflac"]
    assert len(written) == 2, "the cut tracks were imported untagged"
    assert any("TITLE=One" in " ".join(c) for c in written)
    assert any("ALBUM=A Record" in " ".join(c) for c in written)


def test_a_record_with_a_release_is_left_to_the_catalogue(tmp_path: Path) -> None:
    """beets is being asked to match it, and tags written first would be
    replaced by the ones it fetches."""
    review = Path(a_review(tmp_path))
    fake = moving_beets(review)
    I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="aaa")
    assert not [c for c in fake.calls if c[0] == "metaflac"]


# ---------------------------------------------------------------- stale rows


def _rows(tmp_path: Path, *names: str) -> bytes:
    return "\n".join(f"A Record{SEP}{tmp_path / n}" for n in names).encode()


def test_rows_whose_files_are_gone_are_stale(tmp_path: Path) -> None:
    fake = FakeRunner(installed={"beet"}).expect(
        "ls", stdout=_rows(tmp_path, "01.flac", "02.flac")
    )
    found = I.Beets().stale(fake, "/music", "A Band", "A Record")
    assert found is not None and found.tracks == 2
    assert "deleted outside beets" in found.note()


def test_rows_are_not_stale_while_any_file_is_there(tmp_path: Path) -> None:
    (tmp_path / "02.flac").write_bytes(b"fLaC")
    fake = FakeRunner(installed={"beet"}).expect(
        "ls", stdout=_rows(tmp_path, "01.flac", "02.flac")
    )
    assert I.Beets().stale(fake, "/music", "A Band", "A Record") is None


def test_clearing_refuses_while_a_file_is_still_there(tmp_path: Path) -> None:
    """Rows only, never files - so it can never unregister an album that is
    actually present."""
    (tmp_path / "01.flac").write_bytes(b"fLaC")
    fake = FakeRunner(installed={"beet"}).expect(
        "ls", stdout=_rows(tmp_path, "01.flac")
    )
    with pytest.raises(I.ImportFailed, match="still there"):
        I.Beets().clear_stale(fake, "/music", "A Band", "A Record")
    assert not [c for c in fake.calls if "remove" in c]


def test_clearing_removes_rows_and_leaves_files_alone(tmp_path: Path) -> None:
    class Once(FakeRunner):
        seen = 0

        def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
            args = [str(a) for a in argv]
            if "ls" in args:
                self.seen += 1
                # The second listing is after the removal, and empty.
                if self.seen > 1:
                    return super().run(["true"], stdin=stdin, timeout=timeout)
            return super().run(argv, stdin=stdin, timeout=timeout)

    fake = Once(installed={"beet"}).expect("ls", stdout=_rows(tmp_path, "01.flac"))
    cleared = I.Beets().clear_stale(fake, "/music", "A Band", "A Record")
    assert cleared.tracks == 1
    removed = [c for c in fake.calls if "remove" in c]
    assert removed and "-d" not in removed[0], "it must never delete files"


def test_clearing_refuses_when_there_is_nothing_stale() -> None:
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=b"")
    with pytest.raises(I.ImportFailed, match="nothing stale"):
        I.Beets().clear_stale(fake, "/music", "A Band", "A Record")


def test_the_tagger_has_no_database_to_go_stale() -> None:
    assert I.Tagger().stale(FakeRunner(), "/music", "A Band", "A Record") is None


def test_a_refused_import_names_stale_rows_as_the_cause(tmp_path: Path) -> None:
    """The failure beets reports does not say why, and this is the usual why."""
    review = Path(a_review(tmp_path))
    gone = tmp_path / "music"
    fake = FakeRunner(installed={"beet", "metaflac"}).expect(
        "ls", stdout=f"A Record{SEP}{gone / '01.flac'}".encode()
    )
    with pytest.raises(I.ImportFailed, match="deleted outside beets"):
        I.Beets().apply(fake, a_plan(), str(review), "/music")
