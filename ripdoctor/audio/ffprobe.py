"""What a file actually is: its rate, and how long it really runs."""

from __future__ import annotations

from ripdoctor.audio.runner import Runner

MIN_RATE, MAX_RATE = 8000, 384000
DEFAULT_RATE = 48000


def sample_rate(runner: Runner, path: str, default: int = DEFAULT_RATE) -> int:
    """The file's real sample rate.

    Guessing costs more than it looks. Window length is counted in samples, so
    assuming 48 kHz on a 44.1 kHz file makes every window 54.4 ms instead of 50 -
    an 8.8 per cent stretch of the envelope's whole time axis, about two minutes
    of drift by the end of a long side. Nothing looks wrong; every marker is
    simply in the wrong place.
    """
    r = runner.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        timeout=60,
    )
    try:
        rate = int(r.text.strip())
    except ValueError:
        return default
    return rate if MIN_RATE <= rate <= MAX_RATE else default


def true_duration(runner: Runner, path: str, timeout: float = 600.0) -> float:
    """Seconds, obtained by decoding. The container header cannot be trusted.

    A capture ended with a signal never has its header backfilled, so it reports
    no duration at all. Decoding is the only answer that is always right.

    `-map 0:a:0` is not optional. An embedded cover picture is a video stream,
    and without the map ffmpeg muxes it alongside the audio, so progress tracks a
    one-frame still and the answer comes back 0.000 for any file carrying art.
    Raw sides have none, which is why this stayed hidden until something read a
    file from the library.
    """
    r = runner.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    seconds = 0.0
    for line in r.text.splitlines():
        key, _, value = line.partition("=")
        if key not in ("out_time_us", "out_time_ms"):
            continue
        try:
            # Both keys are microseconds despite the name on one of them.
            seconds = max(seconds, int(value) / 1e6)
        except ValueError:
            continue
    return round(seconds, 3)
