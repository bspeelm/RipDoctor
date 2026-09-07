"""Finding the same music in two captures, with the audio faked.

The arithmetic is tested in test_xcorr; these are about the two things that
went wrong when it was wired to real files - the window length and the second
probe's search centre.
"""

from __future__ import annotations

import math

import pytest

from ripdoctor.audio import align as AL
from ripdoctor.audio.runner import FakeRunner
from ripdoctor.core.envelope import DB_MIN
from ripdoctor.core.xcorr import AlignError

RATE = 48000


def levels(values: list[float]) -> bytes:
    return "\n".join(f"lavfi.astats.Overall.RMS_level={v}" for v in values).encode()


def music(seconds: float, window: float, *, shift: float = 0.0) -> list[float]:
    """A repeatable, non-flat series: silence correlates with anything."""
    n = int(seconds / window)
    return [
        round(-40.0 + 20.0 * math.sin((i * window + shift) * 0.7), 2) for i in range(n)
    ]


# ---------------------------------------------------------- the envelope


def test_the_window_is_a_sample_count_from_the_file_itself() -> None:
    """Assuming 48 kHz made every window half as long on a 96 kHz capture, so a
    thirty-second needle met fifteen seconds of hay and the match fell just
    under the floor - refusing a side that could have been aligned."""
    argv = AL.envelope_argv("/a.flac", 0.0, 30.0, int(96000 * AL.COARSE_WINDOW))
    chain = argv[argv.index("-af") + 1]
    assert "asetnsamples=n=19200" in chain


def test_the_seek_happens_before_the_input() -> None:
    """`-ss` after `-i` decodes everything up to the point instead of seeking."""
    argv = AL.envelope_argv("/a.flac", 100.0, 130.0, 9600)
    assert argv.index("-ss") < argv.index("-i")


def test_an_unreadable_reading_becomes_the_floor_not_a_gap() -> None:
    fake = FakeRunner().expect(
        "ffmpeg", stdout=levels([-30.0]) + b"\nlavfi.astats.Overall.RMS_level=-nan"
    )
    got = AL.envelope(fake, "/a.flac", 0.0, 1.0, 0.2, RATE)
    assert len(got) == 2 and got[1] == DB_MIN


def test_output_with_no_readings_is_empty_rather_than_wrong() -> None:
    fake = FakeRunner().expect("ffmpeg", stdout=b"nothing here")
    assert AL.envelope(fake, "/a.flac", 0.0, 1.0, 0.2, RATE) == []


# -------------------------------------------------------------- probing


class Audio(FakeRunner):
    """Two files, one of which is the other shifted in time."""

    def __init__(self, shift: float = 0.0, duration: float = 1200.0) -> None:
        super().__init__()
        self.shift = shift
        self.duration = duration

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        self.calls.append(tuple(args))
        if args[0] == "ffprobe":
            return self._reply(args, str(RATE).encode())
        t0 = float(args[args.index("-ss") + 1])
        t1 = float(args[args.index("-to") + 1])
        chain = args[args.index("-af") + 1]
        samples = int(chain.split("n=")[1].split(",")[0])
        window = samples / RATE
        offset = 0.0 if args[args.index("-i") + 1] == "/old.flac" else -self.shift
        return self._reply(args, levels(music(t1 - t0, window, shift=t0 + offset)))

    def _reply(self, args, stdout: bytes):  # type: ignore[no-untyped-def]
        from ripdoctor.audio.runner import Result

        return Result(tuple(args), 0, stdout, b"")


def test_a_probe_finds_where_the_music_moved_to() -> None:
    fake = Audio(shift=12.0)
    found = AL.probe(
        fake, "/old.flac", "/new.flac", 300.0, 300.0, AL.COARSE_HUNT, 1200.0
    )
    assert found.r > 0.9
    assert found.shift == pytest.approx(12.0, abs=0.1)


