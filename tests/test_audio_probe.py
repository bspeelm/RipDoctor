"""Measuring a file: the rate, the real duration, and the astats parser.

Every case here corresponds to something that went wrong on a real file. Two of
them returned a plausible number rather than an error, which is the failure mode
worth the most tests.
"""

from __future__ import annotations

import pytest

from ripdoctor.audio import astats
from ripdoctor.audio.ffprobe import DEFAULT_RATE, sample_rate, true_duration
from ripdoctor.audio.runner import FakeRunner, ToolFailed
from ripdoctor.core.envelope import DB_MIN, Envelope

# --------------------------------------------------------------- the rate


def test_the_rate_is_read_from_the_file() -> None:
    fake = FakeRunner().expect("ffprobe", stdout=b"44100\n")
    assert sample_rate(fake, "a.flac") == 44100
    argv = fake.argv_for("ffprobe")
    assert "stream=sample_rate" in argv, "asked for the wrong entry"
    assert argv[-1] == "a.flac"


@pytest.mark.parametrize("rate", [8000, 44100, 48000, 96000, 192000, 384000])
def test_every_real_rate_survives(rate: int) -> None:
    fake = FakeRunner().expect("ffprobe", stdout=str(rate).encode())
    assert sample_rate(fake, "a.flac") == rate


@pytest.mark.parametrize("junk", [b"", b"lots\n", b"0", b"7999", b"400000", b"-1"])
def test_an_unusable_answer_falls_back_rather_than_propagating(junk: bytes) -> None:
    """A nonsense rate would silently stretch the whole time axis."""
    fake = FakeRunner().expect("ffprobe", stdout=junk)
    assert sample_rate(fake, "a.flac") == DEFAULT_RATE


def test_the_fallback_is_the_callers_to_choose() -> None:
    fake = FakeRunner().expect("ffprobe", stdout=b"junk")
    assert sample_rate(fake, "a.flac", default=96000) == 96000


# ----------------------------------------------------------- the duration


def test_the_duration_comes_from_decoding() -> None:
    """A capture ended with a signal has no usable header."""
    fake = FakeRunner().expect(
        "-progress",
        stdout=b"out_time_us=1200000\nsomething=else\nout_time_us=1325056000\n",
    )
    assert true_duration(fake, "side-a.flac") == 1325.056


def test_the_audio_stream_is_mapped_explicitly() -> None:
    """Cover art is a video stream.

    Without the map, ffmpeg muxes the picture alongside the audio and progress
    tracks a one-frame still, so the answer comes back 0.000 for any file
    carrying art. Raw sides have none, which is why it stayed hidden until
    something read a file from the library.
    """
    fake = FakeRunner().expect("-progress", stdout=b"out_time_us=100000\n")
    true_duration(fake, "tagged.flac")
    argv = fake.argv_for("-progress")
    assert "-map" in argv and "0:a:0" in argv


def test_both_progress_spellings_are_read() -> None:
    """ffmpeg emits out_time_ms holding microseconds. The name is a lie."""
    fake = FakeRunner().expect("-progress", stdout=b"out_time_ms=600000000\n")
    assert true_duration(fake, "a.flac") == 600.0


def test_the_largest_reading_wins_not_the_last() -> None:
    """Progress output is not guaranteed monotonic at the tail."""
    fake = FakeRunner().expect(
        "-progress", stdout=b"out_time_us=900000000\nout_time_us=12000\n"
    )
    assert true_duration(fake, "a.flac") == 900.0


def test_unparseable_progress_lines_are_skipped() -> None:
    fake = FakeRunner().expect(
        "-progress", stdout=b"out_time_us=N/A\nout_time_us=5000000\nout_time_us=\n"
    )
    assert true_duration(fake, "a.flac") == 5.0


def test_a_file_with_no_progress_at_all_reads_zero() -> None:
    assert true_duration(FakeRunner(), "empty.flac") == 0.0


# ------------------------------------------------------- the astats parser


