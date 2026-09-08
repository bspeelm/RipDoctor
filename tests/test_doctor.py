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


def listing(card: str = "Rx") -> str:
    return (
        f"**** List of CAPTURE Hardware Devices ****\n"
        f"card 1: {card} [SAVITECH Audio], device 0: USB Audio [USB Audio]\n"
    )


def with_device(card: str = "Rx") -> FakeRunner:
    """A machine that can actually see the configured device."""
    return everything().expect("-l", stdout=listing(card).encode())


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
    r = next(iter(D.capture(Settings(), with_device())))
    assert r.level is D.Level.WARN
    assert "ripdoctor devices" in r.fix


def test_a_configured_device_is_reported_with_its_format() -> None:
    s = replace(Settings(), capture_device="hw:Rx,0", capture_rate=96000)
    r = next(iter(D.capture(s, with_device())))
    assert r.level is D.Level.OK
    assert "hw:Rx,0" in r.summary and "96000" in r.summary


def test_a_device_the_machine_cannot_see_is_a_failure() -> None:
    """The configuration file still names an interface that has lost contact.

    Recording against whatever answered instead is twenty minutes of the wrong
    input, which is how 162 seconds of mic-jack bleed once got recorded.
    """
    s = replace(Settings(), capture_device="hw:Rx,0")
    r = find(list(D.capture(s, with_device("PCH"))), "capture_device")
    assert r.level is D.Level.FAIL
    assert "cable" in r.fix


def test_a_sample_format_this_cannot_record_in_is_named() -> None:
    """An unrecognised format means the meter reads the capture at the wrong
    width, which is noise at the wrong speed."""
    s = replace(Settings(), capture_device="hw:Rx,0", capture_format="S24_LE")
    r = find(list(D.capture(s, with_device())), "capture_format")
    assert r.level is D.Level.FAIL
    assert "S24_3LE" in r.fix


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

    working = replace(defaults(a_machine(tmp_path)), capture_device="hw:Rx,0")
    fine = D.run_all(a_machine(tmp_path), working, Thresholds(), with_device())
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


# ------------------------------------------------------------- importing


def test_the_importer_in_use_is_reported() -> None:
    r = find(list(D.importing(Settings(), everything())), "importer")
    assert r.level is D.Level.OK and "beets" in r.summary


def test_beets_missing_is_a_broken_install_rather_than_a_silent_swap() -> None:
    """`choose` falls back rather than refusing, and a silent fallback is
    exactly what this exists to say out loud. beets is a dependency, so its
    absence is not a configuration problem - it is a broken install."""
    s = replace(Settings(), importer="beets")
    r = find(list(D.importing(s, FakeRunner(installed={"ffmpeg"}))), "importer")
    assert r.level is D.Level.FAIL
    assert "built-in tagger will be used" in r.summary
    assert "reinstall" in r.fix


def configured(directory: str) -> FakeRunner:
    """A beets that answers `config -d` the way the real one does."""
    return FakeRunner(installed=set(D.REQUIRED) | set(D.OPTIONAL)).expect(
        lambda a: "config" in a,
        stdout=f"library: library.db\ndirectory: {directory}\n".encode(),
    )


def test_a_library_the_two_disagree_about_is_reported() -> None:
    """Two settings name the library and nothing makes them agree. beets keeps
    its own configuration and this project uses whatever it finds, so with no
    beets configuration a record is filed into beets' default - and the archive
    gate then refuses to clear the raw sides of a record that imported fine."""
    s = replace(Settings(), importer="beets", library="/pool/music")
    r = find(list(D.importing(s, configured("~/Music"))), "library")
    assert r.level is D.Level.WARN
    assert "/pool/music" in r.summary and "Music" in r.summary
    assert "directory:" in r.fix


def test_a_library_the_two_agree_about_is_not_a_finding() -> None:
    s = replace(Settings(), importer="beets", library="/pool/music")
    r = find(list(D.importing(s, configured("/pool/music"))), "library")
    assert r.level is D.Level.OK


def test_the_built_in_tagger_is_not_asked_what_beets_thinks() -> None:
    """It files where this project says, so there is nothing to disagree with."""
    s = replace(Settings(), importer="tagger", library="/pool/music")
    assert not [x for x in D.importing(s, everything()) if x.check == "library"]


def loading(plugins: str) -> FakeRunner:
    """A beets that answers `version` the way the real one does."""
    return configured("/pool/music").expect(
        lambda a: "version" in a,
        stdout=f"beets version 2.1.0\nPython version 3.13.5\n{plugins}\n".encode(),
    )


def test_the_plugins_beets_will_run_are_reported() -> None:
    s = replace(Settings(), importer="beets", library="/pool/music")
    runner = loading("plugins: chroma, embedart, fetchart, replaygain")
    r = find(list(D.importing(s, runner)), "beets plugins")
    assert r.level is D.Level.OK and "fetchart" in r.summary


def test_beets_with_no_plugins_is_reported_as_the_silence_it_is() -> None:
    """An import still files the audio correctly with none of them, which is
    why it reads as success. Nothing fails, so nothing says so."""
    s = replace(Settings(), importer="beets", library="/pool/music")
    r = find(list(D.importing(s, loading("no plugins loaded"))), "beets plugins")
    assert r.level is D.Level.WARN
    assert "no plugins" in r.summary and "cover art" in r.summary
    assert "ReplayGain" in r.summary and "BEETSDIR" in r.fix


def test_a_plugin_that_is_configured_but_did_not_load_counts_as_missing() -> None:
    """`beet version` lists what loaded. One whose own dependency is absent is
    configured and not there, and only that answer knows the difference."""
    s = replace(Settings(), importer="beets", library="/pool/music")
    runner = loading("plugins: embedart, fetchart")
    r = find(list(D.importing(s, runner)), "beets plugins")
    assert r.level is D.Level.WARN and "ReplayGain" in r.summary


def test_an_importer_nobody_has_heard_of_is_named() -> None:
    s = replace(Settings(), importer="picard")
    r = find(list(D.importing(s, everything())), "importer")
    assert r.level is D.Level.FAIL and "tagger" in r.fix


def test_a_fix_line_names_something_that_can_be_installed() -> None:
    """ "install arecord" is not a command anybody can run, and a fix line that
    cannot be typed is half a fix."""
    results = list(D.tools(FakeRunner(installed=set())))
    fixes = {r.check: r.fix for r in results}
    assert "alsa-utils" in fixes["arecord"]
    assert "ffmpeg" in fixes["ffprobe"], "ffprobe does not ship on its own"
    assert "flac" in fixes["metaflac"]
    assert "beets" in fixes["beet"]


def test_a_tool_named_after_its_own_package_says_just_that() -> None:
    fixes = {r.check: r.fix for r in D.tools(FakeRunner(installed=set()))}
    assert fixes["ffmpeg"] == "install ffmpeg"
