"""The command line, end to end, with no ffmpeg anywhere.

Every command is driven through a fake runner, so these exercise the real
argument parsing, the real fitting and the real refusals - only the external
programs are stood in for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.audio.runner import FakeRunner
from ripdoctor.cli import build_parser, main
from ripdoctor.doctor import checks as D

FIXTURES = Path(__file__).parent / "fixtures" / "golden"


def a_record() -> dict:
    return json.loads(sorted(FIXTURES.glob("record-*.json"))[0].read_text())


def write_inputs(tmp_path: Path) -> tuple[Path, list[str]]:
    """A spec and one envelope file per side, from a golden record."""
    rec = a_record()
    spec = tmp_path / "album.spec.json"
    spec.write_text(json.dumps(rec["detector_spec"]))
    envs = []
    for letter, side in rec["sides"].items():
        p = tmp_path / f"env-side-{letter}.json"
        p.write_text(
            json.dumps({"levels_b64": side["levels_b64"], "window": side["window"]})
        )
        envs.append(str(p))
    return spec, envs


def everything() -> FakeRunner:
    return FakeRunner(installed=set(D.REQUIRED) | set(D.OPTIONAL))


# ------------------------------------------------------------ the parser


def test_no_arguments_prints_help_and_succeeds(capsys) -> None:
    assert main([]) == 0
    assert "ripdoctor" in capsys.readouterr().out


def test_every_subcommand_is_reachable() -> None:
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "no subcommands are registered"
    names = set(actions[0].choices)
    assert {"doctor", "config", "fit", "split", "check"} <= names


def test_every_documented_command_exists() -> None:
    """Prose has no compiler; this stands in for one.

    Every command the README names must be a real subparser, and every
    subparser must be named in the README.
    """
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    parser = build_parser()
    names = set(next(a for a in parser._actions if a.dest == "command").choices)

    documented = {n for n in names if f"ripdoctor {n}" in readme}
    assert documented, "the README documents no commands at all"
    undocumented = names - documented
    assert not undocumented, f"commands absent from the README: {sorted(undocumented)}"


# ------------------------------------------------------------- doctor


def test_doctor_reports_and_fails_on_a_bare_machine(capsys) -> None:
    assert main(["doctor"], runner=FakeRunner(installed=set())) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "cannot run" in out


def test_doctor_emits_json_that_parses(capsys) -> None:
    main(["doctor", "--json"], runner=everything())
    data = json.loads(capsys.readouterr().out)
    assert "ok" in data and data["checks"]
    assert "settings" in data and "thresholds" in data["settings"]


def test_config_prints_every_resolved_setting(capsys) -> None:
    assert main(["config"], runner=everything()) == 0
    data = json.loads(capsys.readouterr().out)
    assert "vinyl" in data and "library" in data


# ---------------------------------------------------------------- fit


def test_fit_places_boundaries_and_writes_a_plan(tmp_path: Path, capsys) -> None:
    spec, envs = write_inputs(tmp_path)
    out = tmp_path / "album.plan.json"
    code = main(
        ["fit", str(spec), *envs, "--lane", "full", "-o", str(out)], runner=everything()
    )
    assert code == 0, out.read_text() if out.exists() else "refused"

    plan = json.loads(out.read_text())
    assert plan["sides"] and all(s["tracks"] for s in plan["sides"])
    printed = capsys.readouterr().out
    assert "boundary" in printed and "total measured" in printed


def test_fit_refuses_a_side_it_was_given_no_envelope_for(
    tmp_path: Path, capsys
) -> None:
    spec, envs = write_inputs(tmp_path)
    assert (
        main(
            [
                "fit",
                str(spec),
                envs[0],
                "--lane",
                "full",
                "-o",
                str(tmp_path / "p.json"),
            ],
            runner=everything(),
        )
        == 2
    )
    assert "no envelope" in capsys.readouterr().err


def test_fit_does_not_write_a_plan_that_cannot_be_cut(tmp_path: Path, capsys) -> None:
    """The record whose first pass runs out of side.

    It produces the same numbers the reference does and then refuses, rather
    than writing a plan whose last track would be an empty file.
    """
    rec = json.loads((FIXTURES / "record-f838e3f8.json").read_text())
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(rec["detector_spec"]))
    envs = []
    for letter, side in rec["sides"].items():
        p = tmp_path / f"env-side-{letter}.json"
        p.write_text(
            json.dumps({"levels_b64": side["levels_b64"], "window": side["window"]})
        )
        envs.append(str(p))

    out = tmp_path / "p.json"
    code = main(
        ["fit", str(spec), *envs, "--lane", "full", "-o", str(out)], runner=everything()
    )
    if code == 1:
        assert "refusing to write" in capsys.readouterr().err
        assert not out.exists(), "an uncuttable plan was written anyway"


# -------------------------------------------------------------- split


def a_plan(tmp_path: Path) -> Path:
    p = tmp_path / "plan.json"
    p.write_text(
        json.dumps(
            {
                "slug": "album",
                "album": "A",
                "artist": "B",
                "sides": [
                    {
                        "file": "side-a.flac",
                        "tracks": [
                            {
                                "number": 1,
                                "title": "One",
                                "start": 15.35,
                                "end": 167.15,
                                "cat": 150.0,
                            },
                            {
                                "number": 2,
                                "title": "Two",
                                "start": 168.0,
                                "end": 300.0,
                                "cat": 130.0,
                            },
                        ],
                    }
                ],
            }
        )
    )
    return p


def test_split_dry_run_prints_the_cuts_and_writes_nothing(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "review"
    fake = everything()
    assert (
        main(
            ["split", str(a_plan(tmp_path)), str(tmp_path), str(out), "--dry-run"],
            runner=fake,
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "15.350" in printed and "01 One.flac" in printed
    assert not fake.calls, "a dry run ran something"
    assert not out.exists(), "a dry run created a directory"


def test_split_cuts_every_track(tmp_path: Path) -> None:
    fake = everything()
    out = tmp_path / "review"
    assert (
        main(["split", str(a_plan(tmp_path)), str(tmp_path), str(out)], runner=fake)
        == 0
    )
    cuts = [c for c in fake.calls if "-ss" in c]
    assert len(cuts) == 2
    assert out.is_dir()


def test_split_refuses_the_superseded_format(tmp_path: Path, capsys) -> None:
    p = tmp_path / "old.json"
    p.write_text(
        json.dumps(
            {
                "slug": "a",
                "sides": [{"file": "side-a.flac", "cuts": [1.0, 2.0], "tracks": []}],
            }
        )
    )
    assert (
        main(["split", str(p), str(tmp_path), str(tmp_path / "o")], runner=everything())
        == 2
    )
    assert "re-fit" in capsys.readouterr().err


def test_split_refuses_an_invalid_plan_before_cutting(tmp_path: Path, capsys) -> None:
    p = tmp_path / "bad.json"
    p.write_text(
        json.dumps(
            {
                "slug": "a",
                "sides": [
                    {
                        "file": "side-a.flac",
                        "tracks": [
                            {
                                "number": 1,
                                "title": "x",
                                "start": 100.0,
                                "end": 50.0,
                                "cat": 1.0,
                            }
                        ],
                    }
                ],
            }
        )
    )
    fake = everything()
    assert main(["split", str(p), str(tmp_path), str(tmp_path / "o")], runner=fake) == 1
    assert "refusing to cut" in capsys.readouterr().err
    assert not fake.calls, "it started cutting before checking"


# -------------------------------------------------------------- check


def test_check_builds_two_clips_per_track(tmp_path: Path) -> None:
    fake = everything()
    assert (
        main(
            ["check", str(a_plan(tmp_path)), str(tmp_path), str(tmp_path / "clips")],
            runner=fake,
        )
        == 0
    )
    clips = [c for c in fake.calls if "-filter_complex" in c]
    assert len(clips) == 4, "one clip per boundary, two per track"


# ------------------------------------------------------------ failures


def test_a_missing_tool_points_at_the_doctor(tmp_path: Path, capsys) -> None:
    """The error a person meets first should name the command that explains it."""
    fake = FakeRunner(installed={"flac"})
    assert (
        main(
            ["split", str(a_plan(tmp_path)), str(tmp_path), str(tmp_path / "o")],
            runner=fake,
        )
        == 3
    )
    assert "ripdoctor doctor" in capsys.readouterr().err


def test_a_tool_failure_is_reported_not_traced(tmp_path: Path, capsys) -> None:
    fake = everything().expect("ffmpeg", returncode=1, stderr=b"Invalid data found")
    assert (
        main(
            ["split", str(a_plan(tmp_path)), str(tmp_path), str(tmp_path / "o")],
            runner=fake,
        )
        == 1
    )
    assert "Invalid data found" in capsys.readouterr().err


def test_a_missing_file_is_reported_not_traced(tmp_path: Path, capsys) -> None:
    assert (
        main(
            ["split", str(tmp_path / "nope.json"), str(tmp_path), str(tmp_path / "o")],
            runner=everything(),
        )
        == 1
    )
    assert capsys.readouterr().err.strip()


@pytest.mark.parametrize(
    "argv",
    [["doctor"], ["config"], ["doctor", "--json"]],
)
def test_no_command_needs_a_working_machine(argv: list[str]) -> None:
    """The commands that explain a broken machine must run on one."""
    assert main(argv, runner=FakeRunner(installed=set())) in (0, 1)
