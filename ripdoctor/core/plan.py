"""The spec and the plan: intent in, decision out.

Two documents, deliberately not one.

A **spec** is what a person wants. It carries the catalogue tracklist, the
side's music start and end, and - crucially - any edges set by ear. It is the
input to fitting.

A **plan** is what the fitter decided: every track with its own start and end.
It is the input to cutting.

The reason they are separate is that fitting must be repeatable without losing
human corrections. Editing a boundary writes it back into the *spec* as an
ear-set edge, so re-running the fit passes it straight through instead of
recomputing over it. Collapse the two and every re-fit silently discards the
listening that produced the last one.

**A track's own start and end always win.** Nothing here overrides them - not a
detected gap, not the catalogue duration, not a manual gap override. The ear is
the only instrument that hears where a fade actually stops, and detector-chosen
edges measured two to ten seconds short at almost every track end on one record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Silence kept before a track's first note and after its last. The repeated
# human correction that produced these was "it cuts the lead-in and starts right
# before the first notes" - a boundary placed exactly at the music is audibly
# abrupt.
LEAD = 1.3
TAIL = 1.5


class OldFormat(Exception):
    """A plan in the superseded contiguous-cuts format.

    The predecessor's first plan format was one `cuts` list per side, where each
    boundary was a single point shared by two tracks - so the whole inter-track
    groove had to land inside one track or the other. On gaps running fourteen
    seconds that is a track ending in blank groove.

    These are refused rather than converted. Converting would guess which side of
    the boundary the groove belonged to, which is the question the format could
    not answer; and other tools still read the old format from disk.
    """


class BadPlan(ValueError):
    """A plan that cannot be cut from."""


@dataclass(frozen=True, slots=True)
class SpecTrack:
    """One track as requested: catalogue data, plus any ear-set edges."""

    number: int
    title: str
    cat: float  # catalogue duration, the arithmetic constraint
    start: float | None = None  # ear-set; overrides everything
    end: float | None = None  # ear-set; overrides everything

    @property
    def has_ear_edges(self) -> bool:
        return self.start is not None or self.end is not None


@dataclass(frozen=True, slots=True)
class SpecSide:
    """One side: where its music runs, and what is on it.

    `letter` is opaque and case-sensitive. It is usually a, b, c, d, but a
    single-track re-rip of a botched side is kept as its own side under its own
    name, so nothing may assume one character or an alphabet.

    `fix` overrides one gap's two edges by track number: the value is
    (music_end, next_music_start), measured by hand when a detector cannot see a
    gap it should have.
    """

    letter: str
    start: float
    end: float
    tracks: tuple[SpecTrack, ...]
    fix: dict[int, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Spec:
    slug: str
    album: str
    artist: str
    sides: tuple[SpecSide, ...]
    date: str = ""
    lead: float = LEAD
    tail: float = TAIL

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Spec:
        sides = []
        for s in d.get("sides", []):
            tracks = tuple(
                SpecTrack(
                    number=int(t["number"]),
                    title=str(t.get("title", "")),
                    cat=float(t.get("len", 0.0)),
                    start=None if t.get("start") is None else float(t["start"]),
                    end=None if t.get("end") is None else float(t["end"]),
                )
                for t in s.get("tracks", [])
            )
            sides.append(
                SpecSide(
                    letter=str(s["letter"]),
                    start=float(s["start"]),
                    end=float(s["end"]),
                    tracks=tracks,
                    fix={
                        int(k): (float(v[0]), float(v[1]))
                        for k, v in (s.get("fix") or {}).items()
                    },
                )
            )
        return cls(
            slug=str(d["slug"]),
            album=str(d.get("album", "")),
            artist=str(d.get("artist", "")),
            sides=tuple(sides),
            date=str(d.get("date", "")),
            lead=float(d.get("lead", LEAD)),
            tail=float(d.get("tail", TAIL)),
        )


@dataclass(frozen=True, slots=True)
class PlanTrack:
    """One track as decided: its own start and end, and the catalogue it was
    checked against.

    `start` and `end` are not shared with neighbours. Whatever lies between one
    track's end and the next one's start is groove, and is not written.
    """

    number: int
    title: str
    start: float
    end: float
    cat: float

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def delta(self) -> float:
        """Measured minus catalogue.

        A consistent negative bias across a side is normal - quiet heads and
        tails fall below any threshold. An outlier is the bug signal, and equal
        and opposite deltas on adjacent tracks point straight at the boundary
        between them.
        """
        return self.length - self.cat


@dataclass(frozen=True, slots=True)
class PlanSide:
    file: str
    tracks: tuple[PlanTrack, ...]


@dataclass(frozen=True, slots=True)
class Plan:
    slug: str
    album: str
    artist: str
    sides: tuple[PlanSide, ...]
    date: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
        sides = []
        for s in d.get("sides", []):
            if "cuts" in s:
                raise OldFormat(
                    "this plan uses the superseded contiguous 'cuts' format; "
                    "re-fit it rather than converting"
                )
            sides.append(
                PlanSide(
                    file=str(s["file"]),
                    tracks=tuple(
                        PlanTrack(
                            number=int(t["number"]),
                            title=str(t.get("title", "")),
                            start=float(t["start"]),
                            end=float(t["end"]),
                            cat=float(t.get("cat", 0.0)),
                        )
                        for t in s.get("tracks", [])
                    ),
                )
            )
        return cls(
            slug=str(d["slug"]),
            album=str(d.get("album", "")),
            artist=str(d.get("artist", "")),
            sides=tuple(sides),
            date=str(d.get("date", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "album": self.album,
            "artist": self.artist,
            "date": self.date,
            "slug": self.slug,
            "sides": [
                {
                    "file": s.file,
                    "tracks": [
                        {
                            "number": t.number,
                            "title": t.title,
                            "start": round(t.start, 2),
                            "end": round(t.end, 2),
                            "cat": t.cat,
                        }
                        for t in s.tracks
                    ],
                }
                for s in self.sides
            ],
        }


def validate(plan: Plan, durations: dict[str, float] | None = None) -> None:
    """Refuse a plan that cannot be cut. Raises BadPlan with the reason.

    Every check here corresponds to a way a cut goes wrong silently: a track
    that ends before it starts writes nothing, overlapping tracks write the same
    audio twice, a duplicate number collides on import, and an end past the side
    writes a truncated final track.
    """
    seen: dict[int, str] = {}
    for side in plan.sides:
        prev: PlanTrack | None = None
        for t in side.tracks:
            if t.end <= t.start:
                raise BadPlan(
                    f"{side.file} track {t.number}: end {t.end:.2f} is not after "
                    f"start {t.start:.2f}"
                )
            if t.number in seen:
                raise BadPlan(
                    f"track number {t.number} appears in both {seen[t.number]} "
                    f"and {side.file}"
                )
            seen[t.number] = side.file
            if prev is not None and t.start < prev.end:
                raise BadPlan(
                    f"{side.file}: track {t.number} starts at {t.start:.2f}, "
                    f"before track {prev.number} ends at {prev.end:.2f}"
                )
            prev = t

        if durations and side.file in durations:
            dur = durations[side.file]
            for t in side.tracks:
                if t.end > dur + 0.05:
                    raise BadPlan(
                        f"{side.file} track {t.number}: end {t.end:.2f} is past "
                        f"the side's {dur:.2f}s"
                    )
