"""The long work on a tape: clips, a preview and its cuts. ADR-063."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ripdoctor.audio import split
from ripdoctor.audio.runner import Runner
from ripdoctor.core import tape as TP
from ripdoctor.store import cache as C
from ripdoctor.store import tapes as T
from ripdoctor.store.files import Layout
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
