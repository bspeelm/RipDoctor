"""Laying out a side: where its music runs, and which tracks are on it.

Two steps, both done before any boundary is placed.

`music_span` finds where a side's music starts and ends. It must use the band
lane: full band, the needle-down transient and arm-handling rumble are LOUDER
than quiet music, so a level gate on the full lane puts the start in the middle
of the handling noise rather than at the first note.

`assign_sides` decides which tracks are on which side by comparing each side's
measured music length against running totals of catalogue durations. Vinyl
releases rarely record side breaks, so this is arithmetic rather than metadata -
the same sum a person does by hand, done exhaustively.
"""

from __future__ import annotations

from ripdoctor.core.envelope import Envelope

# How far below the music level still counts as music, when looking for the
# first and last sustained run on a side. Wider than the gap threshold because
# this only has to separate music from the run-in and run-out groove, not music
# from an inter-track silence.
SPAN_BELOW = 20.0

# A run must last this long to count as the start of the music. Shorter than an
# inter-track gap on purpose: a needle drop that skids into a groove can produce
# a second or so of real audio before the arm is lifted and re-dropped, and that
# accident is not the start of side one.
SPAN_RUN = 1.5

MUSIC_PCT = 0.85


def _run_start(levels: tuple[float, ...], threshold: float, need: int) -> int | None:
    """Index where the first run of `need` readings above `threshold` begins."""
    run = 0
    for i, v in enumerate(levels):
        if v > threshold:
            run += 1
            if run >= need:
                return i - run + 1
        else:
            run = 0
    return None


def music_span(env: Envelope) -> tuple[float, float]:
    """(start, end) of the music on one side, measured on the band lane.

    Both ends are found the same way - the first sustained run of music, scanning
    inward from each end of the side - so the answer is symmetric. Everything
    outside is run-in groove, run-out groove, the needle drop and the lift.

    Returns the whole side if no sustained music is found at all, which is the
    honest answer for an envelope that is silent or too short to judge.
    """
    if not len(env):
        return 0.0, 0.0

    threshold = env.percentile(MUSIC_PCT) - SPAN_BELOW
    need = max(1, int(SPAN_RUN / env.window))

    start = _run_start(env.levels, threshold, need)
    if start is None:
        return 0.0, env.duration

    # Scanning the reversed lane finds the last run; mapping the index back
    # gives the reading just past the final note.
    tail = _run_start(env.levels[::-1], threshold, need)
    if tail is None:
        return 0.0, env.duration
    end = len(env) - tail

    if end <= start:
        return 0.0, env.duration
    return round(start * env.window, 2), round(end * env.window, 2)


def assign_sides(spans: list[float], lengths: list[float]) -> list[tuple[int, int]]:
    """Split a tracklist into one contiguous run per side.

    Returns [(first, last_exclusive), ...], one pair per side, chosen to
    minimise the total mismatch between each side's catalogue sum and the music
    actually measured on it.

    Exhaustive rather than greedy. A greedy walk commits to the first split that
    looks reasonable, and one bad split displaces every side after it - the same
    failure the fitter avoids by not walking forward. The search is small enough
    that there is no reason to guess: a handful of sides against a dozen or two
    tracks.
    """
    n, k = len(lengths), len(spans)
    if k == 0 or n == 0:
        return []

    prefix = [0.0]
    for length in lengths:
        prefix.append(prefix[-1] + (length or 0.0))

    inf = float("inf")
    # best[j][i]: least cost of placing sides j..k-1 over tracks i..n-1.
    best = [[inf] * (n + 1) for _ in range(k + 1)]
    pick: list[list[int | None]] = [[None] * (n + 1) for _ in range(k + 1)]
    best[k][n] = 0.0

    for j in range(k - 1, -1, -1):
        for i in range(n + 1):
            for e in range(i, n + 1):
                if best[j + 1][e] == inf:
                    continue
                # The last side must consume every remaining track.
                if j == k - 1 and e != n:
                    continue
                cost = abs((prefix[e] - prefix[i]) - spans[j]) + best[j + 1][e]
                if cost < best[j][i]:
                    best[j][i] = cost
                    pick[j][i] = e

    out: list[tuple[int, int]] = []
    i = 0
    for j in range(k):
        chosen = pick[j][i]
        end = n if chosen is None else chosen
        out.append((i, end))
        i = end
    return out


def side_mismatch(
    spans: list[float], lengths: list[float], cuts: list[tuple[int, int]]
) -> list[float]:
    """Per side, catalogue total minus measured music.

    Reported rather than hidden. A side that is out by seconds is normal - quiet
    heads and tails fall below any threshold. A side out by a whole track is the
    signal that the release is wrong, or that a track is missing from it.
    """
    out = []
    for span, (lo, hi) in zip(spans, cuts, strict=False):
        out.append(sum(lengths[lo:hi]) - span)
    return out
