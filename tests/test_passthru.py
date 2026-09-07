"""Listening to the input, with nothing playing.

What is asserted here is the argv and the cleanup. Whether opus sounds right is
not something a test can say.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.audio import passthru as PT
from ripdoctor.audio.capture import CaptureError, Format
from ripdoctor.audio.runner import FakeRunner


def test_the_card_is_read_directly_rather_than_through_a_pipe() -> None:
    """One process. Two, with a pipe between them, is plumbing that can block."""
    argv = PT.device_argv("hw:Rx,0", Format(rate=96000, channels=2))
    assert argv[0] == "ffmpeg" and "alsa" in argv
    assert argv[argv.index("-i") + 1] == "hw:Rx,0"
    assert argv[argv.index("-sample_rate") + 1] == "96000"


def test_an_odd_looking_device_is_refused() -> None:
    with pytest.raises(CaptureError):
        PT.device_argv("hw:Rx,0; rm -rf /", Format())


def test_a_capture_in_progress_is_read_at_playing_speed(tmp_path: Path) -> None:
    """Without that ffmpeg empties the file in a second: what a listener hears
    is the last few seconds at high speed, then silence."""
    argv = PT.tail_argv(tmp_path / "a.wav")
    assert "-re" in argv and argv.index("-re") < argv.index("-i")


def test_the_stream_is_encoded_for_latency_not_for_keeping() -> None:
    """The delay between the needle and the headphones is the whole point."""
    argv = PT.device_argv("hw:Rx,0", Format())
    assert argv[argv.index("-application") + 1] == "lowdelay"
    assert argv[argv.index("-f") + 1] == "alsa" and argv[-1] == "-"


def test_chunks_come_back_as_the_encoder_writes_them() -> None:
    fake = FakeRunner(output=b"OggS" + b"\x00" * 20000)
    got = b"".join(PT.stream(fake, ["ffmpeg"], chunk=4096))
    assert got.startswith(b"OggS") and len(got) == 20004


def test_the_encoder_is_stopped_when_the_listener_goes_away() -> None:
    """A tab closed mid-stream would otherwise leave ffmpeg holding the card."""
    fake = FakeRunner(output=b"\x00" * 100000)
    chunks = PT.stream(fake, ["ffmpeg"], chunk=16)
    next(chunks)
    chunks.close()
    assert fake.started[0].killed, "the encoder was left running"


def test_an_encoder_that_already_exited_is_not_killed() -> None:
    fake = FakeRunner(output=b"OggS", exit_after=1)
    assert b"".join(PT.stream(fake, ["ffmpeg"])) == b"OggS"
    assert not fake.started[0].killed


# ------------------------------------------------- joining a capture late


def a_capture(tmp_path: Path, seconds: float, fmt: Format) -> Path:
    wav = tmp_path / "side.wav"
    wav.write_bytes(bytes(44 + int(seconds * fmt.rate) * fmt.channels * fmt.width))
    return wav


def test_a_listener_joins_behind_the_write_head(tmp_path: Path) -> None:
    """Starting where the writer is means the two run at the same speed for
    the whole side, so the reader sits permanently at the end of the file. Any
    jitter either way is an end-of-stream, and an Opus decoder resyncing after
    one is a burst of loud static - which is what it sounded like."""
    fmt = Format(rate=96000, channels=2, sample_format="S24_3LE")
    wav = a_capture(tmp_path, 600.0, fmt)
    assert PT.tail_start(wav, fmt) == pytest.approx(600.0 - PT.LAG, abs=0.01)


def test_a_capture_shorter_than_the_cushion_starts_at_the_beginning(
    tmp_path: Path,
) -> None:
    fmt = Format(rate=96000, channels=2, sample_format="S24_3LE")
    assert PT.tail_start(a_capture(tmp_path, 3.0, fmt), fmt) == 0.0


def test_a_capture_that_is_not_there_starts_at_the_beginning(
    tmp_path: Path,
) -> None:
    assert PT.tail_start(tmp_path / "absent.wav", Format()) == 0.0


def test_the_cushion_survives_the_drift_it_exists_for() -> None:
    """ffmpeg paces by its own clock and the card writes by its own. A tenth of
    a per cent over a twenty-minute side is about a second; the cushion has to
    be comfortably more than that."""
    drift_over_a_side = 20 * 60 * 0.001
    assert PT.LAG > drift_over_a_side * 5


def test_the_seek_comes_before_the_input() -> None:
    """After it, ffmpeg decodes everything up to the point instead of seeking -
    which on a twenty-minute side is the delay this exists to remove."""
    argv = PT.tail_argv("/raw/side.wav", 585.0)
    assert argv.index("-ss") < argv.index("-i")
    assert argv[argv.index("-ss") + 1] == "585.000"


def test_a_negative_start_is_not_passed_to_ffmpeg() -> None:
    argv = PT.tail_argv("/raw/side.wav", -3.0)
    assert argv[argv.index("-ss") + 1] == "0.000"
