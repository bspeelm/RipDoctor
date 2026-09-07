"""Command-line entry point. The only module permitted to call sys.exit."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from ripdoctor import __version__
from ripdoctor.audio.devices import enumerate_devices
from ripdoctor.audio.devices import report as devices_report
from ripdoctor.audio.runner import RealRunner, Runner, ToolFailed, ToolMissing
from ripdoctor.audio.split import cut_one, plan_cuts, tick_one
from ripdoctor.config.machine import Machine, detect
from ripdoctor.config.settings import Settings, describe, load
from ripdoctor.config.thresholds import Thresholds
from ripdoctor.core import gaps as G
from ripdoctor.core.envelope import Envelope, decode
from ripdoctor.core.fit import fit_side, report, to_side
from ripdoctor.core.plan import BadPlan, OldFormat, Plan, Spec, validate
from ripdoctor.doctor import checks as D
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.integrations import tagger as T


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

    sides = []
    for side in spec.sides:
        source = envelopes.get(side.letter)
        if source is None:
            print(f"no envelope given for side {side.letter}", file=sys.stderr)
            return 2
        env = _read_envelope(source, args.lane)
        # The anchor has to match the lane. core.gaps will not guess which one
        # an envelope is, and neither will this.
        found = (
            G.find(env, above=ctx.thresholds.gap_above)
            if args.lane == "band"
            else G.find(env, below=ctx.thresholds.gap_below)
        )
        fitted = fit_side(
            side,
            env,
            found,
            duration=env.duration,
            lead=spec.lead,
            tail=spec.tail,
        )
        print(f"=== side {side.letter}")
        print(report(fitted))
        sides.append(to_side(side.letter, fitted))

    plan = Plan(
        slug=spec.slug,
        album=spec.album,
        artist=spec.artist,
        sides=tuple(sides),
        date=spec.date,
    )
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
