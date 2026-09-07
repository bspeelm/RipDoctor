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


# ------------------------------------------------- following a capture


FMT = Format(rate=48000, channels=2, sample_format="S24_3LE")
FRAME = FMT.channels * FMT.width


def a_capture(tmp_path: Path, seconds: float) -> Path:
    wav = tmp_path / "side.wav"
    wav.write_bytes(b"R" * 44 + bytes(int(seconds * FMT.rate) * FRAME))
    return wav


def grow(wav: Path, seconds: float, fill: bytes = b"\x01") -> None:
    with wav.open("ab") as f:
        f.write(fill * (int(seconds * FMT.rate) * FRAME))


def test_the_encoder_is_told_what_is_coming(tmp_path: Path) -> None:
    """There is no header on a pipe, and ffmpeg guessing would guess wrong."""
    argv = PT.pipe_argv(FMT)
    assert argv[argv.index("-f") + 1] == "s24le"
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-i") + 1] == "-"


def test_a_format_that_cannot_be_streamed_is_refused() -> None:
    with pytest.raises(CaptureError):
        PT.pipe_argv(Format(sample_format="S24_LE"))


def test_a_listener_joins_just_behind_the_write_head(tmp_path: Path) -> None:
    """A head start, not a safety margin - the reader waits for the writer
    rather than racing it."""
    wav = a_capture(tmp_path, 600.0)
    at = PT.tail_start(wav, FMT)
    behind = (wav.stat().st_size - at) / FRAME / FMT.rate
    assert behind == pytest.approx(PT.LAG, abs=0.01)


def test_the_start_lands_on_a_frame(tmp_path: Path) -> None:
    """Starting mid-frame swaps the channels and shifts every sample by a byte,
    which sounds like noise rather than like a mistake."""
    wav = a_capture(tmp_path, 7.3)
    assert (PT.tail_start(wav, FMT) - 44) % FRAME == 0


def test_a_capture_shorter_than_the_head_start_begins_at_the_first_sample(
    tmp_path: Path,
) -> None:
    assert PT.tail_start(a_capture(tmp_path, 0.1), FMT) == 44


def test_a_capture_that_is_not_there_begins_at_the_first_sample(
    tmp_path: Path,
) -> None:
    assert PT.tail_start(tmp_path / "absent.wav", FMT) == 44


def test_the_reader_waits_at_the_end_instead_of_stopping_there(
    tmp_path: Path,
) -> None:
    """The whole point. ffmpeg reading the file itself treats end-of-file as
    end of stream, so a monitor keeping pace with the writer ends every time it
    catches up - and a decoder resyncing after that is loud static on a
    recording that is perfect."""
    wav = a_capture(tmp_path, 1.0)
    clock = [0.0]
    added = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if len(added) < 2:
            added.append(True)
            grow(wav, 0.5)

    got = b"".join(
        PT.tail(wav, FMT, lag=1.0, sleep=sleep, now=lambda: clock[0], chunk=4096)
    )
    assert added == [True, True], "it never waited for more"
    assert len(got) == int(1.0 * FMT.rate) * FRAME + 2 * int(0.5 * FMT.rate) * FRAME


def test_the_reader_gives_up_once_the_capture_has_stopped(tmp_path: Path) -> None:
    """A listener held open forever on a side that ended is a process nobody
    closes."""
    wav = a_capture(tmp_path, 0.2)
    clock = [0.0]
    got = b"".join(
        PT.tail(
            wav,
            FMT,
            lag=1.0,
            idle_limit=2.0,
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
            now=lambda: clock[0],
        )
    )
    assert got and clock[0] >= 2.0


def test_what_the_encoder_is_fed_is_the_tail_of_the_capture(
    tmp_path: Path,
) -> None:
    wav = a_capture(tmp_path, 0.5)
    fake = FakeRunner(output=b"OggS" + b"\x00" * 100)
    out = b"".join(
        PT.follow(
            fake,
            wav,
            FMT,
            lag=1.0,
            spawn=lambda work: work(),
            idle_limit=0.0,
            sleep=lambda _s: None,
            now=lambda: 0.0,
        )
    )
    fed = fake.started[0].stdin
    assert fed is not None
    assert fed.getvalue() == (tmp_path / "side.wav").read_bytes()[44:]
    assert out.startswith(b"OggS")


def test_following_stops_the_encoder_when_the_listener_goes_away(
    tmp_path: Path,
) -> None:
    """A tab closed mid-side would otherwise leave ffmpeg running for the life
    of the process."""
    wav = a_capture(tmp_path, 0.5)
    fake = FakeRunner(output=b"\x00" * 100000)
    chunks = PT.follow(
        fake, wav, FMT, spawn=lambda _w: None, sleep=lambda _s: None, now=lambda: 0.0
    )
    next(chunks)
    chunks.close()
    assert fake.started[0].killed
