"""A tape's playlist, after filing and after any import, and putting it away.
ADR-063."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ripdoctor.audio.runner import Runner
from ripdoctor.core import playlist as P
from ripdoctor.core import tape as TP
from ripdoctor.integrations.importer import Beets
from ripdoctor.store import archive as AR
from ripdoctor.store import playlists as S
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout
from ripdoctor.store.safety import contains
from ripdoctor.work.jobs import Busy, Job, Jobs


@dataclass(frozen=True, slots=True)
class Place:
    runner: Runner
    layout: Layout
    library: str
    file_mode: int
    dir_mode: int


def _inside(root: Path, entry: P.Entry) -> P.Entry:
    """Through symlinks, which core cannot see. Out of the library is about this one
    song, so it is named like any other that has no line."""
    if entry.state != P.HERE:
        return entry
    real = Path(entry.path).resolve()
    if contains(root, real):
        return replace(entry, path=str(real))
    why = f"{entry.path} is outside the library at {root}"
    return P.Entry(entry.number, state=P.OUTSIDE, why=why)


def _write(
    place: Place, tape: TP.Tape, entries: tuple[P.Entry, ...], now: float
) -> P.State:
    """Write what resolves. With nothing resolving the file is left alone: that is
    what a library gone from under its mount looks like, and emptying a playlist
    on the music server is no way to report it."""
    prior = S.read(place.layout, tape.id)
    root = Path(place.library).resolve()
    state = replace(prior, at=now, note="")
    try:
        done = P.render(tape.name, [_inside(root, e) for e in entries], str(root))
        state = replace(state, left_out=done.left_out)
        if done.songs:
            file = prior.file or P.file_of(tape)
            modes = place.file_mode, place.dir_mode
            changed = S.write(place.library, tape.name, file, done.text, *modes)
            outcome = P.WRITTEN if changed else P.UNCHANGED
            state = replace(state, file=file, songs=done.songs, outcome=outcome)
        else:
            note = "no song resolves, so the file was left as it was"
            state = replace(state, outcome=P.NOTHING, note=note)
    except (OSError, ValueError) as e:  # kept in the state, and the page shows it
        note = f"the playlist was not written: {e}"
        state = replace(prior, at=now, outcome=P.FAILED, note=note)
    S.save(place.layout, tape.id, state)
    return state


def rebuild(place: Place, beets: Beets, tape_id: str, now: float) -> P.State:
    """Ask the library about every song in one query, and rewrite if that changed
    anything. The caller holds the tape."""
    tape = T.read(place.layout, tape_id)
    try:
        held = beets.held_rows(place.runner, place.library, P.to_ask(tape))
    except Exception as e:  # any failure to ask means not knowing, never "missing"
        note = f"beets could not answer, so the playlist was left as it was: {e}"
        prior = S.read(place.layout, tape_id)
        state = replace(prior, outcome=P.UNKNOWN, note=note, at=now)
        S.save(place.layout, tape_id, state)
        return state
    entries = P.resolved(tape, held)
    moved = P.with_paths(tape, entries)
    if moved != tape:
        T.save(place.layout, moved)
    return _write(place, moved, entries, now)


def any_known(layout: Layout) -> bool:
    return any(S.known(layout, t.id) for t in T.listed(layout).tapes)


def rebuild_all(
    place: Place, beets: Beets, jobs: Jobs, now: float, *, besides: str = ""
) -> list[str]:
    """Every tape with a playlist, each on its own, saying what is worth a look."""
    notes: list[str] = []
    for tape in T.listed(place.layout).tapes:
        if tape.id == besides:
            continue
        try:
            kept = S.read(place.layout, tape.id)
            if kept.note == S.UNREADABLE:
                notes.append(f"{tape.name}: its playlist was not rebuilt: {kept.note}")
            if not kept.file:
                continue
            with jobs.claim(T.key_of(tape.id), "a playlist rebuild"):
                state = rebuild(place, beets, tape.id, now)
            notes += P.summary(tape, state)
        except Busy:
            notes.append(f"{tape.name}: busy, so its playlist was not rebuilt")
        except Exception as e:  # one tape's fault says nothing about the next
            notes.append(f"{tape.name}: its playlist was not rebuilt: {e}")
    return notes


def after_filing(
    place: Place, beets: Beets, jobs: Jobs, tape_id: str, now: float
) -> list[str]:
    """This tape from what filing recorded, then every other tape. Runs whichever way
    filing ended, so it never raises over the outcome that job has to report."""
    try:
        tape = T.read(place.layout, tape_id)
        on_disk = {s.path for s in tape.songs if s.path and Path(s.path).is_file()}
        _write(place, tape, P.from_recorded(tape, on_disk), now)
        return rebuild_all(place, beets, jobs, now, besides=tape_id)
    except Exception as e:
        return [f"playlists were not brought up to date: {e}"]


def refusal(place: Place, tape: TP.Tape, state: P.State) -> str:
    audio = T.audio_file(place.layout, tape.id).is_file()
    there = bool(state.file) and S.path_of(place.library, state.file).is_file()
    return P.archive_refusal(tape, state, audio=audio, playlist_there=there)


def archive(
    place: Place, beets: Beets, tape_id: str, job: Job, now: float
) -> dict[str, Any]:
    """The library is asked again here, whatever the page last showed. Then the copy
    is read back, and only then is anything cleared; the tape's document stays."""
    job.total = 3
    job.step(tape_id, "asking the library about every song")
    state = rebuild(place, beets, tape_id, now)
    if state.outcome in (P.UNKNOWN, P.FAILED):
        raise ValueError(state.note)
    tape = T.read(place.layout, tape_id)
    refused = refusal(place, tape, state)
    if refused:
        raise ValueError(refused)
    job.finished = 1
    job.step(tape.name, "copying to the archive and reading it back")
    kept = AR.keep_tape(place.runner, place.layout, tape_id)
    S.save(place.layout, tape_id, replace(state, archived={**kept, "at": now}))
    job.finished = 2
    job.step(tape.name, "clearing what is proved to be elsewhere")
    count = len(tape.songs)
    proved = {TP.cut_name(s, count) for s in tape.songs if not s.skip}
    cleared = T.clear_proved(place.layout, tape_id, proved, now)
    job.finished = 3
    return {**kept, "cleared": cleared}
