"""Capture: the argv, the guards, and the verdict.

Actually recording needs a sound card and is named in the hardware list rather
than faked. Everything that decides what to record, what to refuse and what a
capture turned out to be is here and runs anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio import capture as C
from ripdoctor.audio.runner import FakeProcess, FakeRunner, ToolFailed
from ripdoctor.core.meter import verdict
from tests import tape


def fmt(**kw: object) -> C.Format:
    return C.Format(**kw)  # type: ignore[arg-type]


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# ---------------------------------------------------------- the device


def test_an_unset_device_says_which_command_lists_them() -> None:
    with pytest.raises(C.CaptureError, match="ripdoctor devices"):
        C.check_device("")


@pytest.mark.parametrize("good", ["hw:Rx,0", "hw:CODEC,0", "default", "plughw:1,0"])
def test_real_device_names_are_accepted(good: str) -> None:
    assert C.check_device(good) == good


@pytest.mark.parametrize("bad", ["hw:Rx,0; rm -rf /", "$(id)", "a b", "hw:'x'"])
def test_an_odd_looking_device_name_is_refused(bad: str) -> None:
    """Nothing is interpolated anywhere, but a name like this is a mistake and
    saying so beats passing it on."""
    with pytest.raises(C.CaptureError, match="odd-looking"):
        C.check_device(bad)


# ------------------------------------------------------------- the argv


def test_the_capture_is_written_as_wav_not_piped_to_an_encoder() -> None:
    """Ending a pipe into an encoder with a signal never closes the stream.

    The header is then never backfilled: the file reports no duration, fails
    verification, and every tool that reads it needs a special case.
    """
    argv = C.capture_argv("hw:Rx,0", "/raw/a.wav", fmt())
    assert flag(argv, "-t") == "wav"
    assert "|" not in " ".join(argv) and "flac" not in argv


def test_the_format_is_taken_from_configuration_not_assumed() -> None:
    argv = C.capture_argv("hw:Rx,0", "/x.wav", fmt(rate=96000, sample_format="S24_3LE"))
    assert flag(argv, "-f") == "S24_3LE"
    assert flag(argv, "-r") == "96000"
    assert flag(argv, "-c") == "2"


def test_an_open_ended_capture_has_no_duration() -> None:
    """A side ends when the record does, not after a number of seconds."""
    assert "-d" not in C.capture_argv("hw:Rx,0", "/x.wav", fmt())


def test_a_test_capture_is_bounded_at_both_ends() -> None:
    short = C.test_capture_argv("hw:Rx,0", "/x.wav", fmt(), seconds=0.1)
    long = C.test_capture_argv("hw:Rx,0", "/x.wav", fmt(), seconds=6000)
    assert float(flag(short, "-d")) == C.MIN_TEST_SECONDS
    assert float(flag(long, "-d")) == C.MAX_TEST_SECONDS


def test_the_encode_re_encodes_rather_than_renaming() -> None:
    argv = C.encode_argv("/x.wav", "/x.flac")
    assert flag(argv, "-c:a") == "flac" and "copy" not in argv


# ---------------------------------------------------- starting and ending


def test_a_capture_in_progress_is_named_so_nothing_reads_it(
    tmp_path: Path,
) -> None:
    """Tools scan for side-*.flac. A partial file matching that pattern is one
    an analysis pass will read as a whole side."""
    partial = C.partial_path(tmp_path, "a")
    assert partial.name.startswith(".")
    assert not partial.name.endswith(".flac")
    assert partial != C.finished_path(tmp_path, "a")


def test_starting_a_capture_makes_the_album_directory(tmp_path: Path) -> None:
    album = tmp_path / "album"
    fake = FakeRunner(installed={"arecord"})
    proc, dest = C.start(fake, "hw:Rx,0", album, "a", fmt())
    assert album.is_dir() and dest.parent == album
    assert proc.poll() is None, "the capture ended immediately"
    assert fake.calls[0][0] == "arecord"


def test_a_side_that_already_exists_is_not_overwritten(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    C.finished_path(album, "a").write_bytes(b"fLaC")
    with pytest.raises(C.CaptureError, match="already exists"):
        C.start(FakeRunner(installed={"arecord"}), "hw:Rx,0", album, "a", fmt())


def test_finishing_encodes_and_clears_the_partial(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    wav = C.partial_path(album, "a")
    wav.write_bytes(b"RIFF" + b"\x00" * 4000)

    fake = FakeRunner()
    out = C.finish(fake, album, "a")
    assert out.name == "side-a.flac"
    assert not wav.exists(), "the partial file was left behind"
    assert any("-c:a" in c for c in fake.calls)


def test_finishing_an_empty_capture_is_refused(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    C.partial_path(album, "a").write_bytes(b"RIFF")
    with pytest.raises(C.CaptureError, match="nothing was captured"):
        C.finish(FakeRunner(), album, "a")


def test_a_failed_encode_leaves_the_capture_alone(tmp_path: Path) -> None:
    """The wav is the only copy until the encode succeeds."""
    album = tmp_path / "album"
    album.mkdir()
    wav = C.partial_path(album, "a")
    wav.write_bytes(b"RIFF" + b"\x00" * 4000)

    fake = FakeRunner().expect("ffmpeg", returncode=1, stderr=b"Invalid data")
    with pytest.raises(ToolFailed):
        C.finish(fake, album, "a")
    assert wav.exists(), "the only copy was deleted after a failed encode"


# ------------------------------------------------------------- salvage


def test_an_interrupted_capture_is_found(tmp_path: Path) -> None:
    """A capture that ended badly is still most of a side, and a side is twenty
    minutes of somebody's evening."""
    album = tmp_path / "album"
    album.mkdir()
    C.partial_path(album, "b").write_bytes(b"RIFF" + b"\x00" * 8000)
    C.finished_path(album, "a").write_bytes(b"fLaC" + b"\x00" * 8000)

    found = C.salvageable(album)
    assert [C.letter_of(p) for p in found] == ["b"]