def frames(*rows: str) -> str:
    out = []
    for i, row in enumerate(rows):
        out.append(f"frame:{i} pts:{i * 2400} pts_time:{i * 0.05}")
        out.extend(row.splitlines())
    return "\n".join(out)


def test_levels_come_back_one_per_window_in_order() -> None:
    text = frames(
        "lavfi.astats.Overall.RMS_level=-25.5\nlavfi.astats.Overall.Peak_level=-10.0",
        "lavfi.astats.Overall.RMS_level=-70.2\nlavfi.astats.Overall.Peak_level=-60.0",
    )
    got = astats.parse(text, (astats.RMS, astats.PEAK))
    assert got[astats.RMS] == [-25.5, -70.2]
    assert got[astats.PEAK] == [-10.0, -60.0]


@pytest.mark.parametrize("raw", ["-nan", "nan", "NaN", "-inf", "inf", "", "lots"])
def test_a_non_finite_reading_becomes_the_floor(raw: str) -> None:
    """ffmpeg prints -nan for a window of digital silence, and it is a reading.

    Catching only ValueError does not handle it: float("-nan") succeeds. The
    resulting NaN compares false against everything and makes the order
    undefined the moment the lane is sorted for a percentile.
    """
    text = frames(f"lavfi.astats.Overall.RMS_level={raw}")
    got = astats.parse(text, (astats.RMS,))[astats.RMS]
    assert got == [DB_MIN], f"{raw!r} survived as {got!r}"


def test_a_lane_carrying_a_nan_would_be_unsortable() -> None:
    """Why the floor rather than passing it on: this is what it protects."""
    text = frames(
        "lavfi.astats.Overall.RMS_level=-20.0",
        "lavfi.astats.Overall.RMS_level=-nan",
        "lavfi.astats.Overall.RMS_level=-80.0",
    )
    levels = astats.parse(text, (astats.RMS,))[astats.RMS]
    assert all(v == v for v in levels), "a NaN reached the lane"
    env = Envelope(tuple(levels), 0.05)
    assert env.percentile(0.85) == -20.0


def test_a_window_missing_a_key_still_produces_a_reading() -> None:
    """Lanes must stay the same length or nothing lines up with the audio."""
    text = frames(
        "lavfi.astats.Overall.RMS_level=-25.0\nlavfi.astats.Overall.Peak_level=-9.0",
        "lavfi.astats.Overall.RMS_level=-30.0",
    )
    got = astats.parse(text, (astats.RMS, astats.PEAK))
    assert len(got[astats.RMS]) == len(got[astats.PEAK]) == 2
    assert got[astats.PEAK][1] == DB_MIN


def test_a_spliced_frame_header_cannot_size_anything() -> None:
    """The line that once cost 26 GB and a running capture.

    Two writers to one pipe interleaved and merged two frame headers:

        frame:18889791200 pts_time:933.15

    The old parser sized its output from that index, tried to allocate 18.9
    billion entries, was OOM-killed, and systemd's restart took the arecord
    running inside the same service with it. Frames are counted by appending
    now, so an absurd index is just a frame boundary.
    """
    text = (
        "frame:0 pts_time:0.0\n"
        "lavfi.astats.Overall.RMS_level=-25.0\n"
        "frame:18889791200 pts_time:933.15\n"
        "lavfi.astats.Overall.RMS_level=-30.0\n"
    )
    got = astats.parse(text, (astats.RMS,))
    assert got[astats.RMS] == [-25.0, -30.0], "an index was trusted for a size"


def test_unrelated_lines_are_ignored() -> None:
    text = (
        "frame:0 pts_time:0.0\n"
        "lavfi.astats.Overall.RMS_level=-25.0\n"
        "lavfi.astats.Overall.Flat_factor=0.0\n"
        "lavfi.astats.1.RMS_level=-99.0\n"
        "size=N/A time=00:00:01.00\n"
    )
    got = astats.parse(text, (astats.RMS,))
    assert got[astats.RMS] == [-25.0], "a per-channel key leaked into the overall lane"


