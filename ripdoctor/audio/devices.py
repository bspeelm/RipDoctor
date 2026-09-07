"""Capture devices as ALSA sees them. ADR-009: listed, never guessed at."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ripdoctor.audio.runner import Runner

# Rates worth offering. A device's own hardware parameters decide which of these
# it can actually take; offering one it cannot moves the failure into the middle
# of a side.
COMMON_RATES = (44100, 48000, 88200, 96000, 176400, 192000)

# Best first. S24_LE is deliberately absent: it is 24 bits padded into four
# bytes, which several converters advertise and then deliver badly.
FORMATS = ("S32_LE", "S24_3LE", "S16_LE")

# Card and device names contain spaces - "ALC1150 Alt Analog" - so the name
# groups have to be non-greedy rather than \S+.
_CARD = re.compile(
    r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+): (.+?) \[([^\]]*)\]", re.M
)


@dataclass(frozen=True, slots=True)
class Device:
    """One capture device, addressed by card name rather than number.

    Card numbers are assigned at detection time and shuffle between boots,
    exactly as disk names do. The name is stable.
    """

    id: str
    card: int
    device: int
    name: str
    rates: tuple[int, ...] = ()
    formats: tuple[str, ...] = ()

    @property
    def best_format(self) -> str:
        return self.formats[0] if self.formats else FORMATS[-1]


def parse_list(text: str) -> list[Device]:
    return [
        Device(
            id=f"hw:{cid},{dev}",
            card=int(card),
            device=int(dev),
            name=f"{cname} - {dname}",
        )
        for card, cid, cname, dev, dname, _ in _CARD.findall(text)
    ]


def parse_hw_params(text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Rates and formats a device will actually accept.

    Read rather than assumed. One converter here was 16-bit only and its
    replacement offers 24; capturing a 24-bit source as 16 silently discards the
    low eight bits and nothing reports it.
    """
    rates: set[int] = set()
    m = re.search(r"^RATE:\s*(.+)$", text, re.M)
    if m:
        line = m.group(1)
        rates |= {int(x) for x in re.findall(r"\d+", line)}
        span = re.match(r"\s*\[?(\d+)\s+(\d+)\]?", line)
        if span:
            lo, hi = int(span.group(1)), int(span.group(2))
            rates |= {r for r in COMMON_RATES if lo <= r <= hi}

    m = re.search(r"^FORMAT:\s*(.+)$", text, re.M)
    offered = set(m.group(1).split()) if m else set()

    return (
        tuple(sorted(r for r in rates if r in COMMON_RATES)),
        tuple(f for f in FORMATS if f in offered),
    )


def probe(
    runner: Runner, device: str, timeout: float = 10.0
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    result = runner.run(
        ["arecord", "-D", device, "--dump-hw-params", "-d", "1", "/dev/null"],
        timeout=timeout,
    )
    # arecord reports hardware parameters on stderr and exits non-zero; that is
    # how it works, not a failure.
    return parse_hw_params(result.text + result.err)


def enumerate_devices(runner: Runner, timeout: float = 10.0) -> list[Device]:
    listed = parse_list(runner.run(["arecord", "-l"], timeout=timeout).text)
    out = []
    for d in listed:
        rates, formats = probe(runner, d.id, timeout=timeout)
        out.append(
            Device(
                id=d.id,
                card=d.card,
                device=d.device,
                name=d.name,
                rates=rates,
                formats=formats,
            )
        )
    return out


def report(devices: list[Device], configured: str = "") -> str:
    """What is here, and how to choose one.

    No device is called the turntable. Deciding that from a card name is what
    made the predecessor refuse to record on any machine but its own.
    """
    if not devices:
        return "  no capture devices found"
    lines = []
    for d in devices:
        mark = "*" if d.id == configured else " "
        rates = ", ".join(str(r) for r in d.rates) or "unknown"
        formats = ", ".join(d.formats) or "unknown"
        lines.append(f"  {mark} {d.id:<14} {d.name}")
        lines.append(f"      rates: {rates}")
        lines.append(f"      formats: {formats}")
    lines.append("")
    lines.append(
        "  set capture_device in config.toml to the one your turntable is on"
        if not configured
        else f"  configured: {configured}"
    )
    return "\n".join(lines)