def test_an_empty_partial_is_not_offered_as_salvage(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    C.partial_path(album, "b").write_bytes(b"RIFF")
    assert C.salvageable(album) == []


def test_salvage_of_a_directory_that_is_not_there_is_empty(tmp_path: Path) -> None:
    assert C.salvageable(tmp_path / "nope") == []


# ------------------------------------------------------------- verdict


@pytest.mark.parametrize(
    ("full_rms", "full_peak", "band_rms", "ok", "expect"),
    [
        (-24.0, -6.0, -40.0, True, "music"),
        (-30.0, -12.0, -77.0, False, "wrong input"),
        (-95.0, -88.0, -99.0, False, "noise floor"),
        (-55.0, -45.0, -65.0, False, "quiet"),
    ],
)
def test_the_four_states_are_told_apart(
    full_rms: float, full_peak: float, band_rms: float, ok: bool, expect: str
) -> None:
    v = verdict(full_rms, full_peak, band_rms)
    assert v.ok is ok
    assert expect in v.summary


def test_a_wrong_input_is_not_reported_as_no_signal() -> None:
    """Order matters. Checking the floor first sends somebody to look at the
    cable instead of the input selector."""
    v = verdict(-72.0, -35.0, -80.0)
    assert "wrong input" in v.summary
    assert "no signal" not in v.summary


def test_levels_are_read_despite_the_logger_prefix() -> None:
    """astats writes through ffmpeg's logger, so every line is prefixed.

    A pattern anchored to the start of a line matched nothing, and the fallback
    then reported every capture as pure noise floor - the one verdict that must
    never be wrong.
    """
    text = (
        "[Parsed_astats_0 @ 0x55f] RMS level dB: -24.500000\n"
        "[Parsed_astats_0 @ 0x55f] Peak level dB: -6.100000\n"
    )
    assert C.read_stat(text, "RMS level dB") == -24.5
    assert C.read_stat(text, "Peak level dB") == -6.1


def test_the_last_reading_wins_over_a_per_channel_one() -> None:
    text = (
        "[Parsed_astats_0 @ 0x1] RMS level dB: -30.000000\n"
        "[Parsed_astats_0 @ 0x1] RMS level dB: -24.000000\n"
    )
    assert C.read_stat(text, "RMS level dB") == -24.0


def test_an_unreadable_reading_is_absent_rather_than_zero() -> None:
    assert C.read_stat("nothing here", "RMS level dB") is None
    assert C.read_stat("RMS level dB: -inf", "RMS level dB") is None


def test_a_capture_is_judged_from_both_lanes() -> None:
    fake = (
        FakeRunner()
        .expect(
            lambda a: any("highpass" in x for x in a),
            stderr=b"[astats] RMS level dB: -41.000000\n",
        )
        .expect(
            "astats",
            stderr=b"[astats] RMS level dB: -24.000000\n"
            b"[astats] Peak level dB: -6.000000\n",
        )
    )
    v = C.judge(fake, "probe.wav")
    assert v.ok and "music" in v.summary
    assert v.band_rms == -41.0 and v.full_peak == -6.0


# ------------------------------------------------------- reading the file


def test_the_format_is_read_from_the_header_not_assumed(tmp_path: Path) -> None:
    """A chain is not permanently whatever it was when this was written. A
    capture metered at the wrong rate measures 2-6 kHz and calls it 1-3."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(tape.header(rate=96000, channels=2, bits=24))
    fmt = C.wav_format(wav, C.Format(rate=48000, sample_format="S16_LE"))
    assert fmt.rate == 96000 and fmt.width == 3


@pytest.mark.parametrize(("bits", "width"), [(16, 2), (24, 3), (32, 4)])
def test_every_capture_width_is_recognised(
    tmp_path: Path, bits: int, width: int
) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(tape.header(bits=bits))
    assert C.wav_format(wav, C.Format()).width == width


def test_a_file_that_is_not_a_wav_falls_back_rather_than_guessing(
    tmp_path: Path,
) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"not a riff header at all, but long enough to read" * 2)
    assert C.wav_format(wav, C.Format(rate=44100)).rate == 44100
    assert C.wav_format(tmp_path / "absent.wav", C.Format(rate=44100)).rate == 44100


def test_an_absurd_rate_in_the_header_is_not_believed(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(tape.header(rate=3))
    assert C.wav_format(wav, C.Format(rate=48000)).rate == 48000


def test_the_meter_reads_the_file_the_recorder_is_writing(tmp_path: Path) -> None:
    """ALSA gives one program the input. A meter needing its own stream would
    be a meter that could not run during a capture."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(tape.header())
    with wav.open("ab") as f:
        f.write(tape.block(1500.0, 48000, 3, 2, 0.4))

    measured = C.meter(wav, C.Format())
    assert measured is not None
    levels, fmt = measured
    assert fmt.rate == 48000
    assert levels.band > -20.0, "a 1500 Hz tone did not show in the band lane"


def test_a_capture_with_less_than_a_block_yet_reads_nothing(tmp_path: Path) -> None:
    """Not a number, and certainly not a floor: too early is not silence."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(tape.header())
    assert C.meter(wav, C.Format()) is None
    assert C.meter(tmp_path / "absent.wav", C.Format()) is None


def test_only_the_tail_is_read(tmp_path: Path) -> None:
    """Whatever the side has done so far, the meter shows what it is doing
    now."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(tape.header())
    with wav.open("ab") as f:
        f.write(tape.block(1500.0, 48000, 3, 2, 0.4))
        f.write(tape.block(None, 48000, 3, 2, 0.0))

    measured = C.meter(wav, C.Format())
    assert measured is not None
    assert measured[0].band < -100.0, "the meter is still showing the loud part"


# ------------------------------------------------------------- overruns


def test_overruns_are_counted_with_the_time_they_cost(tmp_path: Path) -> None:
    log = tmp_path / "a.log"
    log.write_text(
        "overrun!!! (at least 8.235 ms long)\n"
        "arecord: something else entirely\n"
        "overrun!!! (at least 12.100 ms long)\n"
    )
    assert C.overruns(log) == (2, 20.3)


def test_a_log_that_is_not_there_is_no_complaints(tmp_path: Path) -> None:
    assert C.overruns(tmp_path / "absent.log") == (0, 0.0)


# -------------------------------------------------------------- stopping


def test_a_capture_is_interrupted_so_the_header_is_backfilled() -> None:
    """Killed, arecord leaves a file that reports no duration."""
    proc = FakeProcess()
    C.stop(proc)
    assert proc.interrupted and not proc.killed


def test_a_capture_that_will_not_stop_is_killed() -> None:
    class Stubborn(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                raise TimeoutError("still going")
            return 0

    proc = Stubborn()
    C.stop(proc)
    assert proc.killed, "a recorder that ignored the interrupt was left running"


def test_a_capture_that_already_ended_is_left_alone() -> None:
    proc = FakeProcess(exit_after=1)
    proc.poll()
    C.stop(proc)
    assert not proc.interrupted and not proc.killed