def test_output_with_no_frames_yields_nothing() -> None:
    assert astats.parse("", (astats.RMS,))[astats.RMS] == []


def test_a_frame_whose_value_is_unreadable_still_occupies_its_window() -> None:
    """Skipping it would shorten the lane and shift every later window by 50 ms.

    A corrupt line is exactly what the interleaving defect produced, so this is
    not a hypothetical input.
    """
    text = frames(
        "lavfi.astats.Overall.RMS_level=-20.0",
        "lavfi.astats.Overall.RMS_level=",
        "lavfi.astats.Overall.Flat_factor=0.0",
        "lavfi.astats.Overall.RMS_level=-80.0",
    )
    got = astats.parse(text, (astats.RMS,))[astats.RMS]
    assert len(got) == 4, "a window was dropped and the lane no longer lines up"
    assert got == [-20.0, DB_MIN, DB_MIN, -80.0]


# ------------------------------------------------------------- the chain


def measured(fake: FakeRunner) -> list[tuple[str, ...]]:
    return [c for c in fake.calls if c[0] == "ffmpeg" and "-af" in c]


def build_fake(rate: bytes = b"48000") -> FakeRunner:
    body = frames(
        "lavfi.astats.Overall.RMS_level=-25.0\nlavfi.astats.Overall.Peak_level=-9.0",
        "lavfi.astats.Overall.RMS_level=-70.0\nlavfi.astats.Overall.Peak_level=-55.0",
    ).encode()
    return FakeRunner().expect("ffprobe", stdout=rate).expect("-af", stdout=body)


def test_the_window_is_counted_in_samples_at_the_files_own_rate() -> None:
    """asetnsamples counts samples, so the rate decides the window length."""
    fake = build_fake(rate=b"96000")
    astats.lanes(fake, "a.flac")
    for argv in measured(fake):
        af = argv[argv.index("-af") + 1]
        assert "asetnsamples=n=4800" in af, af


def test_the_band_lane_is_filtered_and_the_others_are_not() -> None:
    fake = build_fake()
    astats.lanes(fake, "a.flac")
    chains = [a[a.index("-af") + 1] for a in measured(fake)]
    assert len(chains) == 2, "one pass per lane group"
    assert any("highpass=f=1000" in c and "lowpass=f=3000" in c for c in chains)
    assert any("highpass" not in c for c in chains), "the full lane was filtered"


def test_one_metadata_writer_per_pass() -> None:
    """Two writers to one pipe is what interleaved and cost the capture."""
    fake = build_fake()
    astats.lanes(fake, "a.flac")
    for argv in measured(fake):
        af = argv[argv.index("-af") + 1]
        assert af.count("ametadata=print") == 1, af


def test_the_rate_is_measured_once_and_shared() -> None:
    """Two passes measuring separately could disagree about the grid."""
    fake = build_fake()
    astats.lanes(fake, "a.flac")
    assert sum(1 for c in fake.calls if c[0] == "ffprobe") == 1


def test_a_supplied_rate_skips_probing() -> None:
    fake = build_fake()
    astats.lanes(fake, "a.flac", rate=44100)
    assert not [c for c in fake.calls if c[0] == "ffprobe"]
    af = measured(fake)[0][measured(fake)[0].index("-af") + 1]
    assert "asetnsamples=n=2205" in af


def test_the_lanes_share_a_grid() -> None:
    lanes = astats.lanes(build_fake(), "a.flac")
    assert len(lanes.full) == len(lanes.peak) == len(lanes.band) == 2
    assert lanes.window == pytest.approx(0.05)


def test_a_failed_measurement_is_raised_not_returned_empty() -> None:
    """An empty envelope would be read as a side of pure silence."""
    fake = (
        FakeRunner()
        .expect("ffprobe", stdout=b"48000")
        .expect("-af", returncode=1, stderr=b"Invalid data found when processing input")
    )
    with pytest.raises(ToolFailed, match="Invalid data found"):
        astats.lanes(fake, "broken.flac")
