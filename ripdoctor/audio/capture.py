"""Recording a side, and the two guards that keep a bad one from costing twenty
minutes."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.audio.runner import Process, Runner
from ripdoctor.core.meter import BLOCK, Levels, Verdict, levels_of, verdict

# A device name is a small alphabet. Anything else is a mistake or an attempt,
# and either way the answer is to say so rather than to pass it on.
_DEVICE = re.compile(r"^[A-Za-z0-9:,_./-]+$")

TEST_SECONDS = 20.0
MIN_TEST_SECONDS, MAX_TEST_SECONDS = 3.0, 60.0

# A capture in progress is written under a name nothing else will pick up: tools
# scan for side-*.flac, and a partial file matching that pattern is one an
# analysis pass will read as a whole side.
PARTIAL = ".{stem}-{letter}.capturing.wav"
FINISHED = "{stem}-{letter}.flac"

# A side, or a punch - a re-recording of one track. They are captured the same
# way and must never be confused afterwards, so the stem is part of the name:
# `punch-7.flac` does not match `side-*.flac` and no side scan can see it.
# A WAV header, before the samples start.
HEADER_BYTES = 44

STEMS = ("side", "punch")
SUFFIX = ".capturing.wav"


class CaptureError(Exception):
    """A capture could not be started, or should not be."""


def check_device(device: str) -> str:
    if not device:
        raise CaptureError("no capture device is set - run `ripdoctor devices`")
    if not _DEVICE.match(device):
        raise CaptureError(f"refusing an odd-looking device name: {device!r}")
    return device


# arecord's names for the formats worth capturing at, and how wide each is. A
# 24-bit file read as 16 is noise at the wrong speed, not slightly wrong.
SAMPLE_FORMATS = {"S16_LE": 2, "S24_3LE": 3, "S32_LE": 4}
_FORMATS = {16: "S16_LE", 24: "S24_3LE", 32: "S32_LE"}


@dataclass(frozen=True, slots=True)
class Format:
    rate: int = 48000
    channels: int = 2
    sample_format: str = "S24_3LE"

    @property
    def width(self) -> int:
        """Bytes per sample."""
        return SAMPLE_FORMATS.get(self.sample_format, 3)


def capture_argv(
    device: str, dest: str, fmt: Format, seconds: float | None = None
) -> list[str]:
    """Record to WAV, not into an encoder. ADR-031."""
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


def _stem(stem: str) -> str:
    if stem not in STEMS:
        raise CaptureError(f"unknown capture kind: {stem!r}")
    return stem


def partial_path(album_dir: str | Path, letter: str, stem: str = "side") -> Path:
    return Path(album_dir) / PARTIAL.format(stem=_stem(stem), letter=letter)


def finished_path(album_dir: str | Path, letter: str, stem: str = "side") -> Path:
    return Path(album_dir) / FINISHED.format(stem=_stem(stem), letter=letter)


def start(
    runner: Runner,
    device: str,
    album_dir: str | Path,
    letter: str,
    fmt: Format,
    *,
    log: bool = False,
    stem: str = "side",
) -> tuple[Process, Path]:
    """Begin recording one side. Returns the running process and its file."""
    check_device(device)
    Path(album_dir).mkdir(parents=True, exist_ok=True)
    dest = partial_path(album_dir, letter, stem)
    if finished_path(album_dir, letter, stem).exists():
        raise CaptureError(f"{stem} {letter} already exists; move it first")
    errors = str(log_path(album_dir, letter, stem)) if log else None
    argv = capture_argv(device, str(dest), fmt)
    return runner.start(argv, stderr_path=errors), dest


def finish(
    runner: Runner, album_dir: str | Path, letter: str, stem: str = "side"
) -> Path:
    """Encode a finished capture and remove the partial file."""
    wav = partial_path(album_dir, letter, stem)
    if not wav.is_file() or wav.stat().st_size < 1024:
        raise CaptureError(f"nothing was captured to {wav}")
    flac = finished_path(album_dir, letter, stem)
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
        if any(p.name.startswith(f".{stem}-") for stem in STEMS)
        and p.name.endswith(SUFFIX)
        and p.stat().st_size >= 1024
    )


def stem_of(partial: Path) -> str:
    return partial.name[1:].split("-", 1)[0]


def letter_of(partial: Path) -> str:
    return partial.name[len(f".{stem_of(partial)}-") : -len(SUFFIX)]


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

    `-inf` is what a digitally silent file reports and float() accepts it -
    ADR-027. A non-finite reading is no reading.
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


LOG = ".{stem}-{letter}.capturing.log"


def log_path(album_dir: str | Path, letter: str, stem: str = "side") -> Path:
    return Path(album_dir) / LOG.format(stem=_stem(stem), letter=letter)


def wav_format(path: str | Path, fallback: Format) -> Format:
    """Rate, channels and sample width read from the header, not assumed.

    `wave.open` refuses a file still being written, so the fixed fields are
    parsed directly - they are written up front and never change. ADR-031.
    """
    try:
        with Path(path).open("rb") as f:
            header = f.read(HEADER_BYTES)
    except OSError:
        return fallback
    if len(header) < 40 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return fallback
    channels = struct.unpack("<H", header[22:24])[0] or fallback.channels
    rate = struct.unpack("<I", header[24:28])[0]
    bits = struct.unpack("<H", header[34:36])[0] or fallback.width * 8
    if not (8000 <= rate <= 384000):
        rate = fallback.rate
    return Format(rate, channels, _FORMATS.get(bits, fallback.sample_format))


def meter(path: str | Path, fallback: Format) -> tuple[Levels, Format] | None:
    """Levels over the tail of a capture that is still being written. ADR-031."""
    fmt = wav_format(path, fallback)
    need = BLOCK * fmt.channels * fmt.width
    try:
        with Path(path).open("rb") as f:
            f.seek(0, 2)
            if f.tell() < need + HEADER_BYTES:
                return None
            f.seek(-need, 2)
            raw = f.read(need)
    except OSError:
        return None
    levels = levels_of(raw, fmt.rate, fmt.channels, fmt.width)
    return None if levels is None else (levels, fmt)


# "overrun!!! (at least 8.235 ms long)" - the driver could not be read fast
# enough and that many milliseconds of the record are simply not in the file.
_OVERRUN = re.compile(r"overrun!!! \(at least ([\d.]+) ms long\)")


def overruns(log: str | Path) -> tuple[int, float]:
    """How many gaps the driver reported, and how much time they cost."""
    try:
        text = Path(log).read_text(errors="replace")
    except OSError:
        return 0, 0.0
    found = [float(ms) for ms in _OVERRUN.findall(text)]
    return len(found), round(sum(found), 1)


def stop(proc: Process, *, grace: float = 15.0) -> None:
    """Ask the capture to stop, and kill it only if it will not. ADR-033."""
    if proc.poll() is not None:
        return
    proc.interrupt()
    try:
        proc.wait(timeout=grace)
    except Exception:  # whatever the wait raises, the answer is the same
        proc.kill()
        proc.wait(timeout=5)
