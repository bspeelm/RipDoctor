"""Comparing a signal chain against the numbers this ships with.

The shipped thresholds came from one turntable through one converter. These
tests are about what happens on a different one - including the case that
matters most, a chain where a shipped threshold cannot fire at all.
"""

from __future__ import annotations

from ripdoctor.config.thresholds import Thresholds
from ripdoctor.core import measure as M
from ripdoctor.core.meter import Levels


def chain(
    *, dead: float = -95.0, groove: float = -72.0, music: float = -30.0
) -> M.Reference:
    """Three reference recordings, described by their band levels."""
    return M.Reference(
        dead=Levels(full=dead + 4, band=dead, peak=dead + 10),
        groove=Levels(full=groove + 20, band=groove, peak=groove + 12),
        music=Levels(full=music + 8, band=music, peak=-12.3),
    )


def shipped() -> dict[str, float]:
    return Thresholds().as_dict()


def named(findings: list[M.Finding], name: str) -> M.Finding:
    return next(f for f in findings if f.name == name)


# ------------------------------------------------------- what it measures


def test_the_separation_is_measured_in_both_lanes() -> None:
    """The founding observation, on the user's own equipment.

    Full band, a gap and a quiet passage sit close together. Band-limited they
    do not, and this is where somebody sees that for themselves.
    """
    ref = chain(groove=-72.0, music=-30.0)
    assert ref.band_separation == 42.0
    assert ref.full_separation < ref.band_separation


def test_a_chain_like_the_one_the_numbers_came_from_agrees() -> None:
    findings = M.compare(chain(), shipped())
    assert findings, "nothing was compared"
    assert all(f.agrees for f in findings)
    assert M.snippet(findings) == ""


# ------------------------------------------------- where they disagree


def test_a_chain_with_less_separation_gets_smaller_offsets() -> None:
    """A threshold wider than the separation available never fires."""
    findings = M.compare(chain(groove=-45.0, music=-30.0), shipped())
    span = named(findings, "span_below")
    assert not span.agrees
    assert span.suggestion is not None and span.suggestion < span.shipped
    assert span.suggestion <= span.measured - M.MARGIN


def test_a_suggested_offset_is_never_zero_or_negative() -> None:
    """A chain with almost no separation has a problem no number fixes, and
    emitting `span_below = -4` would hide it behind a plausible-looking value.
    """
    findings = M.compare(chain(groove=-33.0, music=-30.0), shipped())
    for f in findings:
        assert f.suggestion is None or f.suggestion >= 1.0


def test_an_arm_floor_above_the_music_is_caught() -> None:
    """The failure this exists to catch: on a quiet chain the auto-stop never
    arms, and every side runs to the hard cap with nothing said."""
    findings = M.compare(chain(dead=-95.0, groove=-80.0, music=-58.0), shipped())
    arm = named(findings, "arm_floor")
    assert not arm.agrees
    assert arm.suggestion is not None and arm.suggestion < -58.0


def test_an_arm_floor_under_the_electrical_floor_is_caught() -> None:
    """The other direction: it arms on an empty room."""
    findings = M.compare(chain(dead=-58.0, groove=-50.0, music=-20.0), shipped())
    assert not named(findings, "arm_floor").agrees


# ---------------------------------------------------------- the output


def test_the_snippet_carries_only_what_moved() -> None:
    """Writing the defaults back into a file makes them look deliberate."""
    findings = M.compare(chain(groove=-45.0, music=-30.0), shipped())
    text = M.snippet(findings)
    assert text.startswith("[thresholds]")
    for f in findings:
        assert (f.name in text) is not f.agrees


def test_every_suggestion_is_a_real_threshold_name() -> None:
    """A snippet naming a setting that does not exist is a snippet that gets
    pasted and silently ignored."""
    from ripdoctor.config import thresholds as T

    findings = M.compare(chain(groove=-45.0, music=-30.0), shipped())
    assert [f.name for f in findings if f.name not in T.NAMES] == []


def test_the_report_shows_both_lanes_and_the_calibration_peak() -> None:
    ref = chain()
    text = M.report(ref, M.compare(ref, shipped()))
    assert "1-3k" in text and "full band" in text
    assert str(M.CALIBRATED_PEAK) in text
    assert "-12.3" in text, "the measured peak is not shown"


def test_a_disagreement_is_marked_in_the_report() -> None:
    ref = chain(groove=-45.0, music=-30.0)
    text = M.report(ref, M.compare(ref, shipped()))
    assert "!span_below" in text
    assert "->" in text
