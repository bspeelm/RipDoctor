"""Cutting and clipping, tested by the argv they construct.

For this layer the argv is the behaviour. ffmpeg exits zero whether or not it
took the right seconds, so a test that only checks the return value checks
nothing at all.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from ripdoctor.audio.runner import (
    FakeRunner,
    RealRunner,
    Result,
    ToolFailed,
    ToolMissing,
)
from ripdoctor.audio.split import (
    CLIP_RATE,
    EOF_MARGIN,
    TICK_HZ,
    Cut,
    cut_argv,
    cut_one,
    cut_verified,
    plan_cuts,
    span_argv,
    tick_argv,
    tick_one,
    verify,
    verify_argv,
)
from ripdoctor.core.plan import Plan, PlanSide, PlanTrack
from ripdoctor.work import punch


def track(n: int = 1, start: float = 15.35, end: float = 167.15) -> PlanTrack:
    return PlanTrack(
        number=n, title=f"Track {n}", start=start, end=end, cat=end - start
    )


def a_cut(**kw: float) -> Cut:
    return Cut(track=track(**kw), source="raw/side-a.flac", dest="out/01 Track 1.flac")


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs a real ffmpeg"
)
RATE, FRAME = 44100, 4096
MIN_FLAC_FRAME = 16


def sample_at(ms: int) -> int:
    """Half a sample rounds up, as ffmpeg's seek does; `round` would go to even."""
    return (ms * RATE + 500) // 1000


