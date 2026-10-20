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
    if args.bundle:
        from .agents.bundle import bundle_json_schema

        print(json.dumps(bundle_json_schema(), indent=2))
        return 0
    print(json.dumps(findings_json_schema() if args.findings else json_schema(), indent=2))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import load_store, serve

    store = load_store([Path(p) for p in args.events or []], args.generate or [])
    options = {}
    if args.transport == "streamable-http":
        options = dict(host=args.host, port=args.port, stateless_http=args.stateless,
                       json_response=args.json_response, allowed_hosts=args.allowed_host or [],
                       allowed_origins=args.allowed_origin or [])
    serve(store, args.transport, **options)
    return 0


def _agent_settings(args: argparse.Namespace, usage_log: Path):
    """Settings for an agent run. Live calls need both PITCHSIDE_LLM=foundry and --live; offline runs log usage
    inside their own output directory, so fake costs never count toward the live budgets."""
    settings = load_settings()
    if settings.mode == "foundry" and not args.live:
        raise ValueError("PITCHSIDE_LLM=foundry makes paid model calls: add --live to confirm")
    if args.live and settings.mode != "foundry":
        raise ValueError("--live needs PITCHSIDE_LLM=foundry and the Foundry settings (see .env.example)")
    if settings.mode == "fake":
        settings = settings.model_copy(update={"usage_log": usage_log})
    return settings


def _fault_plan(args: argparse.Namespace):
    from .agents.faults import bad_claims_plan, combine, parse_faults

    return combine(parse_faults(args.inject_faults) if args.inject_faults else None,
                   bad_claims_plan(args.inject_bad) if args.inject_bad else None)


def _format_run(run, path: Path) -> str:
    lines = [f"== {run.match_id}  run {run.run_id}", f"   {_label(run)}"]
    for r in run.records:
        n = r.narrative
        text = n.text("casual") or n.headline
        semantic = "" if n.semantic_check in ("passed", "not_run") else f" [{n.semantic_check[:40]}]"
        lines.append(f"{int(r.clock_s // 60) + 1:>4}' {r.label:<15} {r.route:<8} {n.source:<8}{semantic}  {text}")
        if n.source == "template" and r.route == "model":
            lines.append(f"{'':>6}fallback: {n.reason}")
    model = [r for r in run.records if r.route == "model"]
    usage = [u for u in run.usage_log.read() if u.run_id == run.run_id]
    lines.append(f"   jobs {len(run.records)} (model {len(model)}, template {len(run.records) - len(model)}); "
                 f"model fallbacks {sum(r.narrative.source == 'template' for r in model)}; calls "
                 f"{sum(u.status != 'refused' for u in usage)}; cost ${sum(u.cost_usd for u in usage):.4f}"
                 + (" (fake client: token estimates, not real spend)" if run.settings.mode == "fake" else ""))
    if run.fault_plan:
        lines.append(f"   faults injected ({run.fault_plan.name}): {len(run.faults_fired)} fired")
    lines.append(f"   bundle: {path}")
    return "\n".join(lines)


def _label(run) -> str:
    from .agents.bundle import label_for

    return label_for(run.settings.mode, bool(run.fault_plan))


async def _run_one(result, pipeline, config, run_id):
    from .agents.pipeline import run_match

    try:
        return await run_match(result.events, pipeline, config, run_id=run_id, answer_key=result.answer_key)
    finally:
        await pipeline.aclose()


def _cmd_run(args: argparse.Namespace) -> int:
    import asyncio

    from .agents.bundle import bundle_dir_name, write_bundle
    from .agents.pipeline import RunConfig, build_pipeline, new_run_id

    result = generate_match(seed=args.seed, home=args.home, away=args.away, storyline=args.storyline)
    match_id = result.answer_key["match_id"]
    run_id = new_run_id(match_id)
    out = Path(args.out)
    settings = _agent_settings(args, out / bundle_dir_name(match_id, run_id) / "usage.jsonl")
    plan = _fault_plan(args)
    config = RunConfig.from_settings(settings, featured=tuple(args.fav_player) if args.fav_player else None)
    run = asyncio.run(_run_one(result, build_pipeline(settings, plan), config, run_id))
    path = write_bundle(run, out)
    print(_format_run(run, path))
    return 0


def _cmd_storylines(_: argparse.Namespace) -> int:
    for storyline in STORYLINES.values():
        print(f"{storyline.name:<12} {storyline.description}")
    return 0


