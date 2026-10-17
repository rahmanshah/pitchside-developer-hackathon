"""Command line interface: ``pitchside generate | replay | summary | schema | storylines``."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional, Sequence

from .generator import generate_match
from .jsonl import read_events, write_answer_key, write_events
from .replay import replay
from .schema import Event, PassEvent, ShotEvent, json_schema, to_json_line
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


def _cmd_schema(_: argparse.Namespace) -> int:
    print(json.dumps(json_schema(), indent=2))
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

    sub.add_parser("schema", help="print the JSON Schema of all events").set_defaults(func=_cmd_schema)
    sub.add_parser("storylines", help="list available storylines").set_defaults(func=_cmd_storylines)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
