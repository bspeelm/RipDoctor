"""Finding the same music in two different captures.

Used for two things: carrying an archived cut onto a re-rip of the same record,
and placing a punch - a re-recording of one track - against the side it replaces.

The arithmetic is pure and lives in core/xcorr. What is here is getting
comparable numbers out of two files, and the two-pass search: a coarse pass
locates the music, a fine pass judges it. The coarse correlation is thrown away,
because 0.2 s windows will report a confident match on any two passages of
similar loudness.
"""

from __future__ import annotations

import re

from ripdoctor.audio.ffprobe import sample_rate
from ripdoctor.audio.runner import Runner
from ripdoctor.core.envelope import DB_MIN
from ripdoctor.core.xcorr import (
    AlignError,
    Probe,
    Transform,
    best_lag,
    fit,
    require_match,
    verify,
)

COARSE_WINDOW = 0.2  # locating resolution
FINE_WINDOW = 0.02  # judging resolution: fine enough to see a transient

COARSE_PROBE = 30.0  # seconds of music correlated on in the locating pass
FINE_PROBE = 2.0  # short, because drift inside a probe blurs the match

COARSE_HUNT = 120.0  # how far the first probe hunts for the needle drop
REFINE = 1.5  # how far around the coarse hit the fine pass sweeps

_RMS = re.compile(r"^lavfi\.astats\.Overall\.RMS_level=(.+)$")


def envelope_argv(path: str, t0: float, t1: float, samples: int) -> list[str]:
    """`-ss` before `-i`, so the seek is a seek rather than a decode."""
    chain = (
        f"asetnsamples=n={samples},"
        "astats=metadata=1:reset=1:measure_perchannel=none,"
        "ametadata=print:file=-"
    )
    return [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, t0):.3f}",
        "-to",
        f"{t1:.3f}",
        "-i",
        path,
        "-ac",
        "1",
        "-af",
        chain,
        "-f",
        "null",
        "-",
    ]


def envelope(
    runner: Runner, path: str, t0: float, t1: float, window: float, rate: int
) -> list[float]:
    """RMS dB per window between two times.

    The window is a count of samples, so it has to come from the file's own
    rate. Assuming 48 kHz degraded rather than broke: on a 96 kHz capture every
    window was half as long, so a 30-second needle was compared against fifteen
    seconds of hay and the correlation came back just under the floor. The
    result was a side that could have been aligned being refused.

    `-ar` does not fix it. That is an output option, applied after the filter
    graph, and asetnsamples sits inside the graph and sees the input rate.
    """
    result = runner.run(envelope_argv(path, t0, t1, int(rate * window)), timeout=300)
    out = []
    for line in result.text.splitlines():
        found = _RMS.match(line)
        if found:
            try:
                out.append(max(DB_MIN, float(found.group(1))))
            except ValueError:
                out.append(DB_MIN)
    return out


def probe(
    runner: Runner,
    old: str,
    new: str,
    old_at: float,
    center: float,
    search: float,
    new_duration: float,
    *,
    coarse_probe: float = COARSE_PROBE,
    rates: tuple[int, int] | None = None,
) -> Probe:
    """Where the old side's audio at `old_at` lands in the new capture.

    `center` is where to look and `old_at` is what to look for. They are
    separate because the second probe already knows roughly how far the needle
    drop moved and should search there, while still taking its needle from the
    old side's own timeline. Collapsing the two doubled the measured shift on
    the second probe and produced a confident fit with an impossible scale.
    """
    old_rate, new_rate = rates or (
        sample_rate(runner, old),
        sample_rate(runner, new),
    )
    lo = max(0.0, center - search)
    hi = min(new_duration, center + coarse_probe + search)

    needle = envelope(
        runner, old, old_at, old_at + coarse_probe, COARSE_WINDOW, old_rate
    )
    hay = envelope(runner, new, lo, hi, COARSE_WINDOW, new_rate)
    _coarse_r, lag = best_lag(hay, needle)
    approx = lo + lag * COARSE_WINDOW

    fine = envelope(runner, old, old_at, old_at + FINE_PROBE, FINE_WINDOW, old_rate)
    flo = max(0.0, approx - REFINE)
    fhi = min(new_duration, approx + FINE_PROBE + REFINE)
    r, lag = best_lag(envelope(runner, new, flo, fhi, FINE_WINDOW, new_rate), fine)
    return Probe(old_t=round(old_at, 2), new_t=round(flo + lag * FINE_WINDOW, 3), r=r)