def _cmd_eval_agents(args: argparse.Namespace) -> int:
    import asyncio

    from .agents.bundle import write_bundle
    from .agents.evaluate import evaluate_run, format_report, spotcheck, summarise, to_json
    from .agents.pipeline import RunConfig, build_pipeline, new_run_id

    eval_id = new_run_id("eval")
    out = Path(args.out) / eval_id
    settings = _agent_settings(args, out / "usage.jsonl")
    config = RunConfig.from_settings(settings)
    runs, evals = [], []
    for seed in _parse_seeds(args.seeds):
        for storyline in args.storylines:
            result = generate_match(seed=seed, storyline=storyline)
            run_id = f"{eval_id}-{len(runs) + 1:02d}"
            pipeline = build_pipeline(settings, _fault_plan(args))  # fresh per match: faults fire per match
            run = asyncio.run(_run_one(result, pipeline, config, run_id))
            ev = evaluate_run(run)
            if not args.no_bundles:
                write_bundle(run, out / "bundles", evaluation=to_json([ev], summarise(
                    [ev], settings.match_budget_usd, bool(args.inject_faults), bool(args.inject_bad)))["matches"][0])
            runs.append(run)
            evals.append(ev)
            print(f"  {run.match_id}: {ev.model_jobs} model jobs, {sum(ev.model_fallbacks.values())} fallbacks, "
                  f"${ev.cost_usd:.4f}", flush=True)
    summary = summarise(evals, settings.match_budget_usd, bool(args.inject_faults), bool(args.inject_bad))
    notes = [f"faults: {args.inject_faults}" if args.inject_faults else "",
             f"injected bad drafts: {args.inject_bad:g}" if args.inject_bad else ""]
    report = format_report(evals, summary, args.seeds, [n for n in notes if n])
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(report + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(to_json(evals, summary), indent=1) + "\n", encoding="utf-8")
    if settings.mode == "foundry":
        (out / "spotcheck.md").write_text(spotcheck(runs) + "\n", encoding="utf-8")
    print(report)
    print(f"written to {out}" + (" (includes spotcheck.md: label it by hand)" if settings.mode == "foundry" else ""))
    return 0 if summary.passed else 1


def _agent_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--live", action="store_true",
                        help="confirm paid model calls (needs PITCHSIDE_LLM=foundry); default is offline")
    parser.add_argument("--inject-faults", metavar="SPEC",
                        help="opt-in fault injection to show recovery: 'demo' or role:kind[@target][#attempt][xN]")
    parser.add_argument("--inject-bad", type=float, metavar="RATE",
                        help="corrupt the first explainer draft of this share of jobs (0-1), to test the verifier")
    parser.add_argument("--fav-player", action="append", metavar="PLAYER_ID",
                        help="favourite-player persona (repeatable; default: each club's striker)")


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

    sch = sub.add_parser("schema", help="print the JSON Schema of all events (or of findings, or of run bundles)")
    sch.add_argument("--findings", action="store_true", help="schema of stats-engine findings instead")
    sch.add_argument("--bundle", action="store_true", help="schema of run bundles instead")
    sch.set_defaults(func=_cmd_schema)

    rn = sub.add_parser("run", help="run a match through the stats engine and the agents; write a run bundle")
    rn.add_argument("--seed", type=int, default=7)
    rn.add_argument("--storyline", default="comeback", choices=sorted(STORYLINES))
    rn.add_argument("--home", default=DEFAULT_HOME, choices=sorted(CLUBS))
    rn.add_argument("--away", default=DEFAULT_AWAY, choices=sorted(CLUBS))
    rn.add_argument("--out", default="runs/bundles", help="bundle output directory")
    _agent_args(rn)
    rn.set_defaults(func=_cmd_run)

    mc = sub.add_parser("mcp", help="serve the stats engine as read-only MCP tools (stdio by default)")
    mc.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"],
                    help="stdio for local MCP clients; streamable-http for a deployed server")
    mc.add_argument("--events", action="append", metavar="PATH",
                    help="a recorded match (JSONL from `pitchside generate`); repeatable")
    mc.add_argument("--generate", action="append", metavar="SEED[:STORYLINE]",
                    help="generate a match (deterministic); repeatable. Default when nothing is given: 7:comeback")
    mc.add_argument("--host", default="127.0.0.1", help="streamable-http only; 0.0.0.0 inside a container")
    mc.add_argument("--port", type=int, default=8000, help="streamable-http only")
    mc.add_argument("--allowed-host", action="append", metavar="HOST",
                    help="streamable-http: Host header(s) to accept, e.g. the Container Apps FQDN (repeatable)")
    mc.add_argument("--allowed-origin", action="append", metavar="ORIGIN",
                    help="streamable-http: browser Origin(s) to accept (repeatable)")
    mc.add_argument("--stateless", action="store_true",
                    help="streamable-http: no server-side sessions (needed for several replicas / scale to zero)")
    mc.add_argument("--json-response", action="store_true", help="streamable-http: plain JSON replies, not SSE")
    mc.set_defaults(func=_cmd_mcp)

    ea = sub.add_parser("eval-agents", help="run matches through the agents and score them against answer keys "
                                            "(offline by default: a plumbing check, not model quality)")
    ea.add_argument("--seeds", default="1-3", help="e.g. 1-3 (offline default) or 201-205 (live, held out)")
    ea.add_argument("--storylines", nargs="+", default=list(STORYLINES), choices=sorted(STORYLINES))
    ea.add_argument("--out", default="runs/eval", help="output directory (report, summary, bundles)")
    ea.add_argument("--no-bundles", action="store_true", help="do not write a bundle per match")
    _agent_args(ea)
    ea.set_defaults(func=_cmd_eval_agents)
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
