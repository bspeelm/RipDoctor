"""Two ways into a library, behind one interface.

beets is not installed here and never will be for a test. Everything it does
goes through the runner, which is the point of the seam.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core import filing as F
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack
from ripdoctor.integrations import importer as I

SEP = "\x1f"


def copy_row(
    path: object, release: str = "x", album: str = "A Record", who: str = "A Band"
) -> str:
    """One row of every row beets is asked for, by what a selection judges."""
    return SEP.join(("T", who, album, "3:00", str(path), "", "1", release, who))


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
        copy_row(f"/music/A Band/A Record/{i + 1:02d} Track.flac")
        for i in range(listed)
    )
    return FakeRunner(installed={"beet"}, **kw).expect(  # type: ignore[arg-type]
        "ls", stdout=rows.encode()
    )


def test_the_release_is_pinned_rather_than_guessed(tmp_path: Path) -> None:
    """Freshly cut files carry no tags at all, so even the right release scores
    about half and a quiet import skips it without a word."""
    fake = beets_runner()
    I.Beets.with_override(tmp_path).preview(
        fake, a_plan(), str(tmp_path), "/music", mbid="aaa"
    )
    argv = fake.argv_for("import")
    assert "--search-id" in argv and argv[argv.index("--search-id") + 1] == "aaa"


def test_with_no_release_it_imports_as_an_album_rather_than_matching(
    tmp_path: Path,
) -> None:
    fake = beets_runner()
    I.Beets.with_override(tmp_path).preview(fake, a_plan(), str(tmp_path), "/music")
    assert "-A" in fake.argv_for("import")


def test_a_look_runs_against_a_library_it_is_about_to_throw_away(
    tmp_path: Path,
) -> None:
    """beets has no dry run, and no answer on stdin is one either: a match it
    rates strongly is applied without ever prompting, so a look that merely
    closed or fed the input filed the record - moved the files, wrote the tags
    and registered the release, with nobody having agreed to it."""
    beets = I.Beets.with_override(tmp_path)
    fake = FakeRunner(installed={"beet"}).expect("import", stdout=MATCHED.encode())
    beets.propose(fake, "/incoming")

    argv = fake.argv_for("import")
    assert beets.preview_config in argv
    assert beets.config not in argv
    looked = Path(tmp_path / I.PREVIEW_FILE).read_text()
    for refusal in ("move: no", "copy: no", "write: no"):
        assert refusal in looked
    assert str(tmp_path / I.PREVIEW_DB) in looked


def test_a_look_starts_from_an_empty_library_every_time(tmp_path: Path) -> None:
    """Rows kept from the last look would make the next one report the record
    as a duplicate of itself."""
    beets = I.Beets.with_override(tmp_path)
    left = Path(beets.preview_db)
    left.write_bytes(b"old rows")
    fake = FakeRunner(installed={"beet"}).expect("import", stdout=MATCHED.encode())
    beets.propose(fake, "/incoming")

    assert not left.exists()


def test_taking_it_uses_the_real_library_not_the_throwaway(tmp_path: Path) -> None:
    beets = I.Beets.with_override(tmp_path)
    fake = (
        FakeRunner(installed={"beet"})
        .expect(lambda a: a[0] == "beet" and "import" in a, stdout=b"added")
        .expect("ls", stdout=b"")
    )
    beets.take(fake, str(tmp_path), str(tmp_path), mbid="abc", tracks=())

    argv = fake.argv_for("import")
    assert beets.config in argv and beets.preview_config not in argv


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
    text = I.Beets.with_override(tmp_path).preview(
        fake, a_plan(), str(tmp_path), "/music"
    )
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
    where, count = I.Beets().locate(fake, "/music", "A Band", "A Record", mbid="x")
    assert count == 11 and where == Path("/music/A Band/A Record")
    assert not [a for a in fake.argv_for("ls") if a.startswith(("album:", "mb_"))]


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
    rows = "\n".join(copy_row(f"{p}") for p in sorted(placed.glob("*.flac")))
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


def test_the_duplicate_question_is_always_asked_whatever_beets_is_configured_with(
    tmp_path: Path,
) -> None:
    """`duplicate_action: remove` in a person's config answers it without reading
    stdin, and deletes the album that was filed."""
    I.Beets.with_override(tmp_path)
    assert "duplicate_action: ask" in (tmp_path / I.OVERRIDE_FILE).read_text()


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
        copy_row(f"/music/A Band/A Record/{i + 1:02d} Track.flac")
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
    return "\n".join(copy_row(f"{tmp_path / n}") for n in names).encode()


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
                    return super().run(["beet"], stdin=stdin, timeout=timeout)
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
        "ls", stdout=copy_row(f"{gone / '01.flac'}").encode()
    )
    with pytest.raises(I.ImportFailed, match="deleted outside beets"):
        I.Beets().apply(fake, a_plan(), str(review), "/music")


# ------------------------------------------------- answering every question


def test_the_duplicate_question_is_answered_too() -> None:
    """beets asks again when the album is already in the library, and one
    answer leaves that question at end-of-input."""
    assert I.answers("replace", matching=True) == b"A\nK\n"
    assert I.answers("keep", matching=True) == b"A\nK\n"
    assert I.answers("merge", matching=True) == b"A\nM\n"


def test_with_lookup_off_there_is_no_candidate_to_accept() -> None:
    """Sending one anyway lands on the duplicate prompt, whose choices do not
    include it. beets asks again and the next line carries, so it works by
    accident - which is not a thing to rely on."""
    assert I.answers("replace", matching=False) == b"K\n"
    assert I.answers("keep", matching=False) == b"K\n"


def test_an_answer_it_does_not_know_keeps_rather_than_starving() -> None:
    """Whatever arrives, beets must not be left waiting."""
    assert I.answers("nonsense", matching=True) == b"A\nK\n"


def test_the_chosen_answer_reaches_beets(tmp_path: Path) -> None:
    review = Path(a_review(tmp_path))
    fake = moving_beets(review)
    I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="x", duplicates="keep")
    assert fake.stdin_for("import") == b"A\nK\n"


def test_the_no_catalogue_path_sends_only_the_duplicate_answer(
    tmp_path: Path,
) -> None:
    review = Path(a_review(tmp_path))
    fake = moving_beets(review)
    I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="", duplicates="keep")
    assert fake.stdin_for("import") == b"K\n"


def test_a_starved_import_says_that_was_the_cause(tmp_path: Path) -> None:
    """The count left in review is true and is not why it failed. A reader
    should not have to know to look at the bottom of the transcript."""
    review = Path(a_review(tmp_path))
    fake = FakeRunner(installed={"beet"}).expect(
        "import", stderr=b"error: stdin stream ended while input required"
    )
    fake.expect("ls", stdout=b"")
    with pytest.raises(I.ImportFailed, match="asked a question this did not answer"):
        I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="x")


# --------------------------------------------- a copy that was not replaced


def test_more_in_the_library_than_was_cut_is_said_out_loud() -> None:
    """A re-import meant to replace an earlier copy and did not leaves both,
    and every check downstream reads "at least as many" as success. It is
    otherwise found in whatever serves the library, as two of the same album."""
    said = I.too_many(28, 14)
    assert said and "28" in said[0] and "14" in said[0]
    assert "not replaced" in said[0]


def test_the_expected_count_is_not_a_complaint() -> None:
    assert I.too_many(14, 14) == []


def test_fewer_is_somebody_else_s_problem() -> None:
    """Short is the archive gate's question, and it already asks it."""
    assert I.too_many(12, 14) == []


