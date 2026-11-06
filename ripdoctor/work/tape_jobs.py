"""The long work on a tape: looking it up, its cuts and its filing. ADR-063."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ripdoctor.audio import ffprobe, split
from ripdoctor.audio import ingest as G
from ripdoctor.audio.runner import Runner, TimedOut, ToolFailed, ToolMissing
from ripdoctor.core import filing as F
from ripdoctor.core import match as TM
from ripdoctor.core import tape as TP
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.integrations import tagger as TG
from ripdoctor.integrations.importer import Beets, ImportFailed, TapeRun
from ripdoctor.store import incoming as IN
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout
from ripdoctor.store.safety import contains
from ripdoctor.work.jobs import Job, one_import

AUDIO, BOUNDARIES, ALBUMS = "audio", "boundaries", "albums"


def arrived(layout: Layout, tape: TP.Tape) -> IN.Incoming | None:
    """What has arrived for a tape, or why it cannot be taken in yet."""
    came = IN.state(layout, T.key_of(tape.id))
    if not T.audio_file(layout, tape.id).is_file():
        if came is None:
            raise ValueError("this tape has no audio yet - upload it first")
        if came.have != came.total:
            raise ValueError(
                f"only {came.have} of {came.total} bytes arrived - send the rest"
            )
    return came


def _take_in(runner: Runner, layout: Layout, tape_id: str, job: Job, name: str) -> None:
    key = T.key_of(tape_id)
    part, done = IN.part_file(layout, key), IN.done_file(layout, key)
    job.step(name, "reading what arrived")
    try:
        G.probe(runner, str(part))
    except G.NotAudio as e:
        raise ValueError(str(e)) from e
    job.finished = 1
    job.step(name, "normalising")
    G.normalise(runner, str(part), str(done), timeout=T.LONG_RUN)
    job.finished = 2
    job.step(name, "checking it decodes")
    if not split.verify(runner, str(done), timeout=T.LONG_RUN):
        done.unlink(missing_ok=True)
        raise ValueError("the encoded tape would not decode - nothing placed")
    T.take_audio(layout, tape_id)


def find_boundaries(runner: Runner, layout: Layout, tape_id: str, job: Job) -> TP.Tape:
    """Make the audio a tape's, measure it, and lay the list over it. Read again here:
    only now does the job hold the tape against a change. Each stage leaves what the
    next needs, so a restart carries on from wherever it stopped."""
    job.total, job.finished, job.phase = 4, 0, AUDIO
    current = T.read(layout, tape_id)
    if not current.songs:
        raise ValueError("this tape has no song list yet - add one first")
    refused = TP.place_refusal(current)
    if refused:
        raise ValueError(refused)
    came = arrived(layout, current)
    if not T.audio_file(layout, tape_id).is_file() and came is not None:
        _take_in(runner, layout, tape_id, job, came.name)
        current = replace(current, source=came.name)
        T.save(layout, current)
    if came is not None:
        IN.clear(layout, T.key_of(tape_id))
    job.finished = 3

    job.phase = BOUNDARIES
    audio = str(T.audio_file(layout, tape_id))
    job.step(current.name, "measuring")
    seconds = ffprobe.true_duration(runner, audio, timeout=T.LONG_RUN)
    if seconds <= 0:
        raise ValueError("the tape has no playable audio in it")
    lane = T.measure(runner, layout, tape_id, seconds)

    job.step(current.name, "placing boundaries")
    current = TP.place(replace(current, duration=seconds), TP.gaps_of(lane))
    current = replace(current, prepare_failed="")
    T.save(layout, current)
    job.finished = 4
    return current


def _sited(
    layout: Layout, tape_id: str, library: str, rows: tuple[F.Row, ...]
) -> tuple[F.Row, ...]:
    """Whether each row is in the library. Rows carrying a tape's own paths are an
    import that never finished, and nothing has reached the library."""
    ours = T.dir_for(layout, tape_id)
    return tuple(
        replace(r, home=contains(library, r.path) and not contains(ours, r.path))
        for r in rows
    )


@dataclass
class Asking:
    """What a lookup asks. `beets` is None where there is no library to ask."""

    fetcher: MB.Fetcher
    sleep: Callable[[float], None]
    runner: Runner
    layout: Layout
    library: str
    beets: Beets | None
    known: dict[str, tuple[F.Row, ...]] = field(default_factory=dict)
    apart: list[bool | None] = field(default_factory=list)

    def folders(self, beets: Beets) -> bool | None:
        """Once per job. Not knowing changes only the words a choice is put in."""
        if not self.apart:
            try:
                self.apart.append(beets.folders_apart(self.runner))
            except Exception:  # the note says what holds either way
                self.apart.append(None)
        return self.apart[0]

    def by_artist(self, beets: Beets, tape_id: str, artist: str) -> tuple[F.Row, ...]:
        """Once per artist in a job, however many songs are theirs."""
        if artist not in self.known:
            rows = beets.artist_copies(self.runner, self.library, artist)
            self.known[artist] = _sited(self.layout, tape_id, self.library, rows)
        return self.known[artist]

    def by_recording(
        self, beets: Beets, tape_id: str, made: TM.Proposal
    ) -> tuple[F.Row, ...]:
        recording = made.choice.recording if made.choice else ""
        if not recording or made.status != TM.MATCHED:
            return ()
        rows = beets.recording_copies(self.runner, self.library, recording)
        return _sited(self.layout, tape_id, self.library, rows)

    def catalogue(
        self, wanted: TM.Wanted, held: tuple[TM.Held, ...] = ()
    ) -> TM.Proposal:
        try:
            found = MB.find_song(self.fetcher, wanted, sleep=self.sleep)
        except MB.LookupFailed as e:
            return TM.Proposal(TM.FAILED, f"{e} - look again to retry")
        return TM.propose(wanted, found.editions, held=held, loose=found.loose)


def _lookup_one(
    asking: Asking, tape: TP.Tape, song: TP.Song
) -> tuple[TM.Proposal, str]:
    """The library is read first, because an edition it has comes first among releases,
    and then answers the catalogue's answer. Not being able to read it is said, and does
    not stop the lookup: File asks again, and refuses to guess."""
    wanted = TP.wanted(song)
    beets = asking.beets
    if beets is None:
        return asking.catalogue(wanted), ""
    try:
        by_name = asking.by_artist(beets, tape.id, F.lead(song.artist))
    except Exception as e:  # any failure to ask is reported on the song, never a miss
        unread = (
            f"the library could not be read, so this was not checked against it: {e}"
        )
        return replace(asking.catalogue(wanted), library_note=unread), ""
    made = replace(
        asking.catalogue(wanted, F.held_albums(by_name)), apart=asking.folders(beets)
    )
    taken = F.claimed(tape, song)
    try:
        return F.at_lookup(
            song, made, asking.by_recording(beets, tape.id, made), by_name, taken
        )
    except Exception as e:  # as above
        return replace(made, library_note=f"the library could not be read: {e}"), ""


def look_up(asking: Asking, tape_id: str, job: Job, *, again: bool = False) -> TP.Tape:
    """Every song with no answer, or with `again` every one unsettled, saved after each
    so a restart carries on."""
    tape = T.read(asking.layout, tape_id)
    waiting = TP.to_match(tape, again=again)
    job.total, job.finished, job.phase = len(waiting), 0, ALBUMS
    for song in waiting:
        job.step(TP.label(song), "looking it up")
        made, maybe = _lookup_one(asking, tape, song)
        tape = TP.with_proposals(tape, {song.number: made}, {song.number: maybe})
        T.save(asking.layout, tape)
        job.finished += 1
    tape = replace(tape, prepare_failed="")
    T.save(asking.layout, tape)
    return tape


def prepare(asking: Asking, tape_id: str, job: Job) -> dict[str, Any]:
    """Audio and boundaries, then every album, for a tape with its audio and list both
    in. The two have nothing to do with each other, so one failing is said at the end
    and stops neither, and what is already done is not done again: running this after an
    interruption finishes the job."""
    said = {BOUNDARIES: "", ALBUMS: ""}
    if not TP.is_placed(T.read(asking.layout, tape_id)):
        try:
            find_boundaries(asking.runner, asking.layout, tape_id, job)
        except Exception as e:  # kept, and raised below once the albums have been asked
            said[BOUNDARIES] = str(e)
    try:
        look_up(asking, tape_id, job)
    except Exception as e:  # as above, after the boundaries that are already saved
        said[ALBUMS] = str(e)
    job.result = said
    failed = "; ".join(f"{part}: {why}" for part, why in said.items() if why)
    if failed:
        tape = T.read(asking.layout, tape_id)
        T.save(asking.layout, replace(tape, prepare_failed=failed))
        raise ValueError(failed)
    return said


def _cut_one(
    runner: Runner,
    layout: Layout,
    tape: TP.Tape,
    song: TP.Song,
    partial: Path,
    now: float,
) -> TP.CutRecord:
    """A cut that fails leaves whatever was under the real name, and says so. One
    that worked forgets the old record before replacing the file, so a crash at any
    point leaves a song that needs cutting, and never a record of other audio."""
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
    T.save(layout, TP.with_cut(tape, song.number, None))
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
    active = [s for s in tape.songs if not s.skip and not TP.needs_no_cut(s)]
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
            f"{len(failed)} of {len(active)} songs did not cut"
            f"{', and the rest did' if len(failed) < len(active) else ''} - "
            f"{'; '.join(failed)}. Cut again to retry them, or skip them."
        )
    return {"cut": made, "kept": kept, "skipped": job.skipped}


def lacks_cover(runner: Runner, beets: Beets, library: str, path: str) -> bool:
    """Only when both were asked and both said none. A library that cannot answer
    leaves this unsaid, because the art may well be there."""
    try:
        if not Path(path).is_file() or TG.has_art(runner, path):
            return False
        return not beets.art_of(runner, library, path)
    except (ImportFailed, ToolMissing, ToolFailed, TimedOut, OSError):
        return False


def covers_known(
    runner: Runner, beets: Beets, layout: Layout, tape: str, library: str
) -> None:
    """Asked again of each song this tape filed, so one filed before this was asked
    is told too, and one given a cover some other way stops being."""
    current = T.read(layout, tape)
    missing = {
        s.number
        for s in current.songs
        if s.filing == F.FILED and lacks_cover(runner, beets, library, s.path)
    }
    T.save(layout, F.with_cover_known(current, missing))


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
    plugins: tuple[str, ...] | None = None

    def rows(self, query: list[str]) -> tuple[F.Row, ...]:
        return self.beets.library_rows(self.runner, self.library, query)

    def tell(
        self,
        song: TP.Song,
        state: str,
        path: str = "",
        note: str = "",
        *,
        keep_begun: bool = False,
        unconfirmed: str = "",
    ) -> None:
        self.tape = F.recorded(
            self.tape,
            song.number,
            state,
            path,
            note,
            keep_begun=keep_begun,
            unconfirmed=unconfirmed,
        )
        T.save(self.layout, self.tape)
        self.unanswered.discard(song.number)
        self.job.finished += 1

    def sited(self, rows: tuple[F.Row, ...]) -> tuple[F.Row, ...]:
        return _sited(self.layout, self.tape.id, self.library, rows)

    def clear_leftovers(self) -> set[str]:
        """Forget rows beets made for this tape's copies and never moved on, which
        would otherwise read as owned. Their paths, and nothing else, are cleared."""
        filing = T.filing_dir(self.layout, self.tape.id)
        cleared = set()
        for row in self.rows([f"path:{filing}"]):
            if contains(filing, row.path):
                self.beets.forget_row(self.runner, self.library, row.path)
                cleared.add(row.path)
        return cleared

    def taken(self, song: TP.Song) -> set[str]:
        """Rows other songs on this tape have been recorded against."""
        return {
            s.path
            for s in self.tape.songs
            if s.filing in F.FINISHED and s.number != song.number
        }

    def found_again(self, run: F.Run, track: F.Track) -> bool:
        """Whether an attempt that got as far as beets answers for this song. Importing
        again could file it twice, so this comes before the pre-check, whose length
        rule goes by the list and may not recognise what the person accepted."""
        song, taken = track.song, self.taken(track.song)
        rows = self.read_back(run, song)
        paths = F.recovered(song, rows, taken=taken, track=track.number)
        if len(paths) == 1:
            self.complete(song, F.ALREADY, paths[0])
            return True
        if paths:
            self.tell(song, F.UNKNOWN, note=_which(paths))
            return True
        stray = F.astray(song, rows, taken=taken, track=track.number)
        if stray:
            self.tell(
                song, F.FAILED, note=_outside(stray, self.library), keep_begun=True
            )
        return bool(stray)

    def checked(self, run: F.Run) -> list[tuple[F.Track, tuple[F.Row, ...]]]:
        """The songs still to import, with the rows beside them before the import."""
        waiting = []
        for track in run.tracks:
            song = track.song
            self.job.step(TP.label(song), "asking the library")
            if song.import_begun and self.found_again(run, track):
                continue
            by_id, by_name = self.beets.owned(
                self.runner, self.library, F.recording_of(song), F.lead(song.artist)
            )
            said = F.precheck(
                song, self.sited(by_id), self.sited(by_name), self.taken(song)
            )
            if F.found_owned(song) and said.action != F.ALREADY:
                self.tape = F.copy_gone(self.tape, song)
                T.save(self.layout, self.tape)
                self.unanswered.discard(song.number)
                self.job.finished += 1
            elif said.action == F.FILE:
                waiting.append((track, by_name))
            elif said.action == F.ALREADY and song.import_begun:
                self.complete(song, said.action, said.path, said.note)
            else:
                self.tell(
                    song,
                    said.action,
                    said.path,
                    said.note,
                    unconfirmed=said.unconfirmed,
                )
        return waiting

    def read_back(self, run: F.Run, song: TP.Song) -> tuple[F.Row, ...]:
        recording = F.recording_of(song)
        if recording:
            query = [f"mb_trackid:{recording}"]
        elif run.release:
            query = [f"mb_albumid:{run.release}"]
        else:
            query = [F.term("artist", F.lead(song.artist)), F.term("album", song.album)]
        return self.sited(self.rows(query))

    def complete(self, song: TP.Song, state: str, path: str, *notes: str) -> None:
        """A song this tape's own import filed. Beets rewrites what it files at the
        writer's umask, so the modes come back last, before the outcome is recorded.
        An album this tape never touched is not its to change. ADR-056."""
        said = [*notes, *self.repaired(song, path)]
        try:
            TG.set_modes(Path(path).parent, self.file_mode, self.dir_mode)
        except OSError as e:  # said on the song, whose outcome stands
            said.append(f"its modes could not be set: {e}")
        self.tell(song, state, path, "; ".join(n for n in said if n))
        self.covers(song, path)

    def covers(self, song: TP.Song, path: str) -> None:
        if lacks_cover(self.runner, self.beets, self.library, path):
            self.tape = replace(
                self.tape,
                songs=tuple(
                    replace(s, no_cover=True) if s.number == song.number else s
                    for s in self.tape.songs
                ),
            )
            T.save(self.layout, self.tape)

    def repaired(self, song: TP.Song, path: str) -> list[str]:
        """What an import can leave out of a file it filed. Any failure, a timed-out
        ReplayGain as much as a refusal, is a note on the song and not its outcome."""
        said = []
        try:
            if self.plugins is None:
                self.plugins = self.beets.plugins(self.runner)
            if "embedart" in self.plugins:
                said.append(self.with_art(path))
            said.append(self.with_gain(song, path))
        except Exception as e:  # a retry would file it twice to get this back
            said.append(str(e))
        return said

    def with_gain(self, song: TP.Song, path: str) -> str:
        """A from-list song is excused its art and date and not its gain, and with no
        plugin to compute one, that is said against it."""
        if TG.has_gain(self.runner, path):
            return ""
        if "replaygain" not in (self.plugins or ()):
            missing = "no ReplayGain: beets' replaygain plugin is not loaded"
            return "" if F.choice_of(song) else missing
        why = self.beets.replaygain(self.runner, path)
        if TG.has_gain(self.runner, path):
            return ""
        return f"ReplayGain did not reach this file: {why or 'beets gave no reason'}"

    def with_art(self, path: str) -> str:
        """Beets embeds art only as it fetches it, so a song joining an album that
        already has some arrives without."""
        if TG.has_art(self.runner, path):
            return ""
        art = self.beets.art_of(self.runner, self.library, path)
        if not art:
            return ""
        why = self.beets.embed_art(self.runner, path, art)
        if TG.has_art(self.runner, path):
            return ""
        return f"its album's art did not embed: {why or 'beets gave no reason'}"

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

        self.job.step(run.album, "importing with beets")
        with one_import(self.job):
            doubled, how = "", F.BESIDE
            if run.release:
                how = F.joining(
                    run, self.beets.album_rows(self.runner, self.library, run.album)
                )
                doubled = F.beside_note(run) if how == F.BESIDE else ""
            else:
                query = [F.term("albumartist", run.artist), F.term("album", run.album)]
                doubled = F.second_row(run, self.rows(query))
            self.tape = F.begun(self.tape, copies)
            T.save(self.layout, self.tape)
            self.job.step(run.album, "importing with beets")
            took = self.beets.file_tape_run(
                self.runner, str(where), run.release, self.library, how=how
            )

        cleared, unclear = set(), ""
        try:
            cleared = self.clear_leftovers()
        except ImportFailed as e:  # the next attempt clears them; say why not this one
            unclear = str(e)
        for track, before in waiting:
            song = track.song
            try:
                rows = self.read_back(run, song)
            except Exception as e:  # not knowing is kept as that, against this song
                self.tell(song, F.UNKNOWN, note=_unconfirmed(e))
                continue
            had, taken = {r.path for r in before}, self.taken(song)
            home = F.arrivals(song, rows, had, taken=taken, track=track.number)
            stray = F.astray(song, rows, had, taken=taken, track=track.number)
            copy = copies[song.number]
            if len(home) > 1:
                self.tell(song, F.UNKNOWN, note=_which(home))
            elif home:
                self.complete(
                    song, F.FILED, home[0], doubled, _complaint(took), unclear
                )
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
                self.tell(
                    song, F.FAILED, note=_outside(stray, self.library), keep_begun=True
                )
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
            begun = {s.number for s in self.tape.songs if s.import_begun}
            for track in run.tracks:
                number = track.song.number
                if number in self.unanswered and number in begun:
                    self.tell(track.song, F.UNKNOWN, note=_unconfirmed(e))
                elif number in self.unanswered:
                    self.tell(track.song, F.FAILED, note=str(e))


def _unconfirmed(why: Exception) -> str:
    return (
        f"beets may have this and could not be asked: {why} - file again to ask "
        "again, which looks for it before anything is imported"
    )


def _which(paths: list[str]) -> str:
    return (
        f"{len(paths)} rows could be this song and nothing tells them apart: "
        f"{', '.join(paths)} - clear the wrong one with beet remove and file again, "
        "or skip this song"
    )


def _outside(stray: str, library: str) -> str:
    return (
        f"beets filed this at {stray}, outside the library at {library} - "
        "move it in with beet move, then file again"
    )


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

    failed = [TP.label(s) for s in done.songs if F.status(s) in (F.FAILED, F.UNKNOWN)]
    if failed:
        raise ValueError(
            f"{len(failed)} of {len(active)} songs did not file"
            f"{', and the rest did' if len(failed) < len(active) else ''} - "
            f"{', '.join(failed)}. File again to retry them, or skip them."
        )
    return {"outcomes": F.outcomes(done)}