def windows(fake: Audio, path: str) -> list[tuple[float, float]]:
    """Every span read from one file, in order."""
    return [
        (float(c[c.index("-ss") + 1]), float(c[c.index("-to") + 1]))
        for c in fake.calls
        if c[0] == "ffmpeg" and c[c.index("-i") + 1] == path
    ]


def test_the_second_probe_looks_where_the_first_landed() -> None:
    """Searching the whole side again invites a match on the wrong passage, and
    taking the needle from the wrong timeline doubled the measured shift."""
    fake = Audio(shift=60.0)
    AL.fit_side(fake, "/old.flac", "/new.flac", 20.0, 1100.0, 1200.0)
    asked = [lo for lo, _hi in windows(fake, "/old.flac")]
    # The needles still come off the old side's own timeline, at 20% and 80%.
    assert round(asked[0]) == 236 and round(asked[2]) == 884

    # The second search is centred on where the first probe landed, not on the
    # old time: 884 + 60, not 884.
    searched = [lo for lo, _hi in windows(fake, "/new.flac")]
    assert searched[2] > 884.0 + 60.0 - AL.COARSE_HUNT - 1


# ---------------------------------------------------------------- fitting


def test_a_re_rip_is_fitted_and_checked_at_the_midpoint() -> None:
    fake = Audio(shift=12.0)
    transform, probes, miss = AL.fit_side(
        fake, "/old.flac", "/new.flac", 20.0, 1100.0, 1200.0
    )
    assert transform.offset == pytest.approx(12.0, abs=0.2)
    assert len(probes) == 3 and abs(miss) < 0.2


def test_a_side_too_short_for_two_probes_says_so() -> None:
    fake = Audio()
    with pytest.raises(AlignError, match="too short"):
        AL.fit_side(fake, "/old.flac", "/new.flac", 0.0, 60.0, 60.0)


def test_music_that_does_not_match_is_refused() -> None:
    """Not the same side, or a bad capture. Either way it is not a fit."""
    fake = (
        FakeRunner()
        .expect("ffprobe", stdout=b"48000")
        .expect("ffmpeg", stdout=levels([-40.0] * 200))
    )
    with pytest.raises(AlignError):
        AL.fit_side(fake, "/old.flac", "/new.flac", 20.0, 1100.0, 1200.0)


# ------------------------------------------------------------------ punch


def test_a_short_track_gets_an_offset_and_says_the_scale_was_assumed() -> None:
    """Honest rather than convenient: over a sixty-second track a 0.1% platter
    difference is sixty milliseconds, which lands inside the gap it cuts at."""
    fake = Audio(shift=-880.0, duration=200.0)
    transform, probes, assumed = AL.fit_punch(
        fake, "/old.flac", "/new.flac", 900.0, 960.0, 200.0
    )
    assert assumed and len(probes) == 1 and transform.scale == 1.0
    assert transform.apply(900.0) == pytest.approx(20.0, abs=1.0)


def test_a_track_too_short_to_correlate_is_refused() -> None:
    with pytest.raises(AlignError, match="too short"):
        AL.fit_punch(Audio(), "/old.flac", "/new.flac", 100.0, 104.0, 200.0)


def test_a_long_track_measures_the_scale_rather_than_assuming_it() -> None:
    fake = Audio(shift=-800.0, duration=400.0)
    _transform, probes, assumed = AL.fit_punch(
        fake, "/old.flac", "/new.flac", 800.0, 1000.0, 400.0
    )
    assert not assumed and len(probes) == 2


def test_the_first_punch_probe_searches_the_whole_capture() -> None:
    """The offset is about minus nine hundred seconds. A bounded hunt computes
    a range that runs backwards and finds nothing at all."""
    fake = Audio(shift=-880.0, duration=200.0)
    AL.fit_punch(fake, "/old.flac", "/new.flac", 900.0, 960.0, 200.0)
    first = windows(fake, "/new.flac")[0]
    assert first == (0.0, 200.0), f"the punch was searched over {first}"