def starts_leaving_a_sliver(seconds: int, step_ms: int) -> list[int]:
    """Times on a grid, in ms, that fall under FLAC's minimum from a frame's end."""
    ends = (k * FRAME for k in range(1, seconds * RATE // FRAME))
    times = ((end, end * 1000 // RATE // step_ms * step_ms) for end in ends)
    return [ms for end, ms in times if 1 <= end - sample_at(ms) < MIN_FLAC_FRAME]


def a_side_of_known_frames(runner: RealRunner, path: Path, seconds: int) -> Path:
    tone = f"sine=frequency=440:sample_rate={RATE}:duration={seconds}"
    argv = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", tone]
    runner.run([*argv, "-c:a", "flac", "-frame_size", str(FRAME), str(path)]).require()
    return path


def decoded(runner: RealRunner, path: Path) -> bytes:
    argv = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-"]
    return runner.run(argv).require().stdout


# ------------------------------------------------------------- the cut


def test_the_cut_takes_the_exact_seconds_the_plan_says() -> None:
    argv = cut_argv(a_cut(start=15.35, end=167.15))
    assert flag(argv, "-ss") == "15.350"
    assert flag(argv, "-to") == "167.150"
    assert argv[-1] == "out/01 Track 1.flac"


def test_boundaries_keep_millisecond_resolution() -> None:
    """Rounding to hundredths moves a cut by up to 5 ms on every track."""
    argv = cut_argv(a_cut(start=641.907, end=645.001))
    assert flag(argv, "-ss") == "641.907"
    assert flag(argv, "-to") == "645.001"


def test_seeking_happens_before_the_input() -> None:
    """After -i, ffmpeg decodes and discards everything up to the start."""
    argv = cut_argv(a_cut())
    assert argv.index("-ss") < argv.index("-i")
    assert argv.index("-to") < argv.index("-i")


def test_the_audio_is_re_encoded_never_stream_copied() -> None:
    """Stream copy snaps to the nearest frame boundary.

    Every cut then lands a little early or late, by a different amount on every
    track, and nothing reports it.
    """
    argv = cut_argv(a_cut())
    assert "-c:a" in argv and flag(argv, "-c:a") == "flac"
    assert "copy" not in argv


def test_a_plan_becomes_one_cut_per_track_with_numbered_names() -> None:
    plan = Plan(
        slug="s",
        album="",
        artist="",
        sides=(
            PlanSide(file="side-a.flac", tracks=(track(1), track(2, 170.0, 300.0))),
            PlanSide(file="side-b.flac", tracks=(track(3, 12.0, 200.0),)),
        ),
    )
    cuts = plan_cuts(plan, "raw/album", "review/album")
    assert [c.track.number for c in cuts] == [1, 2, 3]
    assert cuts[0].source == "raw/album/side-a.flac"
    assert cuts[2].source == "raw/album/side-b.flac"
    assert cuts[0].dest == "review/album/01 Track 1.flac"
    assert [c.dest for c in cuts] == sorted(c.dest for c in cuts)


def test_a_failed_cut_is_raised_not_ignored() -> None:
    fake = FakeRunner().expect("ffmpeg", returncode=1, stderr=b"Invalid data found")
    with pytest.raises(ToolFailed, match="Invalid data found"):
        cut_one(fake, a_cut())


def test_a_title_cannot_escape_into_the_argv() -> None:
    """The argv list is what makes this safe; the test says so out loud."""
    cut = Cut(
        track=PlanTrack(
            number=1, title='a"; rm -rf /; echo "b', start=0.0, end=1.0, cat=1.0
        ),
        source="in.flac",
        dest="out/01 a_ rm -rf _ echo _b.flac",
    )
    fake = FakeRunner()
    cut_one(fake, cut)
    assert fake.calls[0][-1] == cut.dest
    assert not any(";" in a for a in fake.calls[0][:-1])


# ------------------------------------------------------------ the tick


def test_the_tick_lands_on_the_boundary_not_near_it() -> None:
    """The delay is measured from the clip's own start, not from zero."""
    argv = tick_argv("side-a.flac", at=641.90, dest="clip.flac", pre=4.0)
    graph = flag(argv, "-filter_complex")
    assert "adelay=4000|4000" in graph
    assert flag(argv, "-ss") == "637.90"


def test_a_boundary_near_the_start_of_a_side_still_gets_a_tick() -> None:
    """Clamping the start to zero must move the delay with it."""
    argv = tick_argv("side-a.flac", at=1.5, dest="clip.flac", pre=4.0)
    assert flag(argv, "-ss") == "0.00"
    assert "adelay=1500|1500" in flag(argv, "-filter_complex")


def test_the_clip_stops_short_of_a_truncated_end() -> None:
    """A window past EOF hits the truncated final frame and ffmpeg emits
    garbage rather than silence. That garbage is what made one clip
    unreviewable."""
    duration = 1325.056
    argv = tick_argv(
        "side-a.flac", at=1324.0, dest="c.flac", post=4.0, duration=duration
    )
    assert float(flag(argv, "-to")) == pytest.approx(duration - EOF_MARGIN, abs=0.01)
    assert float(flag(argv, "-to")) < duration

    # A window that already ends inside the side is left alone.
    inside = tick_argv(
        "side-a.flac", at=1000.0, dest="c.flac", post=4.0, duration=duration
    )
    assert flag(inside, "-to") == "1004.00"


def test_with_no_duration_known_the_window_is_not_clamped() -> None:
    argv = tick_argv("side-a.flac", at=100.0, dest="c.flac", post=4.0)
    assert flag(argv, "-to") == "104.00"


def test_the_tick_is_the_measured_tone_and_is_mixed_not_replacing() -> None:
    argv = tick_argv("side-a.flac", at=50.0, dest="c.flac")
    tone = next(a for a in argv if a.startswith("sine="))
    assert f"frequency={TICK_HZ}" in tone
    assert f"sample_rate={CLIP_RATE}" in tone
    graph = flag(argv, "-filter_complex")
    assert "amix=inputs=2" in graph, "the tick replaced the audio, not joined it"
    assert "normalize=0" in graph, "mixing would halve the music's level"


def test_a_failed_clip_is_raised() -> None:
    fake = FakeRunner().expect("ffmpeg", returncode=1, stderr=b"invalid residual")
    with pytest.raises(ToolFailed, match="invalid residual"):
        tick_one(fake, "side-a.flac", 10.0, "c.flac")


# ---------------------------------------------------------- verification


def test_verification_silences_progress_output() -> None:
    """Without -s, flac writes progress to stdout and callers parse noise."""
    assert "-s" in verify_argv("out.flac")
    assert verify_argv("out.flac")[:3] == ["flac", "-t", "-s"]


def test_verification_reports_rather_than_raising() -> None:
    """A bad file is an answer the caller acts on, not an exception."""
    good = FakeRunner()
    bad = FakeRunner().expect("flac", returncode=1, stderr=b"LOST_SYNC")
    assert verify(good, "a.flac") is True
    assert verify(bad, "a.flac") is False


# ----------------------------------------------------- one stretch of a source


def test_the_album_flow_argv_is_exactly_what_it_was() -> None:
    assert cut_argv(a_cut()) == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "15.350",
        "-to",
        "167.150",
        "-i",
        "raw/side-a.flac",
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        "-frame_size",
        "4096",
        "out/01 Track 1.flac",
    ]


def test_the_encoder_frame_size_is_never_left_to_the_first_frame() -> None:
    for argv in (
        cut_argv(a_cut()),
        punch.cut_argv("side.flac", 1.0, 2.0, "o.flac"),
        tick_argv("side.flac", at=9.0, dest="c.flac"),
    ):
        assert flag(argv, "-frame_size") == "4096"


@needs_ffmpeg
@pytest.mark.parametrize("build", [span_argv, punch.cut_argv])
def test_a_cut_starting_just_before_a_source_frame_ends_keeps_every_sample(
    tmp_path: Path, build: Callable[[str, float, float, str], list[str]]
) -> None:
    """The trim leaves a first frame of a few samples, under FLAC's minimum.

    The arithmetic finds starts that do that to a source of known frames, and
    each must come out as exactly that stretch of the source.
    """
    runner = RealRunner()
    source = a_side_of_known_frames(runner, tmp_path / "side.flac", seconds=6)
    whole = decoded(runner, source)
    starts = starts_leaving_a_sliver(seconds=6, step_ms=1)
    assert len(starts) >= 8, "too few starts to exercise the fault"

    for ms in starts[:8]:
        dest = tmp_path / f"{ms}.flac"
        runner.run(build(str(source), ms / 1000, ms / 1000 + 1, str(dest))).require()
        first = sample_at(ms) * 2
        assert decoded(runner, dest) == whole[first : first + RATE * 2]


@needs_ffmpeg
def test_a_clip_starting_just_before_a_source_frame_ends_is_still_written(
    tmp_path: Path,
) -> None:
    """A clip's start has two decimals, so a coarser grid and a longer source."""
    runner = RealRunner()
    source = a_side_of_known_frames(runner, tmp_path / "side.flac", seconds=30)
    starts = starts_leaving_a_sliver(seconds=20, step_ms=10)
    assert len(starts) >= 4, "too few starts to exercise the fault"

    for ms in starts[:4]:
        dest = tmp_path / f"{ms}.flac"
        runner.run(tick_argv(str(source), ms / 1000 + 4, str(dest))).require()
        assert len(decoded(runner, dest)) == 8 * RATE * 2 * 2


def test_a_stretch_seeks_before_the_input_and_re_encodes_exactly() -> None:
    argv = span_argv("tape.flac", 61.5, 200.125, "part/01 A.flac")
    assert argv.index("-ss") < argv.index("-to") < argv.index("-i")
    assert flag(argv, "-ss") == "61.500"
    assert flag(argv, "-to") == "200.125"
    assert flag(argv, "-c:a") == "flac"
    assert "copy" not in argv
    assert argv[-1] == "part/01 A.flac"


def test_a_cut_that_verifies_says_nothing() -> None:
    runner = FakeRunner()
    assert cut_verified(runner, "t.flac", 1.0, 2.0, "o.flac", 60.0) == ""
    assert [c[0] for c in runner.calls] == ["ffmpeg", "flac"]


def test_a_cut_that_will_not_decode_says_so() -> None:
    runner = FakeRunner().expect(lambda a: a[0] == "flac", returncode=1)
    assert "would not decode" in cut_verified(runner, "t", 1.0, 2.0, "o.flac", 60.0)


def test_a_failed_cut_is_not_verified_and_carries_what_ffmpeg_said() -> None:
    runner = FakeRunner().expect(
        lambda a: a[0] == "ffmpeg", returncode=1, stderr=b"No space left on device"
    )
    assert "No space left" in cut_verified(runner, "t", 1.0, 2.0, "o.flac", 60.0)
    assert [c[0] for c in runner.calls] == ["ffmpeg"]


def test_a_cut_that_hangs_is_answered_rather_than_raised() -> None:
    class Hangs(FakeRunner):
        def run(
            self,
            argv: Sequence[str],
            *,
            stdin: bytes | None = None,
            timeout: float | None = None,
        ) -> Result:
            raise subprocess.TimeoutExpired(list(argv), timeout or 0.0)

    assert "longer than 60 s" in cut_verified(Hangs(), "t", 1.0, 2.0, "o.flac", 60.0)


def test_a_missing_ffmpeg_stops_the_whole_cut_rather_than_each_song() -> None:
    with pytest.raises(ToolMissing):
        cut_verified(FakeRunner(installed=set()), "t", 1.0, 2.0, "o.flac", 60.0)
