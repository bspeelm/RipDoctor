"""The long work on a tape: clips, a preview, its cuts and its filing. ADR-063."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ripdoctor.audio import split
from ripdoctor.audio.runner import Runner
from ripdoctor.core import filing as F
from ripdoctor.core import tape as TP
from ripdoctor.integrations import tagger as TG
from ripdoctor.integrations.importer import Beets, ImportFailed, TapeRun
from ripdoctor.store import cache as C
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout
from ripdoctor.store.safety import contains
from ripdoctor.work.jobs import Job


def clips(
    runner: Runner,
    layout: Layout,
    tape: TP.Tape,
    wanted: Sequence[tuple[TP.Song, str]],
    job: Job,
) -> dict[str, Any]:
    source = str(T.audio_file(layout, tape.id))
    partial = T.fresh_partial(layout, tape.id)
    job.total = len(wanted)
    built = 0
    for song, edge in wanted:
        name = TP.clip_name(TP.edge_at(song, edge) or 0.0)
        target = T.clip_file(layout, tape.id, name)
        if not target.is_file():
            job.step(TP.label(song), f"building the {edge} clip")
            part = partial / name
            split.tick_one(
                runner,
                source,
                TP.edge_at(song, edge) or 0.0,
                str(part),
                duration=tape.duration,
            )
            T.promote(part, target)
            built += 1
        job.finished += 1
    return {"clips": built, "asked": len(wanted)}


def preview(runner: Runner, layout: Layout, tape: TP.Tape, job: Job) -> dict[str, Any]:
    target = T.preview_file(layout, tape.id)
    job.total = 1
    if not target.is_file():
        job.step(tape.name, "encoding the preview")
        part = T.fresh_partial(layout, tape.id) / T.PREVIEW
        source = str(T.audio_file(layout, tape.id))
        runner.run(C.preview_argv(source, str(part)), timeout=T.LONG_RUN).require()
        if not part.is_file() or part.stat().st_size == 0:
            raise RuntimeError("the preview encode produced nothing")
        T.promote(part, target)
    job.finished = 1
    return {"preview": True}


def _cut_one(
    runner: Runner,
    layout: Layout,
    tape: TP.Tape,
    song: TP.Song,
    partial: Path,
    now: float,
) -> TP.CutRecord:
    """A cut that fails leaves whatever was under the real name, and says so."""
    name = TP.cut_name(song, len(tape.songs))
    start, end = song.start or 0.0, song.end or 0.0
    part = partial / name
    why = split.cut_verified(
        runner,
        str(T.audio_file(layout, tape.id)),
        start,
        end,
        str(part),
        T.LONG_RUN,
    )
    if why:
        part.unlink(missing_ok=True)
        return TP.CutRecord(TP.CUT_FAILED, name, start, end, why)
    T.take_cut(layout, tape.id, part, name, now)
    return TP.CutRecord(TP.CUT, name, start, end)


def cut(
    runner: Runner, layout: Layout, tape_id: str, job: Job, now: float
) -> dict[str, Any]:
    """Every song not skipped, saved after each so a restart carries on.

    The gate is read again here: only the job holds the tape against a change.
    Any song that failed fails the job, once the rest are done, naming it.
    """
    tape = T.read(layout, tape_id)
    refusal = TP.cut_refusal(tape)
    if refusal:
        raise ValueError(refusal)
    count = len(tape.songs)
    active = [s for s in tape.songs if not s.skip]
    job.skipped = [TP.label(s) for s in tape.songs if s.skip]
    job.total = len(active)

    wanted = {TP.cut_name(s, count) for s in active}
    for stray in sorted(T.cuts_on_disk(layout, tape_id) - wanted):
        T.supersede(layout, tape_id, stray, now)

    partial = T.fresh_partial(layout, tape_id)
    made, kept, failed = [], [], []
    for song in active:
        if TP.is_current(song, count) and (
            T.cut_file(layout, tape_id, TP.cut_name(song, count)).is_file()
        ):
            kept.append(TP.label(song))
            job.finished += 1
            continue
        job.step(TP.label(song), "cutting")
        record = _cut_one(runner, layout, tape, song, partial, now)
        tape = TP.with_cut(tape, song.number, record)
        T.save(layout, tape)
        if record.status == TP.CUT:
            made.append(TP.label(song))
        else:
            failed.append(f"{TP.label(song)}: {record.why}")
        job.finished += 1
    if failed:
        raise ValueError(
            f"{len(failed)} of {len(active)} songs did not cut, and the rest did - "
            f"{'; '.join(failed)}. Cut again to retry them, or skip them."
        )
    return {"cut": made, "kept": kept, "skipped": job.skipped}


@dataclass
class _Filing:
    """One job's filing. Every outcome is saved as it is known, so a restart at any
    point loses nothing, and a retry finds it or asks the library again."""

    runner: Runner
    beets: Beets
    layout: Layout
    tape: TP.Tape
    job: Job
    library: str
    file_mode: int
    dir_mode: int
    unanswered: set[int] = field(default_factory=set)

    def tell(self, song: TP.Song, state: str, path: str = "", note: str = "") -> None:
        self.tape = F.recorded(self.tape, song.number, state, path, note)
        T.save(self.layout, self.tape)
        self.unanswered.discard(song.number)
        self.job.finished += 1

    def sited(self, rows: tuple[F.Row, ...]) -> tuple[F.Row, ...]:
        """Whether each row is in the library. Rows carrying a tape's own paths are
        an import that never finished, and nothing has reached the library."""
        ours = T.dir_for(self.layout, self.tape.id)
        return tuple(
            replace(
                r, home=contains(self.library, r.path) and not contains(ours, r.path)
            )
            for r in rows
        )

    def clear_leftovers(self) -> set[str]:
        """Forget rows beets made for this tape's copies and never moved on, which
        would otherwise read as owned. Their paths, and nothing else, are cleared."""
        filing = T.filing_dir(self.layout, self.tape.id)
        cleared = set()
        query = [f"path:{filing}"]
        for row in self.beets.library_rows(self.runner, self.library, query):
            if contains(filing, row.path):
                self.beets.forget_row(self.runner, self.library, row.path)
                cleared.add(row.path)
        return cleared

    def checked(self, run: F.Run) -> list[tuple[F.Track, tuple[F.Row, ...]]]:
        """The songs still to import, with the rows beside them before the import."""
        waiting = []
        for track in run.tracks:
            song = track.song
            self.job.step(TP.label(song), "asking the library")
            by_id, by_name = self.beets.owned(
                self.runner, self.library, F.recording_of(song), F.lead(song.artist)
            )
            said = F.precheck(song, self.sited(by_id), self.sited(by_name))
            if said.action == F.FILE:
                waiting.append((track, by_name))
            elif said.action == F.ALREADY and song.import_begun:
                self.complete(song, said.action, said.path, said.note)
            else:
                self.tell(song, said.action, said.path, said.note)
        return waiting

    def read_back(self, run: F.Run, song: TP.Song) -> tuple[F.Row, ...]:
        recording = F.recording_of(song)
        if recording:
            query = [f"mb_trackid:{recording}"]
        elif run.release:
            query = [f"mb_albumid:{run.release}"]
        else:
            query = [f"artist:{F.lead(song.artist)}", f"album:{song.album}"]
        return self.sited(self.beets.library_rows(self.runner, self.library, query))

    def complete(self, song: TP.Song, state: str, path: str, *notes: str) -> None:
        """A song the library holds. Beets rewrites what it files at the writer's
        umask, so the modes come back before the outcome is recorded. ADR-056."""
        TG.set_modes(Path(path).parent, self.file_mode, self.dir_mode)
        self.tell(song, state, path, "; ".join(n for n in notes if n))

    def imported(
        self,
        run: F.Run,
        waiting: list[tuple[F.Track, tuple[F.Row, ...]]],
        directory: str,
    ) -> None:
        """Copy, tag, import, and believe only what the library says afterward: a
        song is filed by a row in the library and a file there, whatever beets
        exited with, and any other row it left in this tape's copies is cleared."""
        count = len(self.tape.songs)
        where = T.fresh_filing(self.layout, self.tape.id, directory)
        copies = {}
        for track, _ in waiting:
            song = track.song
            self.job.step(TP.label(song), "tagging a copy")
            copy = T.copy_cut(
                self.layout, self.tape.id, TP.cut_name(song, count), where
            )
            TG.write_tags(self.runner, str(copy), _tags(run, track))
            copies[song.number] = copy

        doubled = ""
        if not run.release:
            doubled = F.second_row(
                run,
                self.beets.library_rows(
                    self.runner,
                    self.library,
                    [f"albumartist:{run.artist}", f"album:{run.album}"],
                ),
            )
        self.tape = F.begun(self.tape, copies)
        T.save(self.layout, self.tape)
        self.job.step(run.album, "importing with beets")
        took = self.beets.file_tape_run(self.runner, str(where), run.release)

        cleared, unclear = set(), ""
        try:
            cleared = self.clear_leftovers()
        except ImportFailed as e:  # the next attempt clears them; say why not this one
            unclear = str(e)
        for track, before in waiting:
            song = track.song
            rows = self.read_back(run, song)
            had = {r.path for r in before}
            path, stray = F.arrived(song, rows, had), F.astray(song, rows, had)
            copy = copies[song.number]
            if path:
                self.complete(song, F.FILED, path, doubled, _complaint(took), unclear)
            elif copy.exists():
                note = f"beets did not take it: {took.why()}"
                if str(copy) in cleared:
                    note += (
                        " - the row it left was cleared and nothing of this song is "
                        "in the library, so put that right and file again"
                    )
                self.tell(
                    song, F.FAILED, note=" - ".join(filter(None, (note, unclear)))
                )
            elif stray:
                note = (
                    f"beets filed this at {stray}, outside the library at "
                    f"{self.library} - move it in with beet move, then file again"
                )
                self.tell(song, F.FAILED, note=note)
            else:
                self.tell(
                    song,
                    F.FAILED,
                    note="beets took the file and lists no row for this song, so it "
                    f"may be filed as another track: {took.why()}",
                )

    def run(self, run: F.Run, directory: str) -> None:
        """One album, whose failure is about that album and stops no other."""
        self.unanswered = {t.song.number for t in run.tracks}
        try:
            self.clear_leftovers()
            waiting = self.checked(run)
            if waiting:
                self.imported(run, waiting, directory)
        except Exception as e:  # kept in the document against each song it stopped
            for track in run.tracks:
                if track.song.number in self.unanswered:
                    self.tell(track.song, F.FAILED, note=str(e))


