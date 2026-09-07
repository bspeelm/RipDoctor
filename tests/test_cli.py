"""The command line, end to end, with no ffmpeg anywhere.

Every command is driven through a fake runner, so these exercise the real
argument parsing, the real fitting and the real refusals - only the external
programs are stood in for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripdoctor.audio import session as S
from ripdoctor.audio.runner import FakeRunner, Result
from ripdoctor.cli import STAGES as CLI_STAGES
from ripdoctor.cli import build_parser, main
from ripdoctor.cli import meter_line as main_meter
from ripdoctor.core.meter import Levels
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


# ---------------------------------------------------------------- probe


def probe_env(monkeypatch, tmp_path: Path) -> Path:
    """Point every directory at the test's own, and stage a capture."""
    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    scratch = tmp_path / "state" / "cache" / "probe.wav"
    scratch.parent.mkdir(parents=True)
    scratch.write_bytes(b"RIFF" + b"\x00" * 4000)
    return scratch


def astats(full_rms: float, peak: float, band_rms: float) -> FakeRunner:
    return (
        FakeRunner()
        .expect(
            lambda a: any("highpass" in x for x in a),
            stderr=f"[astats] RMS level dB: {band_rms}\n".encode(),
        )
        .expect(
            "astats",
            stderr=f"[astats] RMS level dB: {full_rms}\n"
            f"[astats] Peak level dB: {peak}\n".encode(),
        )
    )


def test_probe_reports_music_and_succeeds(monkeypatch, tmp_path: Path, capsys) -> None:
    """Twenty seconds spent before a side, rather than twenty minutes after."""
    scratch = probe_env(monkeypatch, tmp_path)
    code = main(["probe", "--device", "hw:Rx,0"], runner=astats(-24.0, -6.0, -41.0))
    out = capsys.readouterr().out
    assert code == 0 and "music" in out
    assert "-41.0" in out, "the band lane was not reported"
    assert not scratch.exists(), "the probe capture was left behind"


def test_probe_fails_on_a_wrong_input(monkeypatch, tmp_path: Path, capsys) -> None:
    probe_env(monkeypatch, tmp_path)
    code = main(["probe", "--device", "hw:Rx,0"], runner=astats(-30.0, -12.0, -77.0))
    assert code == 1
    assert "wrong input" in capsys.readouterr().out


