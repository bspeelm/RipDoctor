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
