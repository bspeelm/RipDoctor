"""The doctor: what it reports, and that every finding says what to do.

This is what turns detection-that-never-fails into something useful. Every
result a person cannot act on is a result that trains them to ignore the rest.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.config.machine import detect
from ripdoctor.config.settings import Settings, defaults
from ripdoctor.config.thresholds import Thresholds
from ripdoctor.doctor import checks as D


def a_machine(tmp_path: Path):
    return detect(
        {
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / ".config"),
            "XDG_DATA_HOME": str(tmp_path / ".local" / "share"),
        }
    )


def everything() -> FakeRunner:
    return FakeRunner(installed=set(D.REQUIRED) | set(D.OPTIONAL))


def find(results: list[D.Result], check: str) -> D.Result:
    return next(r for r in results if r.check == check)


# --------------------------------------------------------------- tools


def test_a_missing_required_tool_blocks_and_says_what_it_is_for() -> None:
    """ "ffmpeg not found" is a fact. Saying what it was wanted for is help."""
    results = list(D.tools(FakeRunner(installed={"ffprobe", "flac"})))
    r = find(results, "ffmpeg")
    assert r.level is D.Level.FAIL and r.blocking
    assert "cut tracks" in r.summary
    assert r.fix == "install ffmpeg"


def test_a_missing_optional_tool_warns_and_names_what_is_lost() -> None:
    results = list(D.tools(FakeRunner(installed=set(D.REQUIRED))))
    r = find(results, "arecord")
    assert r.level is D.Level.WARN and not r.blocking
    assert "record from a turntable" in r.summary


def test_with_everything_installed_nothing_blocks() -> None:
    assert D.ok(list(D.tools(everything())))


def test_this_machine_is_a_fair_test_of_the_doctor() -> None:
    """Nothing needed is installed here, which is exactly the case to handle."""
    from ripdoctor.audio.runner import RealRunner

    results = list(D.tools(RealRunner()))
    assert results, "no tools were checked"
    for r in results:
        assert r.summary and (r.level is D.Level.OK or r.fix), (
            f"{r.check} reported a problem with nothing to do about it"
        )


# ------------------------------------------------------------- storage


def test_an_unset_directory_blocks(tmp_path: Path) -> None:
    results = list(D.storage(Settings()))
    assert all(r.blocking for r in results)
    assert all("config.toml" in r.fix for r in results)


def test_a_directory_that_does_not_exist_yet_only_warns(tmp_path: Path) -> None:
    """It is made on first use; refusing to start over it would be wrong."""
    s = replace(Settings(), vinyl=str(tmp_path / "nope"), library=str(tmp_path))
    r = find(list(D.storage(s)), "vinyl")
    assert r.level is D.Level.WARN and not r.blocking


def test_a_directory_that_cannot_be_written_blocks(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        s = replace(Settings(), vinyl=str(locked), library=str(tmp_path))
        r = find(list(D.storage(s)), "vinyl")
        assert r.level is D.Level.FAIL
        assert "not writable" in r.summary
    finally:
        locked.chmod(0o700)


def test_a_usable_directory_is_reported_with_its_path(tmp_path: Path) -> None:
    s = replace(Settings(), vinyl=str(tmp_path), library=str(tmp_path))
    r = find(list(D.storage(s)), "vinyl")
    assert r.level is D.Level.OK and str(tmp_path) in r.summary


# ------------------------------------------------------------- capture


def test_capture_is_not_mentioned_when_recording_is_impossible() -> None:
    """A machine with no arecord is not a machine with a capture problem."""
    assert list(D.capture(Settings(), FakeRunner(installed={"ffmpeg"}))) == []


def test_an_unconfigured_device_warns_and_names_the_command_that_lists_them() -> None:
    r = next(iter(D.capture(Settings(), everything())))
    assert r.level is D.Level.WARN
    assert "ripdoctor devices" in r.fix


def test_a_configured_device_is_reported_with_its_format() -> None:
    s = replace(Settings(), capture_device="hw:Rx,0", capture_rate=96000)
    r = next(iter(D.capture(s, everything())))
    assert r.level is D.Level.OK
    assert "hw:Rx,0" in r.summary and "96000" in r.summary


# ------------------------------------------------------- configuration


def test_an_unrecognised_setting_is_reported_with_a_suggestion() -> None:
    s = replace(Settings(), unknown=("prot",))
    r = next(iter(D.configuration(s, Thresholds())))
    assert r.level is D.Level.WARN
    assert "did you mean port" in r.fix


def test_an_unrecognisable_setting_is_reported_without_a_guess() -> None:
    s = replace(Settings(), unknown=("xyzzy",))
    r = next(iter(D.configuration(s, Thresholds())))
    assert "did you mean" not in r.fix


def test_a_moved_threshold_is_surfaced_with_what_it_was() -> None:
    """The first thing to check when detection behaves unexpectedly."""
    results = list(D.configuration(Settings(), Thresholds(gap_below=12.0)))
    r = find(results, "thresholds")
    assert "gap_below 16.0->12.0" in r.summary


def test_untouched_thresholds_are_not_mentioned() -> None:
    assert not [
        r for r in D.configuration(Settings(), Thresholds()) if r.check == "thresholds"
    ]


# -------------------------------------------------------------- report


def test_the_report_puts_problems_first(tmp_path: Path) -> None:
    results = D.run_all(
        a_machine(tmp_path), Settings(), Thresholds(), FakeRunner(installed={"flac"})
    )
    text = D.report(results)
    first_fail = text.index("FAIL")
    assert "ok  " not in text[:first_fail], (
        "a passing check was printed above a failure"
    )


def test_the_report_ends_with_whether_it_can_run(tmp_path: Path) -> None:
    broken = D.run_all(
        a_machine(tmp_path), Settings(), Thresholds(), FakeRunner(installed=set())
    )
    assert "cannot run" in D.report(broken)

    working = replace(defaults(a_machine(tmp_path)), capture_device="hw:X,0")
    fine = D.run_all(a_machine(tmp_path), working, Thresholds(), everything())
    assert "cannot run" not in D.report(fine)


def test_every_finding_that_is_not_ok_says_what_to_do(tmp_path: Path) -> None:
    """A report nobody can act on trains people to ignore the next one."""
    results = D.run_all(
        a_machine(tmp_path), Settings(), Thresholds(), FakeRunner(installed=set())
    )
    assert len(results) >= 8, "too few checks ran; the pattern has drifted"
    for r in results:
        if r.level is not D.Level.OK:
            assert r.fix, f"{r.check}: {r.summary!r} with nothing to do about it"


def test_the_settings_file_is_named_whether_or_not_it_exists(tmp_path: Path) -> None:
    """So a config being edited in the wrong place is visible immediately."""
    m = a_machine(tmp_path)
    r = find(D.run_all(m, Settings(), Thresholds(), everything()), "config_file")
    assert str(m.settings_file) in r.summary

    m.config_dir.mkdir(parents=True)
    m.settings_file.write_text("port = 9000\n")
    r = find(D.run_all(m, Settings(), Thresholds(), everything()), "config_file")
    assert str(m.settings_file) in r.summary


@pytest.mark.parametrize("installed", [set(), {"ffmpeg"}, set(D.REQUIRED)])
def test_the_doctor_never_raises_however_broken_the_machine(
    installed: set[str], tmp_path: Path
) -> None:
    """It is the command that explains a broken machine; it cannot need one."""
    results = D.run_all(
        a_machine(tmp_path),
        Settings(unknown=("x",), unreadable=("y",)),
        Thresholds(gap_below=1.0),
        FakeRunner(installed=installed),
    )
    assert D.report(results)