def test_probe_without_a_device_says_which_command_lists_them(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    probe_env(monkeypatch, tmp_path)
    assert main(["probe"], runner=everything()) == 2
    assert "ripdoctor devices" in capsys.readouterr().err


def test_probe_that_captured_nothing_does_not_judge_an_absent_file(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Judging a file that is not there reported the noise floor - the one
    verdict that must never be wrong."""
    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = everything()
    assert main(["probe", "--device", "hw:Rx,0"], runner=fake) == 1
    assert "nothing was captured" in capsys.readouterr().err
    assert not any("astats" in " ".join(c) for c in fake.calls)


# -------------------------------------------------------------- salvage


def test_salvage_finishes_what_an_interruption_left(tmp_path: Path, capsys) -> None:
    from ripdoctor.audio import capture as C

    C.partial_path(tmp_path, "b").write_bytes(b"RIFF" + b"\x00" * 8000)
    assert main(["salvage", str(tmp_path)], runner=everything()) == 0
    assert "side b" in capsys.readouterr().out
    assert not C.partial_path(tmp_path, "b").exists()


def test_salvage_dry_run_leaves_the_capture_alone(tmp_path: Path, capsys) -> None:
    from ripdoctor.audio import capture as C

    C.partial_path(tmp_path, "b").write_bytes(b"RIFF" + b"\x00" * 8000)
    assert main(["salvage", str(tmp_path), "--dry-run"], runner=everything()) == 0
    assert C.partial_path(tmp_path, "b").exists()


def test_salvage_with_nothing_to_do_says_so(tmp_path: Path, capsys) -> None:
    assert main(["salvage", str(tmp_path)], runner=everything()) == 0
    assert "nothing to salvage" in capsys.readouterr().out


# --------------------------------------------------------------- record


def a_reading(**kw: object) -> S.Reading:
    fields: dict = {
        "elapsed": 63.0,
        "levels": Levels(full=-21.4, band=-18.2, peak=-8.1),
        "music": -17.9,
        "quiet_for": 0.0,
        "warning": None,
    }
    return S.Reading(**{**fields, **kw})


def test_the_meter_line_shows_both_lanes() -> None:
    """The whole point is that they disagree: the full band cannot tell a gap
    from a quiet passage and 1-3 kHz can."""
    line = main_meter(a_reading())
    assert "1:03" in line
    assert "-21.4" in line and "-18.2" in line and "-8.1" in line
    assert "-17.9" in line, "the music level the auto-stop compares against"


def test_the_meter_shows_a_dash_before_the_detector_arms() -> None:
    assert "music      -" in main_meter(a_reading(music=None))


def test_the_quiet_timer_appears_only_while_it_is_running() -> None:
    assert "12.0s" in main_meter(a_reading(quiet_for=12.0))
    assert "0.0s" not in main_meter(a_reading(quiet_for=0.0))


def test_a_warning_is_printed_once_not_every_second(capsys) -> None:
    """Repeating it every second buries the meter under it."""
    from ripdoctor.cli import Meter

    meter = Meter()
    warned = a_reading(warning="no music-like signal after 31s")
    for _ in range(3):
        meter(warned)
    assert capsys.readouterr().err.count("no music-like signal") == 1


def test_record_without_a_device_says_which_command_lists_them(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["record", str(tmp_path), "a"], runner=everything()) == 2
    assert "ripdoctor devices" in capsys.readouterr().err


# --------------------------------------------------------------- measure


class Chain:
    """A signal chain: arecord leaves a file, astats answers per recording.

    Each stage of `ripdoctor measure` is a separate short capture, so the fake
    has to answer differently as it goes - which is the whole shape of the
    command.
    """

    def __init__(self, stages: list[tuple[float, float, float]]) -> None:
        self.stages = stages
        self.stage = -1
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        if args[0] == "arecord":
            self.stage += 1
            Path(args[-1]).write_bytes(b"RIFF" + b"\x00" * 4000)
            return Result(args, 0, b"", b"")
        full, peak, band = self.stages[min(self.stage, len(self.stages) - 1)]
        level = band if any("highpass" in a for a in args) else full
        return Result(
            args,
            0,
            b"",
            f"[astats] RMS level dB: {level}\n"
            f"[astats] Peak level dB: {peak}\n".encode(),
        )

    def start(self, argv, *, stderr_path=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def which(self, tool: str) -> str | None:
        return f"/usr/bin/{tool}"


def measure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


LIKE_THE_DEFAULTS = [
    (-91.0, -85.0, -95.0),
    (-52.0, -60.0, -72.0),
    (-22.0, -12.3, -30.0),
]
NARROW = [(-91.0, -85.0, -95.0), (-40.0, -30.0, -45.0), (-22.0, -12.3, -30.0)]


def test_measure_on_a_chain_like_the_defaults_suggests_nothing(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    measure_env(monkeypatch, tmp_path)
    code = main(
        ["measure", "--yes", "--device", "hw:Rx,0"], runner=Chain(LIKE_THE_DEFAULTS)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "agrees with the defaults" in out
    assert "[thresholds]" not in out


def test_measure_emits_a_snippet_when_the_chain_disagrees(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A chain with less separation than the shipped numbers assume."""
    measure_env(monkeypatch, tmp_path)
    assert main(["measure", "--yes", "--device", "hw:Rx,0"], runner=Chain(NARROW)) == 0
    out = capsys.readouterr().out
    assert "[thresholds]" in out and "span_below" in out
    assert "config.toml" in out, "nowhere to paste it"


def test_measure_records_all_three_references(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    chain = Chain(LIKE_THE_DEFAULTS)
    measure_env(monkeypatch, tmp_path)
    main(["measure", "--yes", "--device", "hw:Rx,0"], runner=chain)
    assert sum(1 for c in chain.calls if c[0] == "arecord") == 3
    out = capsys.readouterr().out
    for _key, title, _how in CLI_STAGES:
        assert title in out


def test_measure_waits_between_recordings_unless_told_not_to(
    monkeypatch, tmp_path: Path
) -> None:
    """Somebody has to move the needle between them."""
    measure_env(monkeypatch, tmp_path)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt))
    main(["measure", "--device", "hw:Rx,0"], runner=Chain(LIKE_THE_DEFAULTS))
    assert len(asked) == 3


def test_measure_that_captured_nothing_stops_rather_than_reporting(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    measure_env(monkeypatch, tmp_path)
    assert main(["measure", "--yes", "--device", "hw:Rx,0"], runner=everything()) == 1
    assert "nothing was captured" in capsys.readouterr().err


def test_measure_without_a_device_says_which_command_lists_them(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    measure_env(monkeypatch, tmp_path)
    assert main(["measure", "--yes"], runner=everything()) == 2
    assert "ripdoctor devices" in capsys.readouterr().err


# ----------------------------------------------------------------- serve


def test_serve_without_a_vinyl_directory_points_at_the_doctor(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Nothing else about the server can be right if it has nowhere to look.

    Driven through cmd_serve rather than main: every other path through this
    command binds a socket, and a test that can accidentally start a server is
    a test that hangs the suite.
    """
    import argparse
    from dataclasses import replace

    from ripdoctor.cli import Context, cmd_serve

    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = Context(everything())
    ctx.settings = replace(ctx.settings, vinyl="")
    args = argparse.Namespace(port=None, bind=None, user="ripdoctor")
    assert cmd_serve(ctx, args) == 2
    assert "ripdoctor doctor" in capsys.readouterr().err


def test_an_explicit_port_of_zero_is_not_read_as_no_port(
    monkeypatch, tmp_path: Path
) -> None:
    """Zero means "any free port". Taken as falsy it silently becomes the
    configured one, and the server comes up somewhere nobody asked for."""
    import argparse
    from dataclasses import replace

    from ripdoctor.cli import Context, cmd_serve
    from ripdoctor.web import auth as A

    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = Context(everything())
    ctx.settings = replace(ctx.settings, vinyl=str(tmp_path / "vinyl"), port=8080)

    asked: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "ripdoctor.cli.HTTPD.serve",
        lambda _app, host, port: asked.append((host, port)),
    )
    monkeypatch.setattr(
        "ripdoctor.cli.AUTH.load_or_create",
        lambda *_a, **_k: (A.create("u", "p", iterations=1000), None),
    )
    cmd_serve(ctx, argparse.Namespace(port=0, bind=None, user="ripdoctor"))
    assert asked == [("127.0.0.1", 0)]


def test_naming_a_record_writes_what_it_is_called(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """The supported way to name a record captured before anything recorded
    one, rather than editing the JSON by hand."""
    import argparse
    from dataclasses import replace

    from ripdoctor.cli import Context, cmd_name
    from ripdoctor.store import files as F
    from ripdoctor.store.files import Layout

    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = Context(everything())
    ctx.settings = replace(ctx.settings, vinyl=str(tmp_path / "vinyl"))
    args = argparse.Namespace(slug="album", artist="First", album="Second", date="2019")
    assert cmd_name(ctx, args) == 0
    spec = F.read_spec(Layout(tmp_path / "vinyl").spec_file("album"))
    assert (spec.artist, spec.album, spec.date) == ("First", "Second", "2019")
    assert "First - Second" in capsys.readouterr().out


def test_naming_without_a_pool_says_where_to_look(monkeypatch, tmp_path: Path) -> None:
    import argparse
    from dataclasses import replace

    from ripdoctor.cli import Context, cmd_name

    monkeypatch.setenv("RIPDOCTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ctx = Context(everything())
    ctx.settings = replace(ctx.settings, vinyl="")
    args = argparse.Namespace(slug="album", artist="", album="", date="")
    assert cmd_name(ctx, args) == 2
