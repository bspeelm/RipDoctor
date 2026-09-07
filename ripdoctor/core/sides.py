"""Where a side's music runs, and which tracks are on it. docs/method.md."""

from __future__ import annotations

from ripdoctor.core.envelope import Envelope

# Below the music level. Wider than the gap threshold: this separates music
# from groove, not from a silence between tracks.
SPAN_BELOW = 20.0

# A run must last this long to count as music: a needle drop that skids makes a
# second of real audio, and that accident is not the start of side one.
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
    """(start, end) of the music, from the band lane, scanned inward."""
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
    """One contiguous run of tracks per side, as [(first, last_exclusive), ...].

    Exhaustive: a greedy split displaces every side after it.
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
    """Catalogue total minus measured music, per side."""
    out = []
    for span, (lo, hi) in zip(spans, cuts, strict=False):
        out.append(sum(lengths[lo:hi]) - span)
    return out