def fit_side(
    runner: Runner, old: str, new: str, first: float, last: float, duration: float
) -> tuple[Transform, tuple[Probe, ...], float]:
    """Map old-side time onto a re-rip. Returns the fit, its probes and the miss.

    `first` and `last` bound the music, so the probes land in music rather than
    in the lead-in or the run-out - where every record sounds like every other
    record and a correlation means nothing.
    """
    span = last - first
    if span < 3 * COARSE_PROBE:
        raise AlignError(f"side is too short to fit two probes ({span:.0f}s of music)")

    rates = (sample_rate(runner, old), sample_rate(runner, new))
    a_at, b_at = first + span * 0.20, first + span * 0.80

    a = probe(runner, old, new, a_at, a_at, COARSE_HUNT, duration, rates=rates)
    require_match(a, "the first probe")
    # The second searches around where the first landed: the needle drop has
    # already been measured, so hunting the whole side again invites a match on
    # the wrong passage.
    b = probe(
        runner, old, new, b_at, b_at + a.shift, COARSE_HUNT, duration, rates=rates
    )
    require_match(b, "the second probe")

    transform = fit(a, b)
    middle = first + span * 0.50
    check = probe(
        runner,
        old,
        new,
        middle,
        transform.apply(middle),
        REFINE * 4,
        duration,
        rates=rates,
    )
    require_match(check, "the midpoint check")
    return transform, (a, b, check), verify(transform, check)


# A punch is the same problem with one difference that breaks the above: the
# offset is not small. The track sits nine hundred seconds into the side and
# four seconds into the punch, so the shift is about minus nine hundred - far
# outside the hunt, and the search range would run backwards. So the first
# probe searches the whole capture, which is affordable precisely because a
# punch is short.
PUNCH_MIN_SPAN = 3 * COARSE_PROBE


def fit_punch(
    runner: Runner, old: str, new: str, first: float, last: float, duration: float
) -> tuple[Transform, tuple[Probe, ...], bool]:
    """Map an archived track's time onto a punch capture.

    The third value says whether the scale was assumed rather than measured. A
    track too short for two probes gets offset only, which is honest rather
    than convenient: over a sixty-second track a 0.1% platter difference is
    sixty milliseconds, which lands inside the gap it is cutting at. Over a
    sixteen-minute side it would not.
    """
    span = last - first
    if span < 8:
        raise AlignError(f"track is only {span:.1f}s long - too short to correlate")
    length = min(COARSE_PROBE, max(6.0, span * 0.45))
    rates = (sample_rate(runner, old), sample_rate(runner, new))
    middle = duration / 2.0

    if span < PUNCH_MIN_SPAN:
        # Taken near the start, because the boundary that matters most is where
        # the track begins.
        at = first + min(5.0, span * 0.1)
        only = probe(
            runner,
            old,
            new,
            at,
            middle,
            duration,
            duration,
            coarse_probe=length,
            rates=rates,
        )
        require_match(only, "the punch")
        return Transform(offset=only.shift, scale=1.0), (only,), True

    a_at, b_at = first + span * 0.20, first + span * 0.80
    a = probe(
        runner,
        old,
        new,
        a_at,
        middle,
        duration,
        duration,
        coarse_probe=length,
        rates=rates,
    )
    require_match(a, "the first probe")
    b = probe(
        runner,
        old,
        new,
        b_at,
        b_at + a.shift,
        REFINE * 4,
        duration,
        coarse_probe=length,
        rates=rates,
    )
    require_match(b, "the second probe")
    transform = fit(a, b)
    return transform, (a, b), False
