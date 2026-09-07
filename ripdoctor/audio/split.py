"""Cutting a plan into files, and building the clips that verify it."""

from __future__ import annotations

from dataclasses import dataclass

from ripdoctor.audio.runner import Runner
from ripdoctor.core.naming import track_filename
from ripdoctor.core.plan import Plan, PlanTrack

# Re-encoding is not optional. Stream copy cannot cut FLAC where it is asked to:
# it snaps to the nearest frame boundary, so every cut lands a little early or
# late and the error is different on every track.
CODEC = ("-c:a", "flac", "-compression_level", "8")

# The tick that makes a boundary answerable. 1400 Hz is clear of anything on a
# record and short enough not to mask what it sits on.
TICK_HZ = 1400
TICK_SECONDS = 0.06
TICK_VOLUME = 0.22
CLIP_RATE = 44100

# A clip window that runs past the end of a captured side hits the truncated
# final frame, and ffmpeg emits garbage rather than silence. Stop short of it.
EOF_MARGIN = 0.25


@dataclass(frozen=True, slots=True)
class Cut:
    """One track, and where it will be written."""

    track: PlanTrack
    source: str
    dest: str


def plan_cuts(plan: Plan, source_dir: str, dest_dir: str) -> list[Cut]:
    """Every track in a plan, paired with its source side and its filename."""
    return [
        Cut(
            track=t,
            source=f"{source_dir}/{side.file}",
            dest=f"{dest_dir}/{track_filename(t.number, t.title)}",
        )
        for side in plan.sides
        for t in side.tracks
    ]


def cut_argv(cut: Cut) -> list[str]:
    """The command that writes one track.

    `-ss` and `-to` come before `-i` so ffmpeg seeks rather than decoding and
    discarding, and both are absolute times on the source.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{cut.track.start:.3f}",
        "-to",
        f"{cut.track.end:.3f}",
        "-i",
        cut.source,
        *CODEC,
        cut.dest,
    ]


def cut_one(runner: Runner, cut: Cut, timeout: float = 300.0) -> None:
    runner.run(cut_argv(cut), timeout=timeout).require()


def tick_argv(
    source: str,
    at: float,
    dest: str,
    *,
    pre: float = 4.0,
    post: float = 4.0,
    duration: float | None = None,
) -> list[str]:
    """A clip with a tick mixed in at the moment being judged.

    The tick is delayed by exactly the distance from the clip's start to the
    boundary, so it lands on the instant in question rather than near it. That
    is the whole design: the listener answers one question - did the tick fall
    in the gap or on the music - instead of counting seconds.
    """
    start = max(0.0, at - pre)
    end = at + post
    if duration is not None:
        end = min(end, duration - EOF_MARGIN)

    delay_ms = round((at - start) * 1000)
    graph = (
        f"[0:a]aformat=sample_rates={CLIP_RATE}:channel_layouts=stereo[a];"
        f"[1:a]adelay={delay_ms}|{delay_ms},volume={TICK_VOLUME},"
        "aformat=channel_layouts=stereo[t];"
        "[a][t]amix=inputs=2:duration=first:normalize=0[o]"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.2f}",
        "-to",
        f"{end:.2f}",
        "-i",
        source,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={TICK_HZ}:duration={TICK_SECONDS}:sample_rate={CLIP_RATE}",
        "-filter_complex",
        graph,
        "-map",
        "[o]",
        "-c:a",
        "flac",
        dest,
    ]


def tick_one(
    runner: Runner,
    source: str,
    at: float,
    dest: str,
    *,
    pre: float = 4.0,
    post: float = 4.0,
    duration: float | None = None,
    timeout: float = 120.0,
) -> None:
    runner.run(
        tick_argv(source, at, dest, pre=pre, post=post, duration=duration),
        timeout=timeout,
    ).require()


def verify_argv(path: str) -> list[str]:
    """Test a written file. `-s` matters: without it progress reaches stdout."""
    return ["flac", "-t", "-s", path]


def verify(runner: Runner, path: str, timeout: float = 300.0) -> bool:
    return runner.run(verify_argv(path), timeout=timeout).ok
