"""The spec and the plan: intent in, decision out. docs/method.md."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field, replace
from typing import Any

# Silence kept before a track's first note and after its last. A boundary placed
# exactly at the music is audibly abrupt.
LEAD = 1.3
TAIL = 1.5

# Who settled a track's boundaries. Absent, the fit did, or the catalogue if the
# track is forced. Either value is a person's last judgement, and no fit moves it.
HAND = "hand"  # moved or placed by a person
EAR = "ear"  # left where it was, and heard to be right

# What a fit's boundary rests on. Absent, nothing says, which is every plan from
# before the field and a track whose edges were handed to the fit: no claim is
# made for it either way.
GAP = "gap"  # its edges came from what was measured
LENGTH = "length"  # an edge has no gap behind it, only the catalogue's arithmetic
EARLIER = "earlier"  # its times are positions on a capture since replaced


def _settled(value: Any) -> str:
    """An unknown value reads as absent, so it can never clear a doubt."""
    return str(value) if value in (HAND, EAR) else ""


def _placed(value: Any) -> str:
    return str(value) if value in (GAP, LENGTH, EARLIER) else ""


def letter_of(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1]
    return stem[len("side-") : -len(".flac")] if stem.startswith("side-") else stem


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
    placed: str = ""

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def on_replaced_audio(self) -> bool:
        """Its times are positions on a capture since replaced, and nobody has
        said otherwise. A person's mark made since is about what is there now."""
        return self.placed == EARLIER and not self.settled

    @property
    def delta(self) -> float:
        """Measured minus catalogue. A steady bias is normal; an outlier is not."""
        return self.length - self.cat


@dataclass(frozen=True, slots=True)
class PlanSide:
    file: str
    tracks: tuple[PlanTrack, ...]
    # The capture these times are positions on, as the stamp the cache keys it
    # by. Empty on a plan from before the field: nothing says which it was.
    audio: str = ""


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
                            placed=_placed(t.get("placed")),
                        )
                        for t in s.get("tracks", [])
                    ),
                    audio=str(s.get("audio", "")),
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
                    **({"audio": s.audio} if s.audio else {}),
                    "tracks": [
                        {
                            "number": t.number,
                            "title": t.title,
                            "start": round(t.start, 2),
                            "end": round(t.end, 2),
                            "cat": t.cat,
                            **({"forced": True} if t.forced else {}),
                            **({"settled": t.settled} if t.settled else {}),
                            **({"placed": t.placed} if t.placed else {}),
                        }
                        for t in s.tracks
                    ],
                }
                for s in self.sides
            ],
        }


@dataclass(frozen=True, slots=True)
class Released:
    """A person's mark a new fit does not hold, and why."""

    file: str
    track: PlanTrack
    why: str


def _gone(side: PlanSide, stamps: dict[str, str]) -> str:
    """Why this side's times cannot be trusted to mean the audio on disk, or nothing."""
    if side.file not in stamps:
        return "that side is no longer there"
    if not side.audio:
        return "nothing records which capture it was set on"
    if side.audio != stamps[side.file]:
        return "that side has been captured again since"
    return ""


def holding_only_current(
    plan: Plan, stamps: dict[str, str]
) -> tuple[Plan, tuple[Released, ...]]:
    """The plan with no mark left on audio that has gone, and which ones went.

    A settled time is a position in one capture. On another it points at
    different music, so it is a judgement about nothing. ADR-064.
    """
    released: list[Released] = []
    sides = []
    for side in plan.sides:
        why = _gone(side, stamps)
        if not why:
            sides.append(side)
            continue
        released += [Released(side.file, t, why) for t in side.tracks if t.settled]
        sides.append(
            replace(side, tracks=tuple(replace(t, settled="") for t in side.tracks))
        )
    return replace(plan, sides=tuple(sides)), tuple(released)


