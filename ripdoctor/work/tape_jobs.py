"""The long work of a tape's ear check: clips and a preview. ADR-063."""

from __future__ import annotations

from collections.abc import Sequence
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
