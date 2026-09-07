"""Alignment: recovering a shift, and refusing a fit that does not hold.

The measured tables this is built on are in docs/method.md. What matters here is
that correlation is used only to locate, and that every refusal actually fires -
because the thing being protected is a set of boundaries somebody already spent
an evening approving.
"""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from ripdoctor.core import xcorr as X


def signal(n: int, seed: int = 1) -> list[float]:
    """A pseudo-random level sequence standing in for one side's envelope."""
    rng = random.Random(seed)
    return [rng.uniform(-60.0, -20.0) for _ in range(n)]


# ------------------------------------------------------------ correlation


def test_a_known_shift_is_recovered_exactly() -> None:
    hay = signal(400)
    needle = hay[120:180]
    r, lag = X.best_lag(hay, needle)
    assert lag == 120
    assert r == pytest.approx(1.0, abs=1e-6)


def test_the_same_music_scores_near_one_and_different_music_does_not() -> None:
    """The one question r is asked: is this the same music at all?

    Measured on a real library, a side against itself scores 1.000 and a needle
    from one side against a different record scores 0.379. MIN_R sits in the
    empty gap between those groups.
    """
    hay = signal(400, seed=1)
    same = hay[100:160]
    other = signal(60, seed=99)

    r_same, _ = X.best_lag(hay, same)
    r_other, _ = X.best_lag(hay, other)

    assert r_same > 0.99
    assert r_other < X.MIN_R
    assert r_other < r_same


def test_correlation_survives_noise_on_the_re_rip() -> None:
    """A second capture of the same side is not sample-identical."""
    rng = random.Random(7)
    hay = signal(400)
    needle = [v + rng.uniform(-1.5, 1.5) for v in hay[200:260]]
    r, lag = X.best_lag(hay, needle)
    assert lag == 200
    assert r > X.MIN_R


def test_too_little_audio_is_refused_rather_than_correlated() -> None:
    with pytest.raises(X.AlignError, match="not enough audio"):
        X.best_lag([1.0] * 100, [1.0] * 4)
    with pytest.raises(X.AlignError, match="not enough audio"):
        X.best_lag([1.0] * 10, [1.0] * 50)


def test_a_flat_needle_does_not_divide_by_zero() -> None:
    r, _ = X.best_lag(signal(200), [-30.0] * 40)
    assert -1.0 <= r <= 1.0


# --------------------------------------------------------------- the fit


def test_a_pure_offset_is_recovered() -> None:
    t = X.fit(X.Probe(100.0, 107.3, 0.99), X.Probe(500.0, 507.3, 0.99))
    assert t.scale == pytest.approx(1.0)
    assert t.offset == pytest.approx(7.3)
    assert t.apply(300.0) == pytest.approx(307.3)


def test_a_platter_running_slightly_fast_is_recovered_as_scale() -> None:
    """The reason a global offset is not enough.

    0.1 per cent across a sixteen-minute side is about a second of drift, which
    is plainly audible at a boundary.
    """
    scale = 1.001
    t = X.fit(
        X.Probe(100.0, 100.0 * scale + 5.0, 0.99),
        X.Probe(900.0, 900.0 * scale + 5.0, 0.99),
    )
    assert t.scale == pytest.approx(scale, abs=1e-9)
    assert t.drift_ms_per_min == pytest.approx(60.0, abs=0.1)
    assert t.apply(960.0) - (960.0 + 5.0) == pytest.approx(0.96, abs=0.01)


def test_an_incredible_speed_difference_is_refused() -> None:
    """Two points always define a line, including two wrong ones."""
    with pytest.raises(X.AlignError, match="not credible"):
        X.fit(X.Probe(100.0, 105.0, 0.99), X.Probe(500.0, 530.0, 0.99))


def test_two_probes_at_the_same_moment_are_refused() -> None:
    with pytest.raises(X.AlignError, match="same moment"):
        X.fit(X.Probe(100.0, 105.0, 0.99), X.Probe(100.0, 106.0, 0.99))


def test_a_probe_that_did_not_match_is_refused_by_name() -> None:
    with pytest.raises(X.AlignError, match="first probe only matched"):
        X.require_match(X.Probe(100.0, 105.0, 0.39), "first probe")
    X.require_match(X.Probe(100.0, 105.0, 0.99), "first probe")


# ------------------------------------------------------------- verification


def test_a_fit_that_holds_across_the_side_passes_the_midpoint_check() -> None:
    scale, offset = 1.0005, 3.2
    a = X.Probe(100.0, offset + scale * 100.0, 0.99)
    b = X.Probe(900.0, offset + scale * 900.0, 0.99)
    c = X.Probe(500.0, offset + scale * 500.0, 0.99)

    t = X.fit(a, b)
    assert abs(X.verify(t, c)) < 1e-6


def test_an_internally_consistent_but_wrong_fit_is_caught() -> None:
    """The check that two probes cannot perform on themselves.

    Both probes agree perfectly with the line through them. The midpoint is
    somewhere else entirely, and only a third point can say so.
    """
    a = X.Probe(100.0, 105.0, 0.99)
    b = X.Probe(900.0, 905.0, 0.99)
    t = X.fit(a, b)
    assert t.scale == pytest.approx(1.0)

    off_by = X.Probe(500.0, 505.0 + 1.4, 0.99)
    with pytest.raises(X.AlignError, match="does not hold across the side"):
        X.verify(t, off_by)


def test_a_miss_inside_the_tolerance_is_returned_rather_than_raised() -> None:
    t = X.fit(X.Probe(100.0, 105.0, 0.99), X.Probe(900.0, 905.0, 0.99))
    miss = X.verify(t, X.Probe(500.0, 505.15, 0.99))
    assert miss == pytest.approx(0.15, abs=1e-6)


# ---------------------------------------------------------- probe placement


def test_probes_land_inside_the_music() -> None:
    """Never in lead-in or run-out, where every record sounds like every other."""
    first, last = 15.0, 1215.0
    points = X.probe_points(first, last)
    assert len(points) == 3
    assert all(first < p < last for p in points)
    assert points == pytest.approx((255.0, 615.0, 975.0))


def test_a_side_too_short_for_three_probes_is_refused() -> None:
    with pytest.raises(X.AlignError, match="too short"):
        X.probe_points(10.0, 50.0)


def test_the_transform_moves_a_whole_tracklist_consistently() -> None:
    """What the fit is actually for: every approved boundary, carried over."""
    t = X.fit(X.Probe(100.0, 102.5, 0.99), X.Probe(900.0, 902.9, 0.99))
    old = [15.35, 167.15, 168.0, 320.4, 321.0, 480.9]
    new = [t.apply(x) for x in old]

    assert all(b > a for a, b in pairwise(new))
    # Spacing is preserved to within the platter difference being corrected.
    for (a1, b1), (a2, b2) in zip(pairwise(old), pairwise(new), strict=True):
        assert (b2 - a2) == pytest.approx((b1 - a1) * t.scale, abs=1e-9)