def as_it_stands(plan: Plan, stamps: dict[str, str]) -> Plan:
    """The plan as it should read now: a replaced side's times are not what they say.

    A side nothing can compare is left alone. A plan from before the field, or a
    side nobody has on disk, gives no grounds to call its times wrong.
    """
    return replace(
        plan,
        sides=tuple(
            replace(side, tracks=tuple(_from_earlier(t) for t in side.tracks))
            if side.audio and side.file in stamps and side.audio != stamps[side.file]
            else side
            for side in plan.sides
        ),
    )


def _from_earlier(t: PlanTrack) -> PlanTrack:
    return replace(t, settled="", placed=EARLIER)


def saved_over(
    plan: Plan, before: Plan | None, stamps: dict[str, str]
) -> tuple[Plan, tuple[Released, ...]]:
    """A plan being saved, on the capture that is there now.

    A time unchanged from the plan it replaces, on a side captured again since,
    was placed on the earlier audio: saving it again must not launder it into a
    judgement about this one. A time that differs was set here, on what is
    there now, and is whatever the page says it is. ADR-064.
    """
    old = {s.file: s for s in before.sides} if before else {}
    released: list[Released] = []
    sides = []
    for side in plan.sides:
        now = stamps.get(side.file, "")
        was = old.get(side.file)
        if not now:
            sides.append(replace(side, audio=was.audio if was else ""))
            continue
        if was is None or not was.audio or was.audio == now:
            tracks = tuple(_heard_here(t) for t in side.tracks)
            sides.append(replace(side, tracks=tracks, audio=now))
            continue
        placed = {t.number: t for t in was.tracks}
        kept = []
        for t in side.tracks:
            if t.placed == EARLIER and t.settled == EAR:
                kept.append(_heard_here(t))
                continue
            prior = placed.get(t.number)
            same = prior is not None and (prior.start, prior.end) == (t.start, t.end)
            if prior is not None and same and prior.settled:
                released.append(
                    Released(side.file, prior, "that side has been captured again")
                )
            kept.append(_from_earlier(t) if same else t)
        sides.append(replace(side, tracks=tuple(kept), audio=now))
    return replace(plan, sides=tuple(sides)), tuple(released)


def _heard_here(t: PlanTrack) -> PlanTrack:
    """A check by ear on a row the page was showing as from an earlier capture.

    The page only ever says `earlier` after the capture was replaced, so the two
    together cannot come from a tab older than the re-rip: they are somebody
    listening to the audio that is there now, and the time is theirs.
    """
    return replace(t, placed="") if t.placed == EARLIER and t.settled else t


def cut_refusal(plan: Plan, alignable: Collection[str] = ()) -> str:
    """Why this plan may not be cut yet, naming every track, or nothing. ADR-064.

    Give it the plan `as_it_stands`: what was saved says nothing about a capture
    replaced since it was written. `alignable` is the sides Align can still carry,
    so a way out is only named where it is one.
    """
    said = [
        _on_replaced_audio(
            letter_of(side.file), gone, letter_of(side.file) in alignable
        )
        for side in plan.sides
        if (gone := [t for t in side.tracks if t.on_replaced_audio])
    ]
    waiting = [
        t
        for side in plan.sides
        for t in side.tracks
        if t.forced and not t.on_replaced_audio
    ]
    if waiting:
        said.append(
            f"placed from the catalogue and not yet checked: {_names(waiting)}. "
            "Listen to each and press Checked, or move its boundary."
        )
    return " ".join(said)


def _names(tracks: list[PlanTrack]) -> str:
    return ", ".join(f'{t.number} "{t.title}"' for t in tracks)


def _on_replaced_audio(letter: str, tracks: list[PlanTrack], align: bool) -> str:
    aligning = ", or Align from archive" if align else ""
    return (
        f"side {letter} was captured again after these were placed, so their times "
        f"are positions on audio that has gone: {_names(tracks)}. Run First pass, "
        f"which keeps what you have set since{aligning}. "
        "A track you have listened to on this capture can be marked Checked, or moved."
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