def _complaint(took: TapeRun) -> str:
    """A song the library holds stays filed whatever beets exited with, since a
    retry would file it twice. What it did not finish, art first, is said instead."""
    if took.returncode == 0:
        return ""
    return (
        f"beets exited {took.returncode} after filing this, so its art or tags may "
        f"be incomplete: {took.why()}"
    )


def _tags(run: F.Run, track: F.Track) -> dict[str, str]:
    """A pinned run needs only what beets matches by, and fetches the rest. With no
    release, this is all the file will ever say. ADR-049."""
    tags = {TG.TAG_TITLE: track.title, TG.TAG_TRACK: str(track.number)}
    if run.release:
        return tags
    return {
        **tags,
        TG.TAG_ARTIST: track.song.artist,
        TG.TAG_ALBUMARTIST: run.artist,
        TG.TAG_ALBUM: run.album,
    }


def file_songs(
    runner: Runner,
    beets: Beets,
    layout: Layout,
    tape_id: str,
    job: Job,
    now: float,
    *,
    library: str,
    file_mode: int,
    dir_mode: int,
) -> dict[str, Any]:
    """Every album still unfinished, each on its own.

    The gate is read again here: only the job holds the tape against a change. Any
    song that failed fails the job, once the rest are done, naming it.
    """
    tape = T.read(layout, tape_id)
    refusal = F.file_refusal(tape, T.cuts_on_disk(layout, tape_id))
    if refusal:
        raise ValueError(refusal)
    active = [s for s in tape.songs if not s.skip]
    job.skipped = [TP.label(s) for s in tape.songs if s.skip]
    job.total = len(active)
    job.finished = sum(s.filing in F.FINISHED for s in active)

    filing = _Filing(runner, beets, layout, tape, job, library, file_mode, dir_mode)
    for index, run in enumerate(F.runs(tape), 1):
        filing.run(run, f"{round(now)}-{index:03d}")
    done = filing.tape
    count = len(done.songs)
    T.clear_copies(
        layout,
        tape_id,
        {TP.cut_name(s, count) for s in done.songs if s.filing in F.FINISHED},
    )

    failed = [TP.label(s) for s in done.songs if F.status(s) == F.FAILED]
    if failed:
        raise ValueError(
            f"{len(failed)} of {len(active)} songs did not file, and the rest did - "
            f"{', '.join(failed)}. File again to retry them, or skip them."
        )
    return {"outcomes": F.outcomes(done)}
