"""The on-disk layout, and reading and writing the two documents."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ripdoctor.core.plan import LEAD, TAIL, Plan, Spec, SpecSide, SpecTrack
from ripdoctor.store.safety import under

# Side files are named by letter, and the letter is opaque - a single-track
# re-rip is kept as its own side under its own name.
SIDE = "side-{letter}.flac"

# arecord's diagnostics, kept beside a capture and removed with it.
LOG = ".log"


@dataclass(frozen=True, slots=True)
class Layout:
    """Where everything lives under one vinyl root."""

    root: Path
    # Named rather than fixed so a pool that already holds measurements under
    # another name can be adopted in place. The format is the same file for the
    # same side; re-measuring an archive to change a directory name would cost
    # hours of ffmpeg for nothing.
    cache_name: str = ".cache"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def cache(self) -> Path:
        return self.work / self.cache_name

    def album_dir(self, slug: str) -> Path:
        """A record's sides: raw first, then archive.

        An imported record is moved to archive, and re-cutting one is a normal
        thing to want. Looking only in raw makes that fail obscurely - the
        decode writes nothing, the envelope comes back empty, and the failure
        surfaces somewhere far from its cause.

        The test is for sides rather than for a directory, because a punch is
        written into raw for a record whose sides are in archive. A directory
        holding one punched track would otherwise shadow the archive it was
        taken from, and every side of that record would stop resolving.
        """
        for base in (self.raw, self.archive):
            candidate = base / slug
            if candidate.is_dir() and any(candidate.glob(SIDE.format(letter="*"))):
                return under(base, slug)
        raise FileNotFoundError(f"no album {slug!r} in {self.raw} or {self.archive}")

    def albums(self, include_archive: bool = False) -> list[str]:
        """Records with sides on disk, raw first and never listed twice."""
        found: list[str] = []
        roots = (self.raw, self.archive) if include_archive else (self.raw,)
        for base in roots:
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if (
                    d.is_dir()
                    and d.name not in found
                    and any(p.name.startswith("side-") for p in d.iterdir())
                ):
                    found.append(d.name)
        return found

    def side_file(self, slug: str, letter: str) -> Path:
        where = self.album_dir(slug) / SIDE.format(letter=letter)
        if not where.is_file():
            raise FileNotFoundError(f"no side {letter!r} for {slug!r}")
        return where

    def sides_on_disk(self, slug: str) -> list[str]:
        found = []
        for p in sorted(self.album_dir(slug).iterdir()):
            name = p.name
            if name.startswith("side-") and name.endswith(".flac"):
                found.append(name[len("side-") : -len(".flac")])
        return found

    def spec_file(self, slug: str) -> Path:
        return under(self.work, f"{slug}.spec.json")

    def plan_file(self, slug: str) -> Path:
        return under(self.work, f"{slug}.plan.json")

    def review_dir(self, slug: str) -> Path:
        return under(self.review, slug)

    def clips_dir(self, slug: str) -> Path:
        """Clips are a sibling of the album, never inside it.

        An importer pointed at the review directory would otherwise take the
        tick clips for tracks.
        """
        return under(self.review, f"_clips-{slug}")

    def ensure(self) -> None:
        for d in (self.raw, self.work, self.review, self.archive, self.cache):
            d.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write atomically, so an interrupted write cannot truncate what was there.

    The temporary file is made beside the target rather than in the system
    temporary directory: os.replace cannot cross filesystems, and on a machine
    where the pool is its own mount that is exactly what would happen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)


def read_spec(path: Path) -> Spec:
    return Spec.from_dict(json.loads(path.read_text()))


def read_plan(path: Path) -> Plan:
    return Plan.from_dict(json.loads(path.read_text()))


def save(layout: Layout, slug: str, spec: Spec, plan: Plan) -> tuple[Path, Path]:
    """Write both documents together.

    Never one without the other. The plan is what the cutter reads; the spec is
    where the same edges become ear overrides that win on a re-fit. Writing only
    the plan means the next fit recomputes over a decision somebody made by
    listening, and there is nothing to say it happened.
    """
    spec_path = layout.spec_file(slug)
    plan_path = layout.plan_file(slug)
    write_json(spec_path, _spec_to_dict(spec))
    write_json(plan_path, plan.to_dict())
    return spec_path, plan_path


def _spec_to_dict(spec: Spec) -> dict[str, Any]:
    return {
        "slug": spec.slug,
        "album": spec.album,
        "artist": spec.artist,
        "date": spec.date,
        "mbid": spec.mbid,
        "lead": spec.lead,
        "tail": spec.tail,
        "sides": [
            {
                "letter": s.letter,
                "start": s.start,
                "end": s.end,
                **(
                    {"fix": {str(k): list(v) for k, v in s.fix.items()}}
                    if s.fix
                    else {}
                ),
                "tracks": [
                    {
                        "number": t.number,
                        "title": t.title,
                        "len": t.cat,
                        **({"start": t.start} if t.start is not None else {}),
                        **({"end": t.end} if t.end is not None else {}),
                    }
                    for t in s.tracks
                ],
            }
            for s in spec.sides
        ],
    }


def remember(
    layout: Layout, slug: str, *, album: str, artist: str, date: str = ""
) -> Path:
    """Record what a record is called, before anything has been cut from it.

    A spec with no tracks in it. Only the names are touched, so one that
    already carries boundaries somebody set by ear keeps every one. ADR-041.
    """
    where = layout.spec_file(slug)
    if where.is_file():
        current = read_spec(where)
        spec = replace(
            current,
            album=album or current.album,
            artist=artist or current.artist,
            date=date or current.date,
        )
    else:
        spec = Spec(slug=slug, album=album, artist=artist, date=date, sides=())
    write_json(where, _spec_to_dict(spec))
    return where


def forget(layout: Layout, slug: str) -> bool:
    """Remove a name that turned out to belong to nothing.

    Deliberately timid, and the audio test is "any file at all except a capture
    log" rather than a list of the names sides are known by. ADR-041.
    """
    where = layout.spec_file(slug)
    if not where.is_file() or layout.plan_file(slug).is_file():
        return False
    try:
        spec = read_spec(where)
    except (OSError, ValueError, KeyError):
        return False
    if spec.sides or spec.mbid:
        return False
    for base in (layout.raw, layout.archive):
        album = under(base, slug)
        if album.is_dir() and any(
            p.is_file() and p.suffix != LOG for p in album.rglob("*")
        ):
            return False
    where.unlink(missing_ok=True)
    # An emptied album directory is not tidiness: album_dir looks for sides
    # rather than for a directory, but a stray one still turns up in listings.
    with suppress(OSError):
        under(layout.raw, slug).rmdir()
    return True


def letter_of(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1]
    return stem[len("side-") : -len(".flac")] if stem.startswith("side-") else stem


def spec_of(plan: Plan, keep: Spec | None = None) -> Spec:
    """A spec derived from a plan, with every edge ear-set.

    A plan that was saved is a decision somebody made about where the cuts go,
    so the next fit passes those edges through rather than recomputing them.

    `keep` is the spec being replaced: its lead, tail and per-side fix map come
    across, because the plan carries none of them and a save that dropped them
    would undo the overrides the spec exists to hold. ADR-041.
    """
    fixes = {s.letter: s.fix for s in keep.sides} if keep else {}
    sides = []
    for side in plan.sides:
        tracks = tuple(
            SpecTrack(
                number=t.number, title=t.title, cat=t.cat, start=t.start, end=t.end
            )
            for t in side.tracks
        )
        letter = letter_of(side.file)
        sides.append(
            SpecSide(
                letter=letter,
                start=side.tracks[0].start if side.tracks else 0.0,
                end=side.tracks[-1].end if side.tracks else 0.0,
                tracks=tracks,
                fix=dict(fixes.get(letter, {})),
            )
        )
    return Spec(
        slug=plan.slug,
        album=plan.album,
        artist=plan.artist,
        date=plan.date,
        sides=tuple(sides),
        lead=keep.lead if keep else LEAD,
        tail=keep.tail if keep else TAIL,
    )


def spec_from_plan(spec: Spec, plan: Plan) -> Spec:
    """Fold a plan's boundaries back into its spec as ear-set edges.

    This is what makes a re-fit safe after somebody has moved a boundary: the
    next run passes those edges through instead of recomputing over them.
    """

    placed = {(side.file, t.number): t for side in plan.sides for t in side.tracks}
    sides = []
    for s in spec.sides:
        file = SIDE.format(letter=s.letter)
        tracks = tuple(
            replace(
                t,
                start=placed[(file, t.number)].start
                if (file, t.number) in placed
                else t.start,
                end=placed[(file, t.number)].end
                if (file, t.number) in placed
                else t.end,
            )
            for t in s.tracks
        )
        sides.append(replace(s, tracks=tracks))
    return replace(spec, sides=tuple(sides))
