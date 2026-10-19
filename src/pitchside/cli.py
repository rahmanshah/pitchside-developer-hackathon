"""Command line interface: ``pitchside generate | replay | summary | findings | eval-stats | cost | schema | storylines``."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional, Sequence

from .generator import generate_match
from .jsonl import read_events, write_answer_key, write_events
from .llm import load_settings
from .llm.usage import UsageLog, format_summary
from .llm.usage import summarise as summarise_usage
from .replay import replay
from .schema import Event, PassEvent, ShotEvent, json_schema, to_json_line
from .stats import Finding, StatsEngine, findings_json_schema
from .stats.evaluate import check_bars, format_table, run_evaluation, summarise
from .storylines import STORYLINES
from .teams import CLUBS, DEFAULT_AWAY, DEFAULT_HOME


def _summarize(events: Sequence[Event]) -> str:
    counts = Counter(e.type for e in events)
    passes = [e for e in events if isinstance(e, PassEvent)]
    shots = [e for e in events if isinstance(e, ShotEvent)]
    completion = sum(p.outcome == "complete" for p in passes) / len(passes) if passes else 0.0
    lines = [
        f"events: {len(events)}  ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})",
        f"pass completion: {completion:.0%}   shots: {len(shots)}   "
        f"goals: {sum(s.outcome == 'goal' for s in shots)}",
    ]
    for team in sorted({s.team_id for s in shots}):
        mine = [s for s in shots if s.team_id == team]
        lines.append(
            f"  {team}: {len(mine)} shots, xG {sum(s.xg for s in mine):.2f}, "
            f"{sum(s.outcome == 'goal' for s in mine)} goals"
        )
    return "\n".join(lines)


def _cmd_generate(args: argparse.Namespace) -> int:
    result = generate_match(seed=args.seed, home=args.home, away=args.away, storyline=args.storyline)
    match_id = result.answer_key["match_id"]
    events_path = Path(args.out) / f"{match_id}.jsonl"
    key_path = Path(args.out) / f"{match_id}.key.json"
    write_events(result.events, events_path)
    write_answer_key(result.answer_key, key_path)
    print(f"wrote {events_path}\nwrote {key_path}\n{_summarize(result.events)}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    speed = None if args.instant else args.speed
    replay(read_events(Path(args.path)), lambda e: print(to_json_line(e), flush=True), speed=speed)
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    print(_summarize(list(read_events(Path(args.path)))))
    return 0


def _parse_seeds(text: str) -> list[int]:
    """'101-120' or '1,2,5' or '1-6,10' -> sorted unique seeds."""
    seeds: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            low, high = (int(x) for x in part.split("-", 1))
            if low > high:
                raise ValueError(f"bad seed range {part!r}")
            seeds.update(range(low, high + 1))
        elif part:
            seeds.add(int(part))
    if not seeds:
        raise ValueError("no seeds given")
    return sorted(seeds)


def _describe(finding: Finding) -> str:
    minute = finding.detected_at_s / 60
    team = finding.team_id or "both"
    numbers = ", ".join(f"{e.metric}={e.value:g}" for e in finding.evidence[:3])
    return f"{minute:5.1f}'  {finding.kind.value:<15} {finding.status:<6} {team:<5} sev {finding.severity:.2f}  {numbers}"


def _cmd_findings(args: argparse.Namespace) -> int:
    engine = StatsEngine()
    for event in read_events(Path(args.path)):
        for finding in engine.ingest(event):
            print(finding.model_dump_json() if args.json else _describe(finding), flush=True)
    return 0


def _cmd_eval_stats(args: argparse.Namespace) -> int:
    summaries = summarise(run_evaluation(_parse_seeds(args.seeds), args.storylines))
    print(format_table(summaries, args.seeds))
    return 0 if all(ok for _, ok in check_bars(summaries)) else 1


def _cmd_cost(args: argparse.Namespace) -> int:
    log = UsageLog(Path(args.log) if args.log else load_settings().usage_log)
    if not log.path.exists():
        print(f"no model usage logged yet ({log.path})")
        return 0
    records = [r for r in log.read() if args.match is None or r.match_id == args.match]
    rows, total = summarise_usage(records, by=args.by)
    print(format_summary(rows, total, args.by))
    if log.bad_lines:
        print(f"warning: skipped {log.bad_lines} unreadable line(s) in {log.path}", file=sys.stderr)
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    print(json.dumps(findings_json_schema() if args.findings else json_schema(), indent=2))
    return 0


def _cmd_storylines(_: argparse.Namespace) -> int:
    for storyline in STORYLINES.values():
        print(f"{storyline.name:<12} {storyline.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pitchside", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate a synthetic match")
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--storyline", default="comeback", choices=sorted(STORYLINES))
    gen.add_argument("--home", default=DEFAULT_HOME, choices=sorted(CLUBS))
    gen.add_argument("--away", default=DEFAULT_AWAY, choices=sorted(CLUBS))
    gen.add_argument("--out", default="matches", help="output directory")
    gen.set_defaults(func=_cmd_generate)

    rep = sub.add_parser("replay", help="stream a recorded match to stdout at adjustable speed")
    rep.add_argument("path")
    rep.add_argument("--speed", type=float, default=60.0, help="x real time (60 = 90 min in 90 s)")
    rep.add_argument("--instant", action="store_true", help="no delay between events")
    rep.set_defaults(func=_cmd_replay)

    summ = sub.add_parser("summary", help="quick sanity summary of a recorded match")
    summ.add_argument("path")
    summ.set_defaults(func=_cmd_summary)

    fnd = sub.add_parser("findings", help="run the stats engine over a recorded match and print its findings")
    fnd.add_argument("path")
    fnd.add_argument("--json", action="store_true", help="one JSON finding per line")
    fnd.set_defaults(func=_cmd_findings)

    ev = sub.add_parser("eval-stats", help="score the stats engine against generated answer keys")
    ev.add_argument("--seeds", default="101-120", help="e.g. 101-120 (held out, default) or 1-6 (tuning)")
    ev.add_argument("--storylines", nargs="+", default=list(STORYLINES), choices=sorted(STORYLINES))
    ev.set_defaults(func=_cmd_eval_stats)

    cost = sub.add_parser("cost", help="summarise model token usage and cost from the usage log")
    cost.add_argument("--log", help="usage log (default: PITCHSIDE_USAGE_LOG or runs/llm_usage.jsonl)")
    cost.add_argument("--match", help="only this match id")
    cost.add_argument("--by", default="role", choices=["role", "match", "model"])
    cost.set_defaults(func=_cmd_cost)

    sch = sub.add_parser("schema",help="print the JSON Schema of all events (or of findings)")
    sch.add_argument("--findings", action="store_true", help="schema of stats-engine findings instead")
    sch.set_defaults(func=_cmd_schema)
    sub.add_parser("storylines", help="list available storylines").set_defaults(func=_cmd_storylines)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as error:  # includes llm.ConfigError
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
