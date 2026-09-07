"""A pool on disk, and a runner that fakes the tools that fill it.

Shared by the cache tests and the route tests: both need a record with a
prepared side, and preparing one means faking ffprobe, astats and an encode
that leaves a file behind.
"""

from __future__ import annotations

from pathlib import Path

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.store import cache as C
from ripdoctor.store.files import Layout

SECONDS = 2.0
WINDOWS = int(SECONDS / C.WINDOW)


class Encoding(FakeRunner):
    """A fake ffmpeg that leaves behind the file it was told to write."""

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = [str(a) for a in argv]
        if args[0] == "ffmpeg" and args[-1].endswith(".tmp"):
            Path(args[-1]).write_bytes(b"OggS" + b"\x00" * 500)
        return super().run(argv, stdin=stdin, timeout=timeout)


def frames(count: int, *, rms: float = -25.0, peak: float = -9.0) -> bytes:
    rows = []
    for i in range(count):
        rows.append(f"frame:{i} pts:{i} pts_time:{i * 0.05}")
        rows.append(f"lavfi.astats.Overall.RMS_level={rms}")
        rows.append(f"lavfi.astats.Overall.Peak_level={peak}")
    return "\n".join(rows).encode()


def quiet_then_loud(count: int) -> bytes:
    """An envelope with a real gap in the middle of it."""
    rows = []
    for i in range(count):
        quiet = count // 3 <= i < 2 * count // 3
        rows.append(f"frame:{i} pts:{i} pts_time:{i * 0.05}")
        rows.append(f"lavfi.astats.Overall.RMS_level={-85.0 if quiet else -25.0}")
        rows.append(f"lavfi.astats.Overall.Peak_level={-70.0 if quiet else -9.0}")
    return "\n".join(rows).encode()


def a_runner(
    *,
    windows: int = WINDOWS,
    seconds: float = SECONDS,
    encodes: bool = True,
    levels: bytes | None = None,
) -> FakeRunner:
    fake = Encoding() if encodes else FakeRunner()
    measured = levels if levels is not None else frames(windows)
    return (
        fake.expect("ffprobe", stdout=b"48000\n")
        .expect(
            lambda a: "-progress" in a,
            stdout=f"out_time_us={int(seconds * 1e6)}\n".encode(),
        )
        .expect(lambda a: any("highpass" in x for x in a), stdout=measured)
        .expect("ametadata", stdout=measured)
    )


def a_layout(tmp_path: Path, sides: tuple[str, ...] = ("a",)) -> Layout:
    layout = Layout(tmp_path / "vinyl")
    layout.ensure()
    album = layout.raw / "album"
    album.mkdir()
    for letter in sides:
        (album / f"side-{letter}.flac").write_bytes(b"fLaC" + b"\x00" * 4000)
    return layout
