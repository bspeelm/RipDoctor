"""Command-line entry point. The only module permitted to call sys.exit."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from ripdoctor import __version__
from ripdoctor.audio import capture as CAP
from ripdoctor.audio import session as SESSION
from ripdoctor.audio.devices import enumerate_devices
from ripdoctor.audio.devices import report as devices_report
from ripdoctor.audio.runner import RealRunner, Runner, ToolFailed, ToolMissing
from ripdoctor.audio.split import cut_one, plan_cuts, tick_one
from ripdoctor.config.machine import Machine, detect
from ripdoctor.config.settings import Settings, describe, load
from ripdoctor.config.thresholds import Thresholds
from ripdoctor.core import autostop as A
from ripdoctor.core import measure as MEASURE
from ripdoctor.core.envelope import Envelope, decode
from ripdoctor.core.fit import fit_plan, report
from ripdoctor.core.meter import Levels, Verdict
from ripdoctor.core.plan import BadPlan, OldFormat, Plan, Spec, validate
from ripdoctor.doctor import checks as D
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.integrations import tagger as T
from ripdoctor.store.files import Layout
from ripdoctor.web import auth as AUTH
from ripdoctor.web import httpd as HTTPD
from ripdoctor.web import routes as ROUTES
from ripdoctor.web import static as STATIC
from ripdoctor.web.service import Service as SERVICE

# The front end ships inside the package, so an install has it and a source tree
# runs against the same files.
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class Context:
    """What every command needs, built once."""

    def __init__(self, runner: Runner | None = None) -> None:
        self.machine: Machine = detect()
        self.settings: Settings = load(self.machine)
        self.thresholds = Thresholds()
        self.runner: Runner = runner or RealRunner()


def _read_envelope(path: Path, lane: str) -> Envelope:
    """A cached envelope file, or a JSON file carrying one lane."""
    if path.suffix == ".env":
        lanes = decode(path.read_bytes())
        return lanes.band if lane == "band" else lanes.full
    data = json.loads(path.read_text())
    levels = base64.b64decode(data["levels_b64"])
    return Envelope(tuple(v * 0.5 - 127.5 for v in levels), data["window"])


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> int:
    results = D.run_all(ctx.machine, ctx.settings, ctx.thresholds, ctx.runner)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": D.ok(results),
                    "checks": [
                        {
                            "check": r.check,
                            "level": r.level.value,
                            "summary": r.summary,
                            "fix": r.fix,
                        }
                        for r in results
                    ],
                    "settings": describe(ctx.settings, ctx.thresholds),
                },
                indent=1,
                default=str,
            )
        )
    else:
        print(D.report(results))
    return 0 if D.ok(results) else 1


def cmd_config(ctx: Context, args: argparse.Namespace) -> int:
    print(json.dumps(describe(ctx.settings, ctx.thresholds), indent=1, default=str))
    return 0


def cmd_devices(ctx: Context, args: argparse.Namespace) -> int:
    """List capture devices. Nothing here decides which one is the turntable."""
    found = enumerate_devices(ctx.runner)
    print(devices_report(found, ctx.settings.capture_device))
    return 0 if found else 1


def _format(ctx: Context) -> CAP.Format:
    return CAP.Format(
        rate=ctx.settings.capture_rate,
        channels=ctx.settings.capture_channels,
        sample_format=ctx.settings.capture_format,
    )


def _short_capture(ctx: Context, device: str, seconds: float) -> Verdict | None:
    """Record briefly, measure it whole, and throw the audio away."""
    scratch = Path(ctx.machine.cache_dir) / "probe.wav"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    argv = CAP.test_capture_argv(device, str(scratch), _format(ctx), seconds)
    try:
        ctx.runner.run(argv, timeout=seconds + 30).require()
        if not scratch.is_file() or scratch.stat().st_size < 1024:
            return None
        return CAP.judge(ctx.runner, str(scratch))
    finally:
        scratch.unlink(missing_ok=True)


def _device_or_fail(ctx: Context, override: str | None) -> str:
    device = override or ctx.settings.capture_device
    CAP.check_device(device)
    return device


def cmd_probe(ctx: Context, args: argparse.Namespace) -> int:
    """Record briefly and say what arrived.

    Turns "drop the needle, wait twenty minutes, find out it was the wrong
    input" into a twenty-second question.
    """
    try:
        device = _device_or_fail(ctx, args.device)
    except CAP.CaptureError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"recording {args.seconds:.0f}s from {device} ...")
    v = _short_capture(ctx, device, args.seconds)
    if v is None:
        print("nothing was captured", file=sys.stderr)
        return 1

    print(f"  full band  rms {v.full_rms:>7.1f}  peak {v.full_peak:>7.1f}")
    print(f"  1-3 kHz    rms {v.band_rms:>7.1f}")
    print(f"\n  {v.summary}")
    return 0 if v.ok else 1


# What each reference recording is of, in the order they are asked for. Dead air
# first because it needs nothing set up, and music last because by then the
# record is already playing.
STAGES = (
    ("dead", "dead air", "amplifier on, needle up, nothing playing"),
    ("groove", "silent groove", "needle down on the lead-in or run-out"),
    ("music", "music", "a loud passage playing"),
)


def cmd_measure(ctx: Context, args: argparse.Namespace) -> int:
    """Report what this chain does beside the numbers this ships with.

    It suggests and does not write. The shipped thresholds came from one
    turntable through one converter; a command that rewrote them from three
    short recordings would be shipping a guess with the authority of a
    measurement. ADR-008.
    """
    try:
        device = _device_or_fail(ctx, args.device)
    except CAP.CaptureError as e:
        print(str(e), file=sys.stderr)
        return 2

    taken: dict[str, Levels] = {}
    for key, title, how in STAGES:
        print(f"\n  {title}: {how}")
        if not args.yes:
            input("  press Enter when ready ")
        print(f"  recording {args.seconds:.0f}s ...")
        v = _short_capture(ctx, device, args.seconds)
        if v is None:
            print(f"nothing was captured for {title}", file=sys.stderr)
            return 1
        taken[key] = Levels(full=v.full_rms, band=v.band_rms, peak=v.full_peak)

    ref = MEASURE.Reference(**taken)
    findings = MEASURE.compare(ref, ctx.thresholds.as_dict())
    print()
    print(MEASURE.report(ref, findings))

    text = MEASURE.snippet(findings)
    if not text:
        print("\n  nothing to change - this chain agrees with the defaults")
        return 0
    print(f"\n  paste this into {ctx.machine.settings_file}:\n")
    print(text)
    return 0


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60:>2}:{int(seconds) % 60:02d}"


def meter_line(r: SESSION.Reading) -> str:
    """The one line somebody watches for twenty minutes.

    Both lanes, because the whole point is that they disagree: the full band
    cannot tell a gap from a quiet passage and 1-3 kHz can. The music level and
    the quiet timer say how close the auto-stop is to firing.
    """
    music = f"{r.music:>6.1f}" if r.music is not None else "     -"
    quiet = f"{r.quiet_for:>5.1f}s" if r.quiet_for else "      "
    return (
        f"  {_clock(r.elapsed)}  full {r.levels.full:>6.1f}  "
        f"1-3k {r.levels.band:>6.1f}  peak {r.levels.peak:>6.1f}  "
        f"music {music}  quiet {quiet}"
    )


class Meter:
    """Prints the live line, and each warning exactly once.

    Repeating the warning every second would bury the meter under it, and the
    warning is the thing worth reading.
    """

    def __init__(self) -> None:
        self.said: set[str] = set()

    def __call__(self, r: SESSION.Reading) -> None:
        print("\r" + meter_line(r), end="", flush=True)
        if r.warning and r.warning not in self.said:
            self.said.add(r.warning)
            print(f"\n  {r.warning}", file=sys.stderr, flush=True)


def cmd_record(ctx: Context, args: argparse.Namespace) -> int:
    """Record one side, watching it, and encode what was captured.

    The meter runs here rather than in a browser: a meter that stopped when
    somebody closed a laptop lid would stop in the middle of every side.
    """
    device = args.device or ctx.settings.capture_device
    try:
        CAP.check_device(device)
    except CAP.CaptureError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"recording side {args.side} from {device} - Ctrl-C to stop")
    outcome = SESSION.record(
        ctx.runner,
        device,
        args.album,
        args.side,
        CAP.Format(
            rate=ctx.settings.capture_rate,
            channels=ctx.settings.capture_channels,
            sample_format=ctx.settings.capture_format,
        ),
        autostop=not args.no_autostop,
        on_reading=Meter(),
        dwell=A.DWELL if args.dwell is None else args.dwell,
        max_seconds=(
            A.MAX_SECONDS if args.max_minutes is None else args.max_minutes * 60.0
        ),
    )
    print(f"\n\n  stopped: {outcome.reason}")
    print(f"  {_clock(outcome.seconds)} captured")
    if outcome.overruns:
        # Each one is a slice of the record that is not in the file.
        print(
            f"  {outcome.overruns} overruns, {outcome.overrun_ms:.0f} ms lost",
            file=sys.stderr,
        )
    if outcome.path is None:
        print(f"  {outcome.error}", file=sys.stderr)
        return 1
    print(f"  -> {outcome.path}")
    return 0


def cmd_salvage(ctx: Context, args: argparse.Namespace) -> int:
    """Find and finish captures an interrupted session left behind."""
    found = CAP.salvageable(args.album)
    if not found:
        print("nothing to salvage")
        return 0
    for partial in found:
        letter = CAP.letter_of(partial)
        size = partial.stat().st_size / 1e6
        print(f"  side {letter}: {size:.0f} MB")
        if not args.dry_run:
            out = CAP.finish(ctx.runner, args.album, letter)
            print(f"    -> {out}")
    return 0


def cmd_serve(ctx: Context, args: argparse.Namespace) -> int:
    """Run the web interface."""
    if not ctx.settings.vinyl:
        print(
            "no vinyl directory is configured - run `ripdoctor doctor`",
            file=sys.stderr,
        )
        return 2

    layout = Layout(Path(ctx.settings.vinyl))
    layout.ensure()
    credentials, generated = AUTH.load_or_create(
        ctx.machine.config_dir / "auth.json", args.user
    )
    if generated:
        # The only time it is ever visible: it is hashed on the way to disk and
        # cannot be recovered from the file.
        print("=" * 68)
        print("  first run - a login was generated")
        print(f"    user:     {credentials.user}")
        print(f"    password: {generated}")
        print("  Store it now; it is not recoverable.")
        print("=" * 68)

    service = SERVICE(
        layout=layout,
        settings=ctx.settings,
        thresholds=ctx.thresholds,
        runner=ctx.runner,
        credentials=credentials,
        sessions=AUTH.Sessions(secret=credentials.secret),
    )
    # Checked against None rather than truthiness: port 0 means "any free
    # port", and `or` turns that into the configured one without a word.
    bind = ctx.settings.bind if args.bind is None else args.bind
    port = ctx.settings.port if args.port is None else args.port
    if bind not in ("127.0.0.1", "localhost", "::1"):
        # Worth saying out loud: on any other address the login is the only
        # thing between the network and a program that writes to the pool.
        print(f"  reachable from the network on {bind} - the login is the only control")
    app = ROUTES.build(service, static=STATIC.handler(STATIC_DIR))
    HTTPD.serve(app, bind, port)
    return 0


def cmd_lookup(ctx: Context, args: argparse.Namespace) -> int:
    """Find candidate releases, most usable first."""
    fetcher = MB.HttpFetcher()
    try:
        found = MB.search(fetcher, args.artist, args.album)
        for r in found[: args.limit]:
            MB.fetch_tracks(fetcher, r)
    except MB.LookupFailed as e:
        print(str(e), file=sys.stderr)
        return 1

    if not found:
        print("nothing matched", file=sys.stderr)
        return 1
    print(MB.report(MB.rank(found[: args.limit])))
    return 0


def cmd_fit(ctx: Context, args: argparse.Namespace) -> int:
    """Fit a spec against envelopes already measured, and write a plan."""
    spec = Spec.from_dict(json.loads(Path(args.spec).read_text()))
    envelopes = {p.stem.split("-")[-1]: p for p in map(Path, args.envelope)}

    lanes = {}
    for side in spec.sides:
        source = envelopes.get(side.letter)
        if source is None:
            print(f"no envelope given for side {side.letter}", file=sys.stderr)
            return 2
        lanes[side.letter] = _read_envelope(source, args.lane)

    # The anchor travels with the lane; neither this nor the core will guess.
    anchor = (
        {"above": ctx.thresholds.gap_above}
        if args.lane == "band"
        else {"below": ctx.thresholds.gap_below}
    )
    plan, working = fit_plan(spec, lanes, **anchor)
    for letter, fitted in working.items():
        print(f"=== side {letter}")
        print(report(fitted))

    try:
        validate(plan)
    except BadPlan as e:
        print(f"\nrefusing to write this plan: {e}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else Path(f"{spec.slug}.plan.json")
    out.write_text(json.dumps(plan.to_dict(), indent=1) + "\n")
    print(f"\nplan -> {out}")
    return 0


def _load_plan(path: str) -> Plan | None:
    try:
        return Plan.from_dict(json.loads(Path(path).read_text()))
    except OldFormat as e:
        print(str(e), file=sys.stderr)
        return None


def cmd_split(ctx: Context, args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    if plan is None:
        return 2
    try:
        validate(plan)
    except BadPlan as e:
        print(f"refusing to cut from this plan: {e}", file=sys.stderr)
        return 1

    cuts = plan_cuts(plan, args.source, args.out)
    if args.dry_run:
        for c in cuts:
            print(f"  {c.track.start:9.3f} {c.track.end:9.3f}  {c.dest}")
        return 0

    Path(args.out).mkdir(parents=True, exist_ok=True)
    for c in cuts:
        cut_one(ctx.runner, c)
        print(f"  {c.dest}")
    print(f"\n{len(cuts)} tracks -> {args.out}")
    return 0


def cmd_check(ctx: Context, args: argparse.Namespace) -> int:
    """Build one tick clip per boundary, for listening to."""
    plan = _load_plan(args.plan)
    if plan is None:
        return 2

    Path(args.out).mkdir(parents=True, exist_ok=True)
    n = 0
    for side in plan.sides:
        source = f"{args.source}/{side.file}"
        for t in side.tracks:
            for edge, at in (("start", t.start), ("end", t.end)):
                n += 1
                dest = f"{args.out}/{n:02d} track {t.number} {edge}.flac"
                tick_one(ctx.runner, source, at, dest)
                print(f"  {dest}")
    print(f"\n{n} clips -> {args.out}")
    return 0


def cmd_import(ctx: Context, args: argparse.Namespace) -> int:
    """Tag the cut tracks and place them in the library."""
    plan = _load_plan(args.plan)
    if plan is None:
        return 2
    try:
        validate(plan)
    except BadPlan as e:
        print(f"refusing to import from this plan: {e}", file=sys.stderr)
        return 1

    library = args.library or ctx.settings.library
    if not library:
        print("no library directory is set; see `ripdoctor doctor`", file=sys.stderr)
        return 2

    if args.dry_run:
        for p in T.placements(plan, args.review, library):
            print(f"  {p.source}  ->  {p.dest}")
        return 0

    moved = T.apply(
        ctx.runner,
        plan,
        args.review,
        library,
        file_mode=ctx.settings.file_mode,
        dir_mode=ctx.settings.dir_mode,
    )
    where, count = T.locate(library, plan.artist, plan.album)
    print(f"\n{len(moved)} tracks -> {where}")
    print(f"the library now holds {count} track(s) for this record")
    return 0


def cmd_archive(ctx: Context, args: argparse.Namespace) -> int:
    """Say whether the raw sides are safe to clear, and why."""
    plan = _load_plan(args.plan)
    if plan is None:
        return 2
    library = args.library or ctx.settings.library
    where, count = T.locate(library, plan.artist, plan.album)
    expected = sum(len(s.tracks) for s in plan.sides)

    if where is None:
        print(f"not in the library yet: {library}", file=sys.stderr)
        return 1
    if count < expected:
        print(f"{count} of {expected} tracks are in {where}", file=sys.stderr)
        return 1
    print(f"all {expected} tracks are in {where}")
    print("the raw sides are safe to archive")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ripdoctor",
        description="Capture a vinyl side, find the track boundaries, "
        "cut them where you want them.",
    )
    p.add_argument("--version", action="version", version=f"ripdoctor {__version__}")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("doctor", help="check this machine and the configuration")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.set_defaults(run=cmd_doctor)

    c = sub.add_parser("config", help="print every resolved setting")
    c.set_defaults(run=cmd_config)

    v = sub.add_parser("devices", help="list capture devices")
    v.set_defaults(run=cmd_devices)

    sv2 = sub.add_parser("serve", help="run the web interface")
    sv2.add_argument("--port", type=int, help="override the configured port")
    sv2.add_argument("--bind", help="override the configured address")
    sv2.add_argument("--user", default="ripdoctor", help="login name on first run")
    sv2.set_defaults(run=cmd_serve)

    ms = sub.add_parser("measure", help="compare this chain against the defaults")
    ms.add_argument("--device", help="override the configured device")
    ms.add_argument("--seconds", type=float, default=CAP.TEST_SECONDS)
    ms.add_argument(
        "--yes", action="store_true", help="do not wait for Enter between recordings"
    )
    ms.set_defaults(run=cmd_measure)

    pr = sub.add_parser("probe", help="record briefly and say what arrived")
    pr.add_argument("--device", help="override the configured device")
    pr.add_argument("--seconds", type=float, default=CAP.TEST_SECONDS)
    pr.set_defaults(run=cmd_probe)

    rc = sub.add_parser("record", help="record one side, watching it")
    rc.add_argument("album", help="directory to write the side into")
    rc.add_argument("side", help="side letter")
    rc.add_argument("--device", help="override the configured device")
    rc.add_argument(
        "--no-autostop",
        action="store_true",
        help="for a record that is quiet throughout; the hard cap still applies",
    )
    rc.add_argument("--dwell", type=float, help="seconds of run-out before stopping")
    rc.add_argument("--max-minutes", type=float, help="hard cap on the capture")
    rc.set_defaults(run=cmd_record)

    sv = sub.add_parser("salvage", help="finish captures an interruption left")
    sv.add_argument("album", help="directory holding the sides")
    sv.add_argument("--dry-run", action="store_true")
    sv.set_defaults(run=cmd_salvage)

    lk = sub.add_parser("lookup", help="find a release in the catalogue")
    lk.add_argument("artist")
    lk.add_argument("album")
    lk.add_argument("--limit", type=int, default=5)
    lk.set_defaults(run=cmd_lookup)

    f = sub.add_parser("fit", help="place boundaries from a spec and envelopes")
    f.add_argument("spec")
    f.add_argument("envelope", nargs="+", help="one per side, named side-<letter>")
    f.add_argument("-o", "--out", help="where to write the plan")
    f.add_argument(
        "--lane",
        choices=("band", "full"),
        default="band",
        help="which lane the envelopes hold; the anchor follows it",
    )
    f.set_defaults(run=cmd_fit)

    s = sub.add_parser("split", help="cut a plan into tracks")
    s.add_argument("plan")
    s.add_argument("source", help="directory holding the side files")
    s.add_argument("out", help="directory to write tracks into")
    s.add_argument("--dry-run", action="store_true", help="print the cuts only")
    s.set_defaults(run=cmd_split)

    i = sub.add_parser("import", help="tag the cut tracks and place them")
    i.add_argument("plan")
    i.add_argument("review", help="directory holding the cut tracks")
    i.add_argument("--library", help="override the configured library root")
    i.add_argument("--dry-run", action="store_true", help="print the moves only")
    i.set_defaults(run=cmd_import)

    a = sub.add_parser("archive", help="check a record arrived before clearing raw")
    a.add_argument("plan")
    a.add_argument("--library", help="override the configured library root")
    a.set_defaults(run=cmd_archive)

    k = sub.add_parser("check", help="build tick clips for listening to")
    k.add_argument("plan")
    k.add_argument("source")
    k.add_argument("out")
    k.set_defaults(run=cmd_check)

    return p


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "run", None) is None:
        parser.print_help()
        return 0
    try:
        code: int = args.run(Context(runner), args)
    except ToolMissing as e:
        print(
            f"{e}\nrun `ripdoctor doctor` to see what else is missing", file=sys.stderr
        )
        return 3
    except (ToolFailed, OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return code


def _unused() -> Any:  # pragma: no cover
    return None


if __name__ == "__main__":
    raise SystemExit(main())
