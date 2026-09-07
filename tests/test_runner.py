"""The subprocess seam.

This is the layer that decides whether anything above it can be tested at all,
so its own behaviour is pinned tightly - especially the failure modes, because a
missing tool discovered halfway through a twenty-minute capture is the worst
possible time to discover it.
"""

from __future__ import annotations

import pytest

from ripdoctor.audio.runner import (
    FakeRunner,
    RealRunner,
    Result,
    ToolFailed,
    ToolMissing,
)

# ------------------------------------------------------------- the result


def test_a_result_reports_success_and_decodes_output() -> None:
    r = Result(("x",), 0, b"out\n", b"")
    assert r.ok and r.text == "out\n" and r.require() is r


def test_a_failure_raises_with_the_last_line_of_stderr() -> None:
    """A traceback nobody can read is one that gets ignored."""
    r = Result(("ffmpeg", "-i", "x"), 1, b"", b"noise\nInvalid data found\n")
    with pytest.raises(ToolFailed, match="Invalid data found") as e:
        r.require()
    assert e.value.code == 1
    assert e.value.argv[0] == "ffmpeg"


def test_undecodable_output_does_not_crash_the_reader() -> None:
    """ffmpeg emits whatever a file's metadata contained."""
    assert "�" in Result(("x",), 0, b"\xff\xfe bad", b"").text


# ------------------------------------------------------------- the fake


def test_the_fake_answers_from_its_script() -> None:
    fake = FakeRunner().expect("ffprobe", stdout=b"48000")
    assert fake.run(["ffprobe", "-i", "a.flac"]).text == "48000"


def test_replies_match_in_order_so_a_specific_case_can_win() -> None:
    fake = (
        FakeRunner()
        .expect(lambda a: "side-b.flac" in a, stdout=b"specific")
        .expect("ffmpeg", stdout=b"general")
    )
    assert fake.run(["ffmpeg", "-i", "side-b.flac"]).text == "specific"
    assert fake.run(["ffmpeg", "-i", "side-a.flac"]).text == "general"


def test_an_unscripted_call_succeeds_silently() -> None:
    """So a test only has to script the calls it cares about."""
    r = FakeRunner().run(["flac", "-t", "x.flac"])
    assert r.ok and r.stdout == b""


def test_every_call_is_recorded_for_argv_assertions() -> None:
    """For cutting, the argv is the behaviour - the return value says nothing."""
    fake = FakeRunner()
    fake.run(["ffmpeg", "-ss", "10.5", "-to", "20.0", "-i", "a.flac", "out.flac"])
    fake.run(["flac", "-t", "out.flac"])

    assert len(fake.calls) == 2
    cut = fake.argv_for("-ss")
    assert "10.5" in cut and "20.0" in cut


def test_argv_for_refuses_an_ambiguous_match() -> None:
    """Asserting against the wrong one of two calls is worse than failing."""
    fake = FakeRunner()
    fake.run(["ffmpeg", "-i", "a.flac"])
    fake.run(["ffmpeg", "-i", "b.flac"])
    with pytest.raises(AssertionError, match="2 calls matched"):
        fake.argv_for("ffmpeg")
    with pytest.raises(AssertionError, match="0 calls matched"):
        fake.argv_for("sox")


def test_the_fake_can_pretend_a_tool_is_absent() -> None:
    fake = FakeRunner(installed={"ffmpeg"})
    assert fake.which("ffmpeg") == "/usr/bin/ffmpeg"
    assert fake.which("beet") is None
    with pytest.raises(ToolMissing, match="beet is not installed"):
        fake.run(["beet", "ls"])


def test_with_nothing_declared_the_fake_pretends_everything_exists() -> None:
    assert FakeRunner().which("anything") is not None


def test_an_empty_argv_is_refused_by_both() -> None:
    for runner in (FakeRunner(), RealRunner()):
        with pytest.raises(ValueError, match="empty argv"):
            runner.run([])


# --------------------------------------------------------- the real one


def test_the_real_runner_runs_a_program_and_captures_it() -> None:
    r = RealRunner().run(["echo", "hello"])
    assert r.ok and r.text.strip() == "hello"


def test_the_real_runner_reports_a_failure_rather_than_raising() -> None:
    """The caller decides whether a non-zero exit matters; `false` is not fatal."""
    r = RealRunner().run(["sh", "-c", "exit 3"])
    assert not r.ok and r.returncode == 3
    with pytest.raises(ToolFailed):
        r.require()


def test_a_missing_tool_is_named_before_anything_starts() -> None:
    """Not a FileNotFoundError from inside a capture already in progress."""
    with pytest.raises(ToolMissing, match="no-such-tool"):
        RealRunner().run(["no-such-tool-ripdoctor", "-x"])


def test_stdin_is_delivered() -> None:
    r = RealRunner().run(["cat"], stdin=b"piped")
    assert r.stdout == b"piped"


def test_arguments_are_not_interpreted_by_a_shell() -> None:
    """The whole defence against a record title containing a quote or a
    semicolon: there is no string for it to break out of."""
    nasty = "Song'; rm -rf /; echo 'oops"
    r = RealRunner().run(["echo", nasty])
    assert r.text.strip() == nasty


def test_a_timeout_is_enforced() -> None:
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        RealRunner().run(["sleep", "5"], timeout=0.2)
