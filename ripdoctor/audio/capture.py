"""Recording a side, and the two guards that keep a bad one from costing twenty
minutes."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.audio.runner import Process, Runner
from ripdoctor.core.meter import Verdict, verdict

# A device name is a small alphabet. Anything else is a mistake or an attempt,
# and either way the answer is to say so rather than to pass it on.
_DEVICE = re.compile(r"^[A-Za-z0-9:,_./-]+$")

TEST_SECONDS = 20.0
MIN_TEST_SECONDS, MAX_TEST_SECONDS = 3.0, 60.0

# A capture in progress is written under a name nothing else will pick up. The
# predecessor's own tools scan for side-*.flac, and a partial file matching that
# pattern is one an analysis pass will happily read as a whole side.
PARTIAL = ".side-{letter}.capturing.wav"
FINISHED = "side-{letter}.flac"


class CaptureError(Exception):
    """A capture could not be started, or should not be."""


def check_device(device: str) -> str:
    if not device:
        raise CaptureError("no capture device is set - run `ripdoctor devices`")
    if not _DEVICE.match(device):
        raise CaptureError(f"refusing an odd-looking device name: {device!r}")
    return device


@dataclass(frozen=True, slots=True)
class Format:
    rate: int = 48000
    channels: int = 2
    sample_format: str = "S24_3LE"


def capture_argv(
    device: str, dest: str, fmt: Format, seconds: float | None = None
) -> list[str]:
    """Record to WAV, not to a FLAC encoder.

    Piping into an encoder and ending the capture with a signal leaves the
    stream never closed: the header is never backfilled, so the file reports no
    duration, fails verification, and every tool that reads it has to work
    around it. WAV is written with a header that can be repaired, and the
    encode happens once the length is known.
    """
    argv = [
        "arecord",
        "-D",
        device,
        "-f",
        fmt.sample_format,
        "-r",
        str(int(fmt.rate)),
        "-c",
        str(int(fmt.channels)),
        "-t",
        "wav",
    ]
    if seconds is not None:
        argv += ["-d", str(int(seconds))]
    argv.append(dest)
    return argv


def encode_argv(wav: str, flac: str) -> list[str]:
    """Encode a finished capture. Re-encoded, never renamed."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        wav,
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        flac,
    ]


def partial_path(album_dir: str | Path, letter: str) -> Path:
    return Path(album_dir) / PARTIAL.format(letter=letter)


def finished_path(album_dir: str | Path, letter: str) -> Path:
    return Path(album_dir) / FINISHED.format(letter=letter)


def start(
    runner: Runner, device: str, album_dir: str | Path, letter: str, fmt: Format
) -> tuple[Process, Path]:
    """Begin recording one side. Returns the running process and its file."""
    check_device(device)
    Path(album_dir).mkdir(parents=True, exist_ok=True)
    dest = partial_path(album_dir, letter)
    if finished_path(album_dir, letter).exists():
        raise CaptureError(f"side {letter} already exists; move it first")
    return runner.start(capture_argv(device, str(dest), fmt)), dest


def finish(runner: Runner, album_dir: str | Path, letter: str) -> Path:
    """Encode a finished capture and remove the partial file."""
    wav = partial_path(album_dir, letter)
    if not wav.is_file() or wav.stat().st_size < 1024:
        raise CaptureError(f"nothing was captured to {wav}")
    flac = finished_path(album_dir, letter)
    runner.run(encode_argv(str(wav), str(flac)), timeout=1800).require()
    wav.unlink()
    return flac


def salvageable(album_dir: str | Path) -> list[Path]:
    """Partial captures left behind by an interrupted session.

    A capture that ended badly is still most of a side, and a side is twenty
    minutes of somebody's evening. Finding these is what makes the difference
    between an interruption and a lost record.
    """
    directory = Path(album_dir)
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.iterdir()
        if p.name.startswith(".side-")
        and p.name.endswith(".capturing.wav")
        and p.stat().st_size >= 1024
    )


def letter_of(partial: Path) -> str:
    return partial.name[len(".side-") : -len(".capturing.wav")]


def test_capture_argv(device: str, dest: str, fmt: Format, seconds: float) -> list[str]:
    bounded = max(MIN_TEST_SECONDS, min(MAX_TEST_SECONDS, seconds))
    return capture_argv(device, dest, fmt, seconds=bounded)


# astats writes through ffmpeg's logger, so every line carries a "[Parsed_astats
# @ 0x...] " prefix. A pattern anchored to the start of a line matches nothing,
# and the fallback then reported every capture as pure noise floor - the one
# verdict that must never be wrong. Unanchored, and the last match wins.
_STAT = r"{key}:\s*(-?[\d.]+|-?inf)"


def read_stat(text: str, key: str) -> float | None:
    """One level, or nothing.

    `-inf` is what a digitally silent file reports, and float() accepts it - the
    same trap as ADR-027. A non-finite reading is no reading, and the caller
    supplies the floor rather than carrying an infinity into a comparison.
    """
    hits = re.findall(_STAT.format(key=re.escape(key)), text)
    if not hits:
        return None
    try:
        value = float(hits[-1])
    except ValueError:
        return None
    return round(value, 1) if math.isfinite(value) else None


def whole_file_argv(path: str, band: bool = False) -> list[str]:
    prefix = ["highpass=f=1000", "lowpass=f=3000"] if band else []
    chain = [*prefix, "astats=metadata=0:measure_perchannel=none"]
    return [
        "ffmpeg",
        "-v",
        "info",
        "-i",
        path,
        "-af",
        ",".join(chain),
        "-f",
        "null",
        "-",
    ]


def judge(runner: Runner, path: str) -> Verdict:
    """Measure a short capture whole and say what it is."""
    wide = runner.run(whole_file_argv(path), timeout=120)
    narrow = runner.run(whole_file_argv(path, band=True), timeout=120)
    return verdict(
        read_stat(wide.err, "RMS level dB") or -120.0,
        read_stat(wide.err, "Peak level dB") or -120.0,
        read_stat(narrow.err, "RMS level dB") or -120.0,
    )