def test_a_doubled_library_is_noted_on_the_import(tmp_path: Path) -> None:
    """Two of everything is what an unreplaced re-import leaves behind."""
    review = Path(a_review(tmp_path))
    cut = sum(len(side.tracks) for side in a_plan().sides)
    rows = "\n".join(
        copy_row(f"/music/A Band/A Record/{i:02d} Track.flac") for i in range(cut * 2)
    )
    fake = Moving(review, installed={"beet", "metaflac"})
    fake.expect("ls", stdout=rows.encode())
    done = I.Beets().apply(fake, a_plan(), str(review), "/music", mbid="x")
    assert any("not replaced" in n for n in done.notes), done.notes


# ------------------------------------- what beets says is relative to its root


def a_filed_album(tmp_path: Path, names: tuple[str, ...]) -> tuple[str, bytes]:
    """A library with real files, and beets' answer about them: relative
    paths, which is what it reports and what a fake that returns absolute
    ones quietly hides."""
    album = tmp_path / "A Band" / "A Record"
    album.mkdir(parents=True)
    for name in names:
        (album / name).write_bytes(b"fLaC")
    said = "\n".join(copy_row(f"A Band/A Record/{name}") for name in names)
    return str(tmp_path), said.encode()


def test_a_relative_path_is_resolved_against_the_library(tmp_path: Path) -> None:
    """Checked from wherever the process happens to be running, a relative
    path is a file that does not exist - so every row reads as missing."""
    library, said = a_filed_album(tmp_path, ("01 One.flac", "02 Two.flac"))
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=said)
    where, count = I.Beets().locate(fake, library, "A Band", "A Record")
    assert count == 2
    assert where is not None and where.is_absolute() and where.is_dir()


def test_files_that_are_there_are_not_stale(tmp_path: Path) -> None:
    """The panel said an album's files were gone while they sat in the
    library, and clearing rows for a record that is present is the one thing
    that must never happen."""
    library, said = a_filed_album(tmp_path, ("01 One.flac",))
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=said)
    assert I.Beets().stale(fake, library, "A Band", "A Record") is None


def test_files_that_are_really_gone_are_still_stale(tmp_path: Path) -> None:
    said = copy_row("A Band/A Record/01 Gone.flac").encode()
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=said)
    found = I.Beets().stale(fake, str(tmp_path), "A Band", "A Record")
    assert found is not None and found.tracks == 1


def test_an_absolute_path_is_left_alone(tmp_path: Path) -> None:
    """Older beets reports absolute paths, and joining a root onto one would
    produce a path under neither."""
    album = tmp_path / "A Band" / "A Record"
    album.mkdir(parents=True)
    (album / "01 One.flac").write_bytes(b"fLaC")
    said = copy_row(f"{album / '01 One.flac'}").encode()
    fake = FakeRunner(installed={"beet"}).expect("ls", stdout=said)
    where, _n = I.Beets().locate(fake, "/somewhere/else", "A Band", "A Record")
    assert where == album


# --------------------------------------------- replacing means replacing


def a_library(tmp_path: Path, *names: str) -> tuple[str, bytes]:
    album = tmp_path / "A Band" / "A Record"
    album.mkdir(parents=True)
    for name in names:
        (album / name).write_bytes(b"fLaC")
    said = "\n".join(copy_row(f"{album / n}") for n in names)
    return str(tmp_path), said.encode()


def replacing(library: str, said: bytes, review: Path) -> FakeRunner:
    fake = Moving(review, installed={"beet", "metaflac"})
    return fake.expect("ls", stdout=said)  # type: ignore[return-value]


def test_replacing_removes_the_filed_audio_not_only_the_rows(
    tmp_path: Path,
) -> None:
    """Dropping only the rows leaves the files, so the new ones cannot take the
    old names and land beside them. Whatever serves the library reads the
    directory, and shows both."""
    library, said = a_library(tmp_path, "01 Old.flac", "02 Old.flac")
    review = Path(a_review(tmp_path))
    fake = replacing(library, said, review)

    done = I.Beets().apply(
        fake, a_plan(), str(review), library, mbid="x", replacing=True
    )
    assert not list((Path(library) / "A Band" / "A Record").glob("*.flac"))
    assert any("removed 2 files" in n for n in done.notes)
    assert [c for c in fake.calls if "remove" in c], "the rows were left behind"


def test_nothing_is_deleted_without_the_acknowledgement(tmp_path: Path) -> None:
    """Replace is the default answer to a question only asked when a copy is
    already filed. On its own it must not delete anything."""
    library, said = a_library(tmp_path, "01 Old.flac")
    review = Path(a_review(tmp_path))
    fake = replacing(library, said, review)

    I.Beets().apply(fake, a_plan(), str(review), library, mbid="x")
    assert (Path(library) / "A Band" / "A Record" / "01 Old.flac").is_file()


def test_a_path_outside_the_library_is_refused(tmp_path: Path) -> None:
    """The paths come from another program and this deletes what they name."""
    outside = tmp_path / "elsewhere" / "kept.flac"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"fLaC")
    library = tmp_path / "music"
    library.mkdir()
    fake = FakeRunner(installed={"beet"}).expect(
        "ls", stdout=copy_row(f"{outside}").encode()
    )
    with pytest.raises(I.ImportFailed, match="outside"):
        I.Beets().replace_filed(fake, str(library), "A Band", "A Record", mbid="x")
    assert outside.is_file(), "it deleted something outside the library"


