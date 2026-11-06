"""The spec and the plan: intent in, decision out. docs/method.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Silence kept before a track's first note and after its last. A boundary placed
# exactly at the music is audibly abrupt.
LEAD = 1.3
TAIL = 1.5

# Who settled a track's boundaries. Absent, the fit did, or the catalogue if the
# track is forced. Either value is a person's last judgement, and no fit moves it.
HAND = "hand"  # moved or placed by a person
EAR = "ear"  # left where it was, and heard to be right


def _settled(value: Any) -> str:
    """An unknown value reads as absent, so it can never clear a doubt."""
    return str(value) if value in (HAND, EAR) else ""


class OldFormat(Exception):
    """A superseded contiguous-cuts plan. Refused, never converted."""


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
    settled: str = ""  # who did, carried onto the plan; a save makes every edge ear-set

    @property
    def has_ear_edges(self) -> bool:
        return self.start is not None or self.end is not None


@dataclass(frozen=True, slots=True)
class SpecSide:
    """One side. `letter` is opaque - nothing may assume one character."""

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
    # The release this was fitted against. Kept because everything downstream
    # that wants to ask the catalogue again - cover art, most obviously - has
    # no other way to find the same entry.
    mbid: str = ""
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
                    settled=_settled(t.get("settled")),
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
            mbid=str(d.get("mbid", "")),
            lead=float(d.get("lead", LEAD)),
            tail=float(d.get("tail", TAIL)),
        )


@dataclass(frozen=True, slots=True)
class PlanTrack:
    """One track as decided. Edges are not shared with neighbours."""

    number: int
    title: str
    start: float
    end: float
    cat: float
    # Placed from the catalogue rather than found, so `delta` is zero by
    # construction and says nothing about whether the boundary is right.
    forced: bool = False
    settled: str = ""

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def delta(self) -> float:
        """Measured minus catalogue. A steady bias is normal; an outlier is not."""
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
                            # A person's judgement ends the doubt, in any document.
                            forced=bool(t.get("forced", False))
                            and not _settled(t.get("settled")),
                            settled=_settled(t.get("settled")),
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
                            **({"forced": True} if t.forced else {}),
                            **({"settled": t.settled} if t.settled else {}),
                        }
                        for t in s.tracks
                    ],
                }
                for s in self.sides
            ],
        }


def cut_refusal(plan: Plan) -> str:
    """Why this plan may not be cut yet, naming every track, or nothing. ADR-064."""
    waiting = [t for side in plan.sides for t in side.tracks if t.forced]
    if not waiting:
        return ""
    names = ", ".join(f'{t.number} "{t.title}"' for t in waiting)
    return (
        f"placed from the catalogue and not yet checked: {names}. Listen to each "
        "and press Checked, or move its boundary."
    )


def validate(plan: Plan, durations: dict[str, float] | None = None) -> None:
    """Refuse a plan that cannot be cut, with the reason."""
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