def test_files_the_library_does_not_know_about_are_named(tmp_path: Path) -> None:
    """What the directory holds and what the database holds are different
    questions, and the second one was the only one being asked."""
    library, said = a_library(tmp_path, "01 One.flac")
    stray = Path(library) / "A Band" / "A Record" / "01 One.1.flac"
    stray.write_bytes(b"fLaC")
    fake = FakeRunner(installed={"beet"}).expect(
        lambda a: "ls" in a and a[-1].startswith("path:"),
        stdout=SEP.join(("A Record", str(stray.with_name("01 One.flac")))).encode(),
    )
    fake.expect("ls", stdout=said)
    spare = I.Beets().unregistered(fake, library, "A Band", "A Record", mbid="x")
    assert [p.name for p in spare] == ["01 One.1.flac"]


# --------------------------------------------- an album that arrives cut


MATCHED = """/incoming (10 items)
Correcting tags from:
    A Band - A Record
To:
    A Band - A Record
URL:
    https://musicbrainz.org/release/1b7c2f4e-7d8a-4e2c-9f11-6a3d5e8c0b92
(Similarity: 97.4%) (Digital Media, 2026, US)
[A]pply, More candidates, Skip, Use as-is?
"""


def test_the_release_beets_settled_on_is_read_out_of_what_it_printed() -> None:
    assert I.proposed(MATCHED) == "1b7c2f4e-7d8a-4e2c-9f11-6a3d5e8c0b92"


def test_several_candidates_are_an_offer_rather_than_a_proposal() -> None:
    """Asked to choose, beets has not chosen, and importing one of them would
    be this project deciding something it said a person would."""
    two = (
        MATCHED
        + "\n2. https://musicbrainz.org/release/"
        + "b" * 8
        + "-1111-2222-3333-444455556666"
    )
    assert I.proposed(two) == ""


def test_no_release_at_all_is_not_a_proposal() -> None:
    assert I.proposed("Skipping album\nNo matching release found") == ""


def test_a_look_reads_the_match_without_pinning_anything(tmp_path: Path) -> None:
    fake = FakeRunner(installed={"beet"}).expect("import", stdout=MATCHED.encode())
    mbid, output = I.Beets.with_override(tmp_path).propose(fake, "/incoming")

    assert mbid == "1b7c2f4e-7d8a-4e2c-9f11-6a3d5e8c0b92"
    assert "Correcting tags" in output
    # Nothing is pinned yet; that is what agreeing to the match is for.
    assert "--search-id" not in fake.argv_for("import")


def test_a_look_with_nowhere_to_throw_away_refuses_rather_than_importing() -> None:
    """The fallback would be the real configuration, and running there files
    the record instead of showing it - the one thing a look must not do."""
    fake = FakeRunner(installed={"beet"}).expect("import", stdout=MATCHED.encode())
    with pytest.raises(I.ImportFailed, match="throw away"):
        I.Beets().propose(fake, "/incoming")
    assert not fake.calls


def test_taking_it_pins_the_release_that_was_shown(tmp_path: Path) -> None:
    fake = (
        FakeRunner(installed={"beet"})
        .expect(lambda argv: argv[0] == "beet" and "import" in argv, stdout=b"added")
        .expect("ls", stdout=b"")
    )
    (tmp_path / "A Band").mkdir()
    I.Beets().take(fake, str(tmp_path), str(tmp_path), mbid="abc", tracks=("01.flac",))

    argv = fake.argv_for("import")
    assert "--search-id" in argv and argv[argv.index("--search-id") + 1] == "abc"
    assert fake.stdin_for("import") == b"A\nK\n"


def test_a_track_left_behind_is_a_failure_rather_than_a_silence(
    tmp_path: Path,
) -> None:
    """beets leaves the files where they were when it declines, so what is
    still waiting is the honest measure of what did not import."""
    (tmp_path / "01.flac").write_bytes(b"a")
    fake = FakeRunner(installed={"beet"}).expect("import", stdout=b"Skipping album")

    with pytest.raises(I.ImportFailed, match="still waiting"):
        I.Beets().take(
            fake, str(tmp_path), str(tmp_path), mbid="abc", tracks=("01.flac",)
        )


# ---------------------------------------------------------------- a tape's album


def _beets() -> I.Beets:
    return I.Beets(config="/state/override.yaml")


def test_a_tape_s_pinned_run_sends_this_argv_and_these_answers() -> None:
    runner = FakeRunner()
    _beets().file_tape_run(runner, "/work/album", "rel-1", how=I.JOIN)
    assert runner.calls == [
        (
            "beet",
            "--config",
            "/state/override.yaml",
            "import",
            "--search-id",
            "rel-1",
            "/work/album",
        )
    ]
    assert runner.fed == [b"A\nM\nA\n"]


def test_a_from_list_run_keeps_both_and_never_merges_what_is_filed() -> None:
    """Sent the pinned answers, its first question took `A` as no answer and `M` as the
    merge, which rematched an album already filed."""
    runner = FakeRunner()
    _beets().file_tape_run(runner, "/work/album")
    assert runner.calls[0][3:] == ("import", "-A", "/work/album")
    assert runner.fed == [b"K\n"]


def test_the_answers_for_a_tape_can_never_say_replace() -> None:
    assert b"R" not in I.FROM_LIST_ANSWERS
    assert not [a for a in I.PINNED_ANSWERS.values() if b"R" in a]


@pytest.mark.parametrize(
    ("how", "fed"),
    [
        (I.NEW_ALBUM, b"A\n"),
        (I.JOIN, b"A\nM\nA\n"),
        (I.BESIDE, b"A\nK\n"),
    ],
)
def test_a_pinned_run_answers_by_what_the_library_holds(how: str, fed: bytes) -> None:
    runner = FakeRunner()
    _beets().file_tape_run(runner, "/work/album", "rel-1", how=how)
    assert runner.fed == [fed]


def test_a_pinned_run_told_nothing_merges_nothing() -> None:
    runner = FakeRunner()
    _beets().file_tape_run(runner, "/work/album", "rel-1")
    assert runner.fed == [b"A\nK\n"]


def test_the_album_rows_are_read_by_album_alone_with_their_release() -> None:
    runner = FakeRunner()
    runner.expect(
        "ls",
        stdout="\x1f".join(
            ("A Song", "A Band", "A Record", "3:20", "/lib/a.flac", "rec", "1", "rel-9")
        ).encode(),
    )
    rows = I.Beets().album_rows(runner, "/lib", "A Record")
    assert [r.release for r in rows] == ["rel-9"]
    assert runner.calls[0][-1] == F.term("album", "A Record")
    assert not [a for a in runner.calls[0] if a.startswith("albumartist")]


def test_the_album_flow_s_answers_and_argv_are_not_the_tape_s(tmp_path: Path) -> None:
    assert I.answers("merge", matching=True) == b"A\nM\n"
    assert I.answers("replace", matching=True) == b"A\nK\n"
    assert I.answers("replace", matching=False) == b"K\n"
    runner = FakeRunner()
    (tmp_path / "A Band").mkdir()
    _beets().apply(runner, a_plan(), str(tmp_path), str(tmp_path), mbid="rel-1")
    assert runner.calls[0] == (
        "beet",
        "--config",
        "/state/override.yaml",
        "import",
        "--search-id",
        "rel-1",
        str(tmp_path),
    )
    assert runner.fed[0] == b"A\nK\n"


def test_a_relative_path_crash_is_reported_with_its_fix_not_as_a_traceback() -> None:
    took = I.TapeRun(
        1,
        "Traceback (most recent call last):\n"
        '  File "beets/library.py", line 1, in move\n'
        "FileNotFoundError: [Errno 2] No such file: 'Music/a.flac'\n",
    )
    said = took.why()
    assert "relative path" in said
    assert "Back up the library database" in said
    assert "Traceback" not in said


def test_a_starved_prompt_says_a_question_went_unanswered() -> None:
    said = I.TapeRun(1, "error: stdin stream ended while input required\n").why()
    assert said.startswith("beets asked a question this did not answer")


def test_a_run_s_reason_is_its_last_line_with_something_on_it() -> None:
    assert I.TapeRun(0, "Tagging:\n  Skipping.\n\n").why() == "Skipping."
    assert I.TapeRun(3, "").why() == "beets exited 3, saying nothing"


def test_library_rows_read_every_field_and_ask_whether_the_file_is_there(
    tmp_path: Path,
) -> None:
    here = tmp_path / "here.flac"
    here.write_bytes(b"x")
    line = "\x1f".join(("One", "A Band", "A Record", "3:20", str(here), "rec-1", "04"))
    gone = "\x1f".join(
        ("Two", "A Band", "A Record", "", str(tmp_path / "gone.flac"), "", "")
    )
    runner = FakeRunner().expect("ls", stdout=f"{line}\n{gone}\n".encode())
    rows = _beets().library_rows(runner, str(tmp_path), ["artist:A Band"])
    assert rows[0] == F.Row(
        "One",
        "A Band",
        "A Record",
        200.0,
        str(here),
        True,
        recording="rec-1",
        track=4,
    )
    assert (rows[1].length, rows[1].present) == (None, False)
    assert runner.calls[0][-1] == "artist:A Band"
    assert runner.calls[0][3:5] == ("ls", "-f")


@pytest.mark.parametrize("mark", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c"])
def test_a_title_holding_a_line_separator_is_one_row_and_not_a_fragment(
    tmp_path: Path, mark: str
) -> None:
    title = f"One{mark}Two"
    line = "\x1f".join((title, "A Band", "A Record", "3:20", "a.flac", "", ""))
    runner = FakeRunner().expect("ls", stdout=line.encode())
    rows = _beets().library_rows(runner, str(tmp_path), ["artist:A Band"])
    assert [r.title for r in rows] == [title]
    runner = FakeRunner().expect("ls", stdout=f"{line}\x1frel".encode())
    rows = _beets().held_rows(runner, str(tmp_path), ("artist:A Band",))
    assert [(r.title, r.release) for r in rows] == [(title, "rel")]


def test_a_path_ending_in_a_comma_is_never_sent_since_beets_would_read_everything() -> (
    None
):
    runner = FakeRunner()
    beets = _beets()
    for ask in (
        lambda: beets.forget_row(runner, "/lib", "/lib/odd,"),
        lambda: beets.art_of(runner, "/lib", "/lib/odd,"),
        lambda: beets.embed_art(runner, "/lib/odd,", "/lib/a.jpg"),
        lambda: beets.replaygain(runner, "/lib/odd,"),
    ):
        with pytest.raises(I.ImportFailed, match="ends in ,"):
            ask()
    assert runner.calls == []


def test_a_path_holding_a_comma_or_anything_else_is_sent_whole() -> None:
    runner = FakeRunner()
    path = "/lib/a, b $c 'd\" é.flac"
    _beets().forget_row(runner, "/lib", path)
    assert runner.calls[0][-1] == f"path:{path}"


def test_a_relative_row_is_rooted_in_the_library(tmp_path: Path) -> None:
    (tmp_path / "a.flac").write_bytes(b"x")
    line = "\x1f".join(("One", "A Band", "A Record", "3:20", "a.flac", "", ""))
    runner = FakeRunner().expect("ls", stdout=line.encode())
    rows = _beets().library_rows(runner, str(tmp_path), [])
    assert (rows[0].path, rows[0].present) == (str(tmp_path / "a.flac"), True)


def test_a_library_that_could_not_be_asked_is_never_read_as_a_miss() -> None:
    runner = FakeRunner().expect("ls", returncode=1, stderr=b"database is locked\n")
    with pytest.raises(I.ImportFailed, match="database is locked"):
        _beets().library_rows(runner, "/lib", ["mb_trackid:rec-1"])


def test_owned_asks_by_recording_and_by_artist_or_only_by_artist_with_no_id() -> None:
    runner = FakeRunner()
    _beets().owned(runner, "/lib", "rec-1", "A Band")
    assert [c[-1] for c in runner.calls] == [
        "mb_trackid:rec-1",
        F.term("artist", "A Band"),
    ]
    runner = FakeRunner()
    _beets().owned(runner, "/lib", "", "A Band")
    assert [c[-1] for c in runner.calls] == [F.term("artist", "A Band")]


@pytest.mark.parametrize(
    ("art", "sent"),
    [
        ("/lib/Ca$$h/$title/cov$album.jpg", "/lib/Ca$$$$h/$$title/cov$$album.jpg"),
        ("/lib/50% Off/%upper{x}.jpg", "/lib/50$% Off/$%upper{x}.jpg"),
        ("/lib/plain/cover.jpg", "/lib/plain/cover.jpg"),
    ],
)
def test_a_dollar_or_percent_in_a_cover_s_path_was_read_as_a_template(
    art: str, sent: str
) -> None:
    runner = FakeRunner()
    _beets().set_art(runner, "7", art)
    assert runner.calls[-1][-1] == f"artpath={sent}"


@pytest.mark.parametrize(
    ("said", "apart"),
    [
        ("paths:\n    default: $albumartist/$album%aunique{}/$track\n", True),
        (
            "paths:\n    default: $albumartist/$album/$track\n    comp: %aunique\n",
            False,
        ),
        ("directory: /x\n", None),
        ("paths:\n    default: $albumartist/$year - $album/$track\n", None),
        ("paths:\n    default: $albumartist/$album/Disc $disc/$track\n", None),
        ("paths:\n    default: %the{$albumartist}/$album/$track\n", None),
        ("paths:\n    default: $album/$track\n", None),
        (
            "paths:\n    default: '$albumartist/$album/$track'\n"
            "    albumtype:soundtrack: Soundtracks/$album/$track\n",
            None,
        ),
        (
            "paths:\n    default: '$albumartist/$album/$track $title'\n"
            "    singleton: Non-Album/$artist/$title\n"
            "    comp: Compilations/$album%aunique{}/$track $title\n",
            False,
        ),
    ],
)
def test_whether_album_rows_get_folders_of_their_own_is_read_from_the_default_path(
    said: str, apart: bool | None
) -> None:
    runner = FakeRunner().expect("config", stdout=said.encode())
    assert _beets().folders_apart(runner) is apart


# ------------------------------------------------- a library gone from under its mount


def rows_at(library: Path, *names: str) -> bytes:
    """What `beet ls` says of tracks it holds under this root."""
    return "\n".join(
        SEP.join(
            ("A Song", "A Band", "A Record", "3:00", str(library / n), "", "1", "")
        )
        for n in names
    ).encode()


def test_a_record_is_refused_where_the_library_is_empty_and_beets_says_otherwise(
    tmp_path: Path,
) -> None:
    """The mount had dropped: it filed onto the mount point, and replacing first asked
    beets to forget every row of the album it could not see."""
    library = tmp_path / "lib"
    library.mkdir()
    review = Path(a_review(tmp_path))
    fake = FakeRunner().expect("ls", stdout=rows_at(library, "A Band/One.flac"))
    with pytest.raises(I.ImportFailed, match="off its mount"):
        I.Beets().apply(
            fake, a_plan(), str(review), str(library), mbid="x", replacing=True
        )
    assert not [c for c in fake.calls if "import" in c or "remove" in c]
    assert list(review.glob("*.flac"))


def test_an_album_that_arrived_cut_is_refused_the_same_way(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    fake = FakeRunner().expect("ls", stdout=rows_at(library, "A Band/One.flac"))
    with pytest.raises(I.ImportFailed, match="off its mount"):
        I.Beets().take(fake, str(tmp_path), str(library), mbid="x", tracks=("a.flac",))
    assert not [c for c in fake.calls if "import" in c]


def test_a_library_with_nothing_in_it_and_no_rows_is_new_and_not_gone(
    tmp_path: Path,
) -> None:
    fake = FakeRunner()
    assert I.Beets().off_its_mount(fake, str(tmp_path / "lib")) == ""
    assert not [c for c in fake.calls if "import" in c]


def test_rows_under_some_other_root_say_nothing_about_this_one(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    fake = FakeRunner().expect("ls", stdout=rows_at(tmp_path / "old", "One.flac"))
    assert I.Beets().off_its_mount(fake, str(library)) == ""


def test_a_library_with_anything_in_it_never_has_beets_list_every_row(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "A Band").mkdir()
    fake = FakeRunner()
    assert I.Beets().off_its_mount(fake, str(tmp_path / "lib")) == ""
    assert not fake.calls


def test_an_empty_library_beets_cannot_answer_for_is_not_called_fresh(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib").mkdir()
    fake = FakeRunner().expect("ls", returncode=1, stderr=b"database is locked")
    with pytest.raises(I.ImportFailed, match="locked"):
        I.Beets().off_its_mount(fake, str(tmp_path / "lib"))


# ------------------------------------------------------------ joining a release's rows


def test_rows_of_one_release_in_different_folders_stay_as_they_are() -> None:
    fake = (
        FakeRunner()
        .expect(lambda a: "-a" in a and "ls" in a, stdout=b"4\n9\n")
        .expect("album_id:4", stdout=b"/lib/one/1.flac\n")
        .expect("album_id:9", stdout=b"/lib/two/2.flac\n")
    )
    assert I.Beets().join_release(fake, "/lib", "rel") == ("4", 0, 1)
    assert not [c for c in fake.calls if "modify" in c or "remove" in c]


def test_a_row_left_empty_by_a_kill_is_cleared_and_counted_as_nothing() -> None:
    fake = (
        FakeRunner()
        .expect(lambda a: "-a" in a and "ls" in a, stdout=b"4\n9\n")
        .expect("album_id:4", stdout=b"/lib/one/1.flac\n")
    )
    assert I.Beets().join_release(fake, "/lib", "rel") == ("4", 0, 0)
    assert fake.calls[-1][1:] == ("remove", "-a", "-f", "id:9")
    assert not [c for c in fake.calls if "modify" in c]


def test_one_row_of_a_release_has_nothing_to_join() -> None:
    fake = FakeRunner().expect("ls", stdout=b"4\n")
    assert I.Beets().join_release(fake, "/lib", "rel") == ("4", 0, 0)
    assert len(fake.calls) == 1


def test_gaining_an_album_names_that_row_alone() -> None:
    fake = FakeRunner()
    assert I.Beets().album_gain(fake, "4") == ""
    assert fake.calls == [("beet", "replaygain", "-a", "-f", "id:4")]


def test_the_override_that_fetches_no_art_changes_nothing_else(tmp_path: Path) -> None:
    beets = I.Beets.with_override(tmp_path)
    said = Path(beets.no_art).read_text()
    assert said == I.OVERRIDE + "fetchart:\n  auto: no\n"


# --------------------------------------------------- what Merge is allowed to do


def merge_row(release: str, album: str = "A Record", title: str = "One") -> str:
    path = f"/music/A Band/{album}/{title}.flac"
    return copy_row(path, release, album).replace("T", title, 1)


def answers_merge_questions(fake: FakeRunner, own: list[str], named: list[str]) -> None:
    """By album name, which holds one field fewer, and then every row."""
    fake.expect(
        lambda a: "ls" in a and "album::" in " ".join(a),
        stdout="\n".join(r.rpartition(SEP)[0] for r in named).encode(),
    )
    fake.expect("ls", stdout="\n".join({*own, *named}).encode())


def merging(review: Path, own: list[str], named: list[str]) -> FakeRunner:
    """A library holding `own` by the release asked about, and `named` by album name."""
    fake = Moving(review, installed={"beet", "metaflac"})
    answers_merge_questions(fake, own, named)
    return fake  # type: ignore[return-value]


def fed_to_the_import(fake: FakeRunner) -> bytes | None:
    return next(f for c, f in zip(fake.calls, fake.fed, strict=True) if "import" in c)


def test_merge_with_no_release_once_kept_both_and_called_it_merged(
    tmp_path: Path,
) -> None:
    """With lookup off beets has no step that acts on the answer, so `M` made another
    album row, and with `%aunique` another folder."""
    review = Path(a_review(tmp_path))
    fake = Moving(review, installed={"beet", "metaflac"})
    with pytest.raises(I.ImportFailed, match="needs a release"):
        I.Beets().apply(fake, a_plan(), str(review), str(tmp_path), duplicates="merge")
    assert not [c for c in fake.calls if "import" in c]
    assert list(review.glob("*.flac"))


def test_merge_once_stopped_at_the_second_apply_and_now_answers_it(
    tmp_path: Path,
) -> None:
    """Merging matches afresh, so `A`, `M` ran out of input and nothing was filed."""
    (tmp_path / "A Band").mkdir()
    review = Path(a_review(tmp_path))
    held = [merge_row("rel")]
    fake = merging(review, held, held)
    done = I.Beets().apply(
        fake, a_plan(), str(review), str(tmp_path), mbid="rel", duplicates="merge"
    )
    assert fed_to_the_import(fake) == b"A\nM\nA\n"
    assert any("matched" in n and "again" in n for n in done.notes)
    assert not any("earlier copy" in n for n in done.notes)


def test_merge_never_runs_where_another_edition_would_be_rewritten(
    tmp_path: Path,
) -> None:
    (tmp_path / "A Band").mkdir()
    review = Path(a_review(tmp_path))
    fake = merging(review, [], [merge_row("rel-other")])
    with pytest.raises(I.ImportFailed, match="Keep both"):
        I.Beets().apply(
            fake, a_plan(), str(review), str(tmp_path), mbid="rel", duplicates="merge"
        )
    assert not [c for c in fake.calls if "import" in c]


def test_merge_with_nothing_held_declines_an_edition_only_beets_can_see(
    tmp_path: Path,
) -> None:
    """A release can be titled otherwise than the plan, so a question nobody foresaw is
    answered with Skip, which leaves every file as it is, and never with Merge."""
    (tmp_path / "A Band").mkdir()
    review = Path(a_review(tmp_path))
    fake = merging(review, [], [])
    I.Beets().apply(
        fake, a_plan(), str(review), str(tmp_path), mbid="rel", duplicates="merge"
    )
    assert fed_to_the_import(fake) == b"A\nS\n"


def test_merge_beside_another_edition_joins_its_row_and_names_no_other(
    tmp_path: Path,
) -> None:
    (tmp_path / "A Band").mkdir()
    review = Path(a_review(tmp_path))
    held = [merge_row("rel")]
    fake = merging(review, held, [*held, merge_row("rel-other", title="Two")])
    fake.replies[:0] = (
        FakeRunner()
        .expect(lambda a: "-a" in a and "ls" in a, stdout=b"2\n3\n")
        .expect("album_id:2", stdout=b"/music/A Band/A Record/One.flac\n")
        .expect("album_id:3", stdout=b"")
        .replies
    )
    beets = I.Beets.with_override(tmp_path / "state")
    done = beets.apply(
        fake, a_plan(), str(review), str(tmp_path), mbid="rel", duplicates="merge"
    )
    assert fed_to_the_import(fake) == b"A\nK\n"
    assert fake.argv_for("import")[2] == beets.no_art
    assert any("the row already there" in n for n in done.notes)


def test_an_album_that_arrived_cut_merges_by_the_same_rule(tmp_path: Path) -> None:
    (tmp_path / "A Band").mkdir()
    held = [merge_row("rel")]
    fake = FakeRunner(installed={"beet"})
    answers_merge_questions(fake, held, held)
    I.Beets().take(
        fake, str(tmp_path), str(tmp_path), mbid="rel", tracks=(), duplicates="merge"
    )
    assert fed_to_the_import(fake) == b"A\nM\nA\n"


def test_another_edition_in_the_folder_was_once_called_not_in_the_library(
    tmp_path: Path,
) -> None:
    album = tmp_path / "A Band" / "A Record"
    album.mkdir(parents=True)
    (album / "01 Mine.flac").write_bytes(b"fLaC")
    (album / "01 Other.flac").write_bytes(b"fLaC")
    (album / "02 Stray.flac").write_bytes(b"fLaC")
    fake = FakeRunner().expect(
        lambda a: "ls" in a and a[-1] == f"path:{album}",
        stdout="\n".join(
            f"A Record{SEP}{album / n}" for n in ("01 Mine.flac", "01 Other.flac")
        ).encode(),
    )
    fake.expect("ls", stdout=copy_row(album / "01 Mine.flac", "rel").encode())
    spare = I.Beets().unregistered(
        fake, str(tmp_path), "A Band", "A Record", mbid="rel"
    )
    assert [p.name for p in spare] == ["02 Stray.flac"]


# ----------------------------------------------------- the folders a replace empties


def replaced(tmp_path: Path, *also: str) -> tuple[Path, I.Outcome]:
    """The old copy replaced by one beets files somewhere else. `also` is other files
    already under the library, by their place in it."""
    library, said = a_library(tmp_path / "lib", "01 Old.flac", "02 Old.flac")
    for name in also:
        where = Path(library) / name
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_bytes(b"x")
    review = Path(a_review(tmp_path))
    done = I.Beets().apply(
        replacing(library, said, review),
        a_plan(),
        str(review),
        library,
        mbid="x",
        replacing=True,
    )
    return Path(library), done


def test_replacing_once_left_the_folders_it_had_emptied(tmp_path: Path) -> None:
    library, done = replaced(tmp_path)
    assert not (library / "A Band").exists()
    assert library.is_dir()
    assert any("removed 2 empty folders" in n for n in done.notes)


def test_a_folder_holding_anything_else_stays(tmp_path: Path) -> None:
    library, _ = replaced(tmp_path, "A Band/A Record/notes.txt")
    assert (library / "A Band" / "A Record" / "notes.txt").is_file()
    assert not list((library / "A Band" / "A Record").glob("*.flac"))


def test_an_artist_with_another_album_keeps_its_folder(tmp_path: Path) -> None:
    library, _ = replaced(tmp_path, "A Band/Another/01.flac")
    assert not (library / "A Band" / "A Record").exists()
    assert (library / "A Band" / "Another" / "01.flac").is_file()


def test_a_folder_it_never_emptied_is_not_touched_even_if_empty(tmp_path: Path) -> None:
    library, said = a_library(tmp_path / "lib", "01 Old.flac")
    (library_path := Path(library)).joinpath("Empty Already").mkdir()
    review = Path(a_review(tmp_path))
    I.Beets().apply(
        replacing(library, said, review),
        a_plan(),
        str(review),
        library,
        mbid="x",
        replacing=True,
    )
    assert (library_path / "Empty Already").is_dir()


def test_the_library_root_is_never_removed_with_its_last_folder(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    (library / "01 Old.flac").write_bytes(b"fLaC")
    said = copy_row(f"{library / '01 Old.flac'}").encode()
    review = Path(a_review(tmp_path))
    I.Beets().apply(
        replacing(str(library), said, review),
        a_plan(),
        str(review),
        str(library),
        mbid="x",
        replacing=True,
    )
    assert library.is_dir()


def test_a_folder_the_new_copy_landed_back_in_stays(tmp_path: Path) -> None:
    class Landing(Moving):
        def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
            if "import" in [str(a) for a in argv]:
                (album / "01 New.flac").write_bytes(b"fLaC")
            return super().run(argv, stdin=stdin, timeout=timeout)

    library, said = a_library(tmp_path / "lib", "01 Old.flac")
    album = Path(library) / "A Band" / "A Record"
    review = Path(a_review(tmp_path))
    fake = Landing(review, installed={"beet", "metaflac"}).expect("ls", stdout=said)
    I.Beets().apply(fake, a_plan(), str(review), library, mbid="x", replacing=True)
    assert (album / "01 New.flac").is_file()


# ------------------------------------------------- a replace removes what it names


def listing(*rows: tuple[str, str, Path, str]) -> bytes:
    """Every row beets holds: album artist, album, file, release."""
    return "\n".join(
        SEP.join(("T", who, what, "3:00", str(path), "", "1", release, who))
        for who, what, path, release in rows
    ).encode()


def on_disk(library: Path, who: str, what: str, name: str = "01.flac") -> Path:
    where = library / who / what.replace(":", "_")
    where.mkdir(parents=True, exist_ok=True)
    (where / name).write_bytes(b"fLaC")
    return where / name


def named_plan(who: str = "Band A", what: str = "Blue") -> Plan:
    return Plan(
        slug="r",
        album=what,
        artist=who,
        date="1999",
        sides=(
            PlanSide(
                file="a.flac",
                tracks=(PlanTrack(number=1, title="T", start=0.0, end=9.0, cat=9.0),),
            ),
        ),
    )


def a_shelf(library: Path) -> dict[str, Path]:
    return {
        "a": on_disk(library, "Band A", "Blue"),
        "b": on_disk(library, "Band B", "Blue"),
        "c": on_disk(library, "Band C", "Blue Skies"),
    }


def shelf_rows(shelf: dict[str, Path]) -> list[tuple[str, str, Path, str]]:
    return [
        ("Band A", "Blue", shelf["a"], ""),
        ("Band B", "Blue", shelf["b"], ""),
        ("Band C", "Blue Skies", shelf["c"], ""),
    ]


def replace(
    tmp_path: Path, plan: Plan, library: Path, rows: bytes, *, mbid: str = ""
) -> tuple[Moving, I.Outcome | I.ImportFailed]:
    review = tmp_path / f"rev-{len(list(tmp_path.iterdir()))}"
    review.mkdir()
    (review / "01 T.flac").write_bytes(b"fLaC")
    fake = Moving(review, installed={"beet", "metaflac"})
    fake.expect("ls", stdout=rows)
    try:
        done: I.Outcome | I.ImportFailed = I.Beets().apply(
            fake, plan, str(review), str(library), mbid=mbid, replacing=True
        )
    except I.ImportFailed as e:
        done = e
    return fake, done


def test_a_replace_once_deleted_every_album_whose_title_held_this_one(
    tmp_path: Path,
) -> None:
    """`album:Blue` is a substring over every artist: Band B and Band C went with it."""
    library = tmp_path / "lib"
    shelf = a_shelf(library)
    fake, done = replace(tmp_path, named_plan(), library, listing(*shelf_rows(shelf)))
    assert isinstance(done, I.Outcome), done
    assert not shelf["a"].exists()
    assert shelf["b"].is_file() and shelf["c"].is_file()
    removes = [c for c in fake.calls if "remove" in c]
    assert [c[-1] for c in removes] == [f"path:{shelf['a']}"]
    assert not [a for c in fake.calls for a in c if a.startswith("album:")]


def test_a_replace_of_nothing_exact_is_refused_with_nothing_touched(
    tmp_path: Path,
) -> None:
    """The artist has nothing filed, and the old query took the other Blues anyway."""
    library = tmp_path / "lib"
    shelf = a_shelf(library)
    fake, done = replace(
        tmp_path, named_plan("Band Z"), library, listing(*shelf_rows(shelf))
    )
    assert isinstance(done, I.ImportFailed)
    assert "nothing that is exactly Band Z - Blue" in str(done)
    assert all(p.is_file() for p in shelf.values())
    assert not [c for c in fake.calls if "remove" in c or "import" in c]


def test_a_replace_of_two_releases_under_one_name_is_refused(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    one = on_disk(library, "Band A", "Blue", "01.flac")
    two = on_disk(library, "Band A", "Blue", "02.flac")
    rows = listing(("Band A", "Blue", one, "rel-1"), ("Band A", "Blue", two, "rel-2"))
    fake, done = replace(tmp_path, named_plan(), library, rows)
    assert isinstance(done, I.ImportFailed) and "2 different releases" in str(done)
    assert one.is_file() and two.is_file()
    assert not [c for c in fake.calls if "remove" in c or "import" in c]


def test_a_difference_of_case_is_the_album_and_of_accent_is_another(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    accented = on_disk(library, "Band D", "écho")
    plain = on_disk(library, "Band D", "echo")
    rows = listing(("Band D", "écho", accented, ""), ("Band D", "echo", plain, ""))
    _, done = replace(tmp_path, named_plan("BAND D", "ÉCHO"), library, rows)
    assert isinstance(done, I.Outcome), done
    assert not accented.exists() and plain.is_file()


def test_a_title_with_query_characters_is_only_itself(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    mine = on_disk(library, "Band E", "A: Part$ 1")
    other = on_disk(library, "Band E", "Part")
    rows = listing(("Band E", "A: Part$ 1", mine, ""), ("Band E", "Part", other, ""))
    _, done = replace(tmp_path, named_plan("Band E", "A: Part$ 1"), library, rows)
    assert isinstance(done, I.Outcome), done
    assert not mine.exists() and other.is_file()


def test_part_of_a_release_id_once_deleted_an_album_it_was_part_of(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    other = on_disk(library, "Band Q", "Quiet")
    rows = listing(("Band Q", "Quiet", other, "e301be01"))
    fake, done = replace(tmp_path, named_plan(), library, rows, mbid="e")
    assert isinstance(done, I.ImportFailed)
    assert other.is_file()
    assert not [c for c in fake.calls if "remove" in c or "import" in c]


def test_a_pinned_replace_takes_every_row_of_that_id_and_no_other_edition(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    held = on_disk(library, "Band A", "Blue", "01 Mine.flac")
    apart = on_disk(library, "Band A", "Blue [2]", "02 Mine.flac")
    other = on_disk(library, "Band A", "Blue", "01 Other.flac")
    rows = listing(
        ("Band A", "Blue", held, "rel"),
        ("Band A", "Blue", apart, "rel"),
        ("Band A", "Blue", other, "rel-other"),
    )
    _, done = replace(tmp_path, named_plan(), library, rows, mbid="rel")
    assert isinstance(done, I.Outcome), done
    assert not held.exists() and not apart.exists() and other.is_file()
    assert not (library / "Band A" / "Blue [2]").exists()


def test_beets_own_replace_answer_is_never_sent_since_it_deleted_another_edition(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    other = on_disk(library, "Band A", "Blue", "01 Other.flac")
    mine = on_disk(library, "Band A", "Blue", "01 Mine.flac")
    rows = listing(
        ("Band A", "Blue", other, "rel-other"), ("Band A", "Blue", mine, "rel")
    )
    fake, _ = replace(tmp_path, named_plan(), library, rows, mbid="rel")
    assert b"R" not in next(
        f for c, f in zip(fake.calls, fake.fed, strict=True) if "import" in c
    )
    assert I.answers("replace", matching=True) == b"A\nK\n"
    assert I.answers("nonsense", matching=False) == b"K\n"


def test_an_album_that_arrived_cut_replaces_its_release_and_no_other_edition(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    mine = on_disk(library, "Band A", "Blue", "01 Mine.flac")
    other = on_disk(library, "Band A", "Blue", "01 Other.flac")
    fake = FakeRunner(installed={"beet"}).expect(
        "ls",
        stdout=listing(
            ("Band A", "Blue", mine, "rel"), ("Band A", "Blue", other, "rel-other")
        ),
    )
    done = I.Beets().take(fake, str(tmp_path), str(library), mbid="rel", tracks=())
    assert not mine.exists() and other.is_file()
    assert any("removed 1 files" in n for n in done.notes)
    assert b"R" not in next(
        f for c, f in zip(fake.calls, fake.fed, strict=True) if "import" in c
    )


def test_an_album_that_arrived_cut_with_none_of_its_release_there_is_not_refused(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    other = on_disk(library, "Band A", "Blue")
    fake = FakeRunner(installed={"beet"}).expect(
        "ls", stdout=listing(("Band A", "Blue", other, "rel-other"))
    )
    I.Beets().take(fake, str(tmp_path), str(library), mbid="rel", tracks=())
    assert other.is_file()
    assert not [c for c in fake.calls if "remove" in c]
    assert [c for c in fake.calls if "import" in c]


def test_locate_once_named_another_artists_album_for_this_one(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    shelf = a_shelf(library)
    fake = FakeRunner().expect("ls", stdout=listing(*shelf_rows(shelf)))
    beets = I.Beets()
    assert beets.locate(fake, str(library), "Band B", "Blue") == (shelf["b"].parent, 1)
    assert beets.locate(fake, str(library), "Band Z", "Blue") == (None, 0)
    assert beets.locate(fake, str(library), "Band C", "Blue") == (None, 0)


def test_what_is_shown_is_what_a_replace_removes(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    shelf = a_shelf(library)
    rows = listing(*shelf_rows(shelf))
    fake = FakeRunner().expect("ls", stdout=rows)
    shown = I.Beets().filed(fake, str(library), "Band A", "Blue")
    assert (shown.tracks, shown.where, shown.refused) == (1, shelf["a"].parent, "")
    _, done = replace(tmp_path, named_plan(), library, rows)
    assert isinstance(done, I.Outcome)
    assert any(f"removed {shown.tracks} files" in n for n in done.notes)


def test_rows_are_cleared_as_stale_only_for_this_album(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    gone = library / "Band A" / "Blue" / "01.flac"
    also = library / "Band B" / "Blue" / "01.flac"
    fake = FakeRunner().expect(
        "ls", stdout=listing(("Band A", "Blue", gone, ""), ("Band B", "Blue", also, ""))
    )
    beets = I.Beets()
    found = beets.stale(fake, str(library), "Band A", "Blue")
    assert found is not None and found.tracks == 1
    with pytest.raises(I.ImportFailed, match="still registered"):
        beets.clear_stale(fake, str(library), "Band A", "Blue")
    assert [c[-1] for c in fake.calls if "remove" in c] == [f"path:{gone}"]


def test_a_row_outside_the_library_stops_a_replace_before_any_file_goes(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    inside = on_disk(library, "Band A", "Blue")
    outside = on_disk(tmp_path / "elsewhere", "Band A", "Blue")
    rows = listing(("Band A", "Blue", inside, ""), ("Band A", "Blue", outside, ""))
    fake, done = replace(tmp_path, named_plan(), library, rows)
    assert isinstance(done, I.ImportFailed) and "outside" in str(done)
    assert inside.is_file() and outside.is_file()
    assert not [c for c in fake.calls if "remove" in c]


def test_nothing_to_forget_never_reaches_beets_as_forgetting_everything() -> None:
    """`beet remove -f` with no query is the whole library."""
    fake = FakeRunner()
    I.Beets()._forget_rows(fake, [])
    assert not fake.calls
