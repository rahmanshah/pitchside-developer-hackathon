"""Score agent runs against the generator's answer keys, and report grounding, fallbacks and cost.

Metrics (per match, then pooled):

* **Coverage**: planted phases the stats engine detected whose finding has a shown narrative naming the phase's
  club (split by model / template), and the same over *all* planted phases, so stats-engine misses are not hidden.
  Scripted goals narrated with the scoring club and the score after the goal. Recaps at half-time and full-time,
  the full-time one stating the final score.
* **Grounding**: share of first-draft claims that pass the deterministic checker; semantic rejections among the
  claims the semantic layer saw.
* **Unsupported claims before the verifier**: first-draft claims rejected by either layer. **After the verifier**:
  shown claims failing a fresh deterministic re-check. That is 0 by construction (the checker is the gate); a live
  run also writes ``spotcheck.md``, a sample of shown claims for a person to judge.
* **Injected bad claims caught** (``--inject-bad`` / fault plans): first drafts that were corrupted on purpose and
  had a claim rejected.
* **Fallbacks**: model jobs that ended with a template narrative, by reason; semantic checks skipped; persona
  texts replaced by templates.
* **Cost**: per match from the usage rows (offline: token estimates of the fake client, not money).

Offline numbers are **plumbing checks, not model quality**: the fake agents are correct by construction.
"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from ..llm.usage import total_cost
from ..schema import Score, ShotEvent
from ..stats.evaluate import evaluate_match
from ..stats.findings import FindingKind
from .checker import CheckContext, check_claim
from .contracts import GoalAt, JobRecord
from .faults import CORRUPTIONS
from .pipeline import MatchRun

OFFLINE_BANNER = ("OFFLINE EVALUATION (fake model client): PLUMBING CHECK, NOT MODEL QUALITY.\n"
                  "The fake agents are deterministic and correct by construction; these numbers show that the "
                  "pipeline is wired end to end\n(prompts, tools, checker, verifier loop, personalizer, recovery, "
                  "bundles). They say nothing about how well a real model writes.")
LIVE_BANNER = "LIVE EVALUATION (Microsoft Foundry model deployments): measured model behaviour on these seeds."
FALLBACK_CATEGORIES = ("budget", "checker rejected", "semantic verifier rejected", "explainer", "pipeline error")


@dataclass
class MatchEval:
    match_id: str
    storyline: str
    seed: int
    run_id: str
    mode: str
    phases_scored: int = 0
    phases_detected: int = 0
    phases_covered: int = 0
    phases_covered_model: int = 0
    goals_total: int = 0
    goals_narrated: int = 0
    goals_name_team: int = 0
    goals_state_score: int = 0
    recaps: int = 0
    recaps_model: int = 0
    final_score_stated: bool = False
    model_jobs: int = 0
    model_fallbacks: dict[str, int] = field(default_factory=dict)
    semantic_skipped: int = 0
    persona_texts: int = 0
    persona_template: int = 0
    first_draft_claims: int = 0
    first_draft_failed_deterministic: int = 0
    semantic_checked: int = 0
    semantic_rejected: int = 0
    first_draft_unsupported: int = 0
    shown_claims: int = 0
    shown_recheck_failures: int = 0
    drafts_per_job: list[int] = field(default_factory=list)
    injected: int = 0
    injected_caught: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    tokens: dict[str, dict[str, int]] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    budget_exhausted: Optional[str] = None


def _texts(record: JobRecord) -> str:
    n = record.narrative
    return " ".join([c.text for c in n.claims] + [t.text for t in n.texts if t.language == "en"])


def _category(reason: str) -> str:
    return next((c for c in FALLBACK_CATEGORIES if reason.startswith(c) or c in reason.split(":")[0]), "other")


def evaluate_run(run: MatchRun) -> MatchEval:
    key = run.answer_key or {}
    ev = MatchEval(match_id=run.match_id, storyline=key.get("storyline", "?"), seed=key.get("seed", -1),
                   run_id=run.run_id, mode=run.settings.mode, budget_exhausted=run.budget_exhausted)
    by_finding = {r.finding_id: r for r in run.records if r.finding_id}
    names = {t.team_id: t.name for t in run.facts.teams}

    # Coverage of planted phases and goals
    if key:
        stats = evaluate_match(run.findings, key)
        for phase in stats.phases:
            if phase.status == "not_scored":
                continue
            ev.phases_scored += 1
            if phase.status != "hit":
                continue
            ev.phases_detected += 1
            record = by_finding.get(phase.finding_id)
            if record is None:
                continue
            text = _texts(record)
            if record.team_id is None or any(names[t] in text for t in phase.team_ids):
                ev.phases_covered += 1
                ev.phases_covered_model += record.narrative.source == "model"
        goal_findings = {f.event_ids[0]: f for f in run.findings if f.kind is FindingKind.GOAL and f.event_ids}
        for goal in key.get("goals", []):
            ev.goals_total += 1
            finding = goal_findings.get(goal["event_id"])
            record = by_finding.get(finding.finding_id) if finding else None
            if record is None:
                continue
            ev.goals_narrated += 1
            text = _texts(record)
            ev.goals_name_team += names[goal["team_id"]] in text
            score = goal["score_after"]
            ev.goals_state_score += f"{score['home']}-{score['away']}" in text
        final = key.get("final_score")
        full_time = next((r for r in run.records if r.label == "full_time"), None)
        if final and full_time:
            ev.final_score_stated = f"{final['home']}-{final['away']}" in _texts(full_time)

    # Agents: grounding, retries, fallbacks
    corrupted = {f["job_id"] for f in run.faults_fired
                 if f["role"] == "explainer" and f["kind"] in (*CORRUPTIONS, "corrupt_all") and f["attempt"] == 1}
    for record in run.records:
        if record.kind == "recap":
            ev.recaps += 1
            ev.recaps_model += record.narrative.source == "model"
        if record.route != "model":
            if (record.narrative.reason or "").startswith("budget exhausted earlier"):
                ev.model_jobs += 1  # model-eligible, skipped for budget: a fallback, not a template route
                ev.model_fallbacks["budget"] = ev.model_fallbacks.get("budget", 0) + 1
            continue
        ev.model_jobs += 1
        n = record.narrative
        ev.drafts_per_job.append(len(record.drafts))
        if n.source == "template":
            category = _category(n.reason or "")
            ev.model_fallbacks[category] = ev.model_fallbacks.get(category, 0) + 1
        ev.semantic_skipped += n.semantic_check.startswith("skipped")
        if n.source == "model":
            ev.persona_texts += len(n.texts)
            ev.persona_template += sum(t.source == "template" for t in n.texts)
            ctx = CheckContext(ledger=record.ledger, facts=run.facts, score=_score_of(run, record),
                               goals=_goals_until(run, record.clock_s), window_start_s=_window(run, record)[0],
                               window_end_s=_window(run, record)[1])
            ev.shown_claims += len(n.claims)
            ev.shown_recheck_failures += sum(not check_claim(c, ctx).ok for c in n.claims)
        if record.drafts:
            first = record.drafts[0]
            det = {c.claim_id: c.ok for c in record.checks if c.layer == "deterministic" and c.attempt == 1}
            sem = {c.claim_id: c.ok for c in record.checks if c.layer == "semantic" and c.attempt == 1}
            ev.first_draft_claims += len(first.claims)
            ev.first_draft_failed_deterministic += sum(not det.get(c.claim_id, False) for c in first.claims)
            ev.semantic_checked += len(sem)
            ev.semantic_rejected += sum(not ok for ok in sem.values())
            ev.first_draft_unsupported += sum(not det.get(c.claim_id, False) or sem.get(c.claim_id) is False
                                              for c in first.claims)
            if record.job_id in corrupted:
                ev.injected += 1
                ev.injected_caught += any(not det.get(c.claim_id, False) or sem.get(c.claim_id) is False
                                          for c in first.claims) or not first.claims
        elif record.job_id in corrupted:
            ev.injected += 1  # the explainer failed before drafting; nothing reached a reader either

    # Cost
    usage = [u for u in (run.usage_log.read() if run.usage_log else []) if u.run_id == run.run_id]
    ev.cost_usd = total_cost(usage)
    ev.calls = sum(u.status != "refused" for u in usage)
    for u in usage:
        row = ev.tokens.setdefault(u.role, {"calls": 0, "input": 0, "output": 0, "reasoning": 0})
        row["calls"] += u.status != "refused"
        row["input"] += u.input_tokens
        row["output"] += u.output_tokens
        row["reasoning"] += u.reasoning_tokens
        if u.status == "ok":
            ev.latencies_ms.append(u.latency_ms)
    return ev


def _score_of(run: MatchRun, record: JobRecord):
    finding = next((f for f in run.findings if f.finding_id == record.finding_id), None)
    if finding is not None:
        return finding.score
    goals = _goals_until(run, record.clock_s)
    return goals[-1].score if goals else Score(home=0, away=0)


def _goals_until(run: MatchRun, clock_s: float) -> list[GoalAt]:
    return [GoalAt(clock_s=e.clock_s, team_id=e.team_id, player_id=e.player_id, score=e.score_after)
            for e in run.events if isinstance(e, ShotEvent) and e.outcome == "goal" and e.score_after
            and e.clock_s <= clock_s]


def _window(run: MatchRun, record: JobRecord) -> tuple[float, float]:
    finding = next((f for f in run.findings if f.finding_id == record.finding_id), None)
    return (finding.start_s, finding.end_s) if finding else (0.0, record.clock_s)


# --------------------------------------------------------------------------- #
# Pooling and the report
# --------------------------------------------------------------------------- #


def _rate(num: float, den: float) -> Optional[float]:
    return None if den == 0 else num / den


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.0%}"


@dataclass
class Summary:
    mode: str
    matches: int
    values: dict[str, Any]
    checks: list[tuple[str, bool]]

    @property
    def passed(self) -> bool:
        return all(ok for _, ok in self.checks)


def summarise(evals: Sequence[MatchEval], match_budget_usd: float, faults_planned: bool,
              injected_planned: bool) -> Summary:
    total = lambda name: sum(getattr(e, name) for e in evals)  # noqa: E731
    fallbacks: Counter[str] = Counter()
    for e in evals:
        fallbacks.update(e.model_fallbacks)
    drafts = [d for e in evals for d in e.drafts_per_job]
    costs = [e.cost_usd for e in evals]
    latencies = sorted(l for e in evals for l in e.latencies_ms)
    tokens: dict[str, dict[str, int]] = {}
    for e in evals:
        for role, row in e.tokens.items():
            agg = tokens.setdefault(role, {"calls": 0, "input": 0, "output": 0, "reasoning": 0})
            for k, v in row.items():
                agg[k] += v
    values = {
        "phases_scored": total("phases_scored"), "phases_detected": total("phases_detected"),
        "phase_coverage_of_detected": _rate(total("phases_covered"), total("phases_detected")),
        "phase_coverage_of_detected_by_model": _rate(total("phases_covered_model"), total("phases_detected")),
        "phase_coverage_of_planted": _rate(total("phases_covered"), total("phases_scored")),
        "goals": total("goals_total"),
        "goals_narrated": _rate(total("goals_narrated"), total("goals_total")),
        "goals_naming_scoring_club": _rate(total("goals_name_team"), total("goals_total")),
        "goals_stating_score": _rate(total("goals_state_score"), total("goals_total")),
        "recaps": total("recaps"), "recaps_from_model": total("recaps_model"),
        "full_time_recaps_stating_final_score": _rate(sum(e.final_score_stated for e in evals), len(evals)),
        "model_jobs": total("model_jobs"),
        "fallback_rate": _rate(sum(fallbacks.values()), total("model_jobs")),
        "fallbacks_by_reason": dict(fallbacks),
        "semantic_check_skipped_rate": _rate(total("semantic_skipped"), total("model_jobs")),
        "persona_texts_replaced_by_template": _rate(total("persona_template"), total("persona_texts")),
        "first_draft_claims": total("first_draft_claims"),
        "deterministic_grounding_rate": _rate(total("first_draft_claims") - total("first_draft_failed_deterministic"),
                                              total("first_draft_claims")),
        "semantic_rejection_rate": _rate(total("semantic_rejected"), total("semantic_checked")),
        "unsupported_before_verifier": _rate(total("first_draft_unsupported"), total("first_draft_claims")),
        "unsupported_after_verifier": _rate(total("shown_recheck_failures"), total("shown_claims")),
        "shown_claims": total("shown_claims"),
        "mean_drafts_per_model_job": statistics.mean(drafts) if drafts else None,
        "jobs_needing_a_retry": _rate(sum(d > 1 for d in drafts), len(drafts)),
        "injected_bad_drafts": total("injected"),
        "injected_bad_drafts_caught": _rate(total("injected_caught"), total("injected")),
        "cost_per_match_mean_usd": statistics.mean(costs) if costs else 0.0,
        "cost_per_match_max_usd": max(costs) if costs else 0.0,
        "calls_per_match_mean": statistics.mean(e.calls for e in evals) if evals else 0,
        "tokens_by_role": tokens,
        "reasoning_share_of_output": _rate(sum(r["reasoning"] for r in tokens.values()),
                                           sum(r["output"] for r in tokens.values())),
        "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
        "latency_ms_p95": latencies[int(len(latencies) * 0.95)] if latencies else None,
        "budget_exhausted_matches": sum(e.budget_exhausted is not None for e in evals),
    }
    mode = evals[0].mode if evals else "fake"
    checks: list[tuple[str, bool]] = [
        ("every detected planted phase has a narrative naming its club",
         values["phase_coverage_of_detected"] in (None, 1.0)),
        ("every scripted goal is narrated, naming the scoring club",
         values["goals_naming_scoring_club"] in (None, 1.0)),
        ("both recaps exist for every match", values["recaps"] == 2 * len(evals)),
        ("no shown claim fails a fresh deterministic re-check", values["unsupported_after_verifier"] in (None, 0.0)),
        ("no match ran out of budget", values["budget_exhausted_matches"] == 0),
        (f"cost per match within the ${match_budget_usd:.2f} match budget",
         values["cost_per_match_max_usd"] <= match_budget_usd + 1e-9),
    ]
    if mode == "fake":
        checks += [
            ("goals state the score after the goal", values["goals_stating_score"] in (None, 1.0)),
            ("full-time recaps state the final score", values["full_time_recaps_stating_final_score"] in (None, 1.0)),
            ("every injected bad draft is caught", values["injected_bad_drafts_caught"] in (None, 1.0)),
        ]
        if not faults_planned and not injected_planned:
            checks += [("no fallbacks without injected faults", values["fallback_rate"] in (None, 0.0)),
                       ("every first-draft claim passes the checker (fake agents)",
                        values["deterministic_grounding_rate"] in (None, 1.0))]
        if injected_planned:
            checks.append(("bad drafts were actually injected", values["injected_bad_drafts"] > 0))
    return Summary(mode=mode, matches=len(evals), values=values, checks=checks)


def format_report(evals: Sequence[MatchEval], summary: Summary, seeds: str, notes: Sequence[str] = ()) -> str:
    v = summary.values
    banner = OFFLINE_BANNER if summary.mode == "fake" else LIVE_BANNER
    rule = "=" * 100
    lines = [rule, banner, rule, f"seeds {seeds}; {summary.matches} match(es); {', '.join(notes)}".rstrip("; "), ""]
    lines.append(f"{'match':<32} {'phases':>10} {'goals':>8} {'recaps':>7} {'model jobs':>11} {'fallbacks':>10} "
                 f"{'1st-draft ok':>13} {'cost $':>9}")
    for e in evals:
        first_ok = _rate(e.first_draft_claims - e.first_draft_failed_deterministic, e.first_draft_claims)
        lines.append(f"{e.match_id:<32} {e.phases_covered:>4}/{e.phases_detected:<2}/{e.phases_scored:<2} "
                     f"{e.goals_name_team:>3}/{e.goals_total:<4} {e.recaps_model:>3}/{e.recaps:<3} "
                     f"{e.model_jobs:>11} {sum(e.model_fallbacks.values()):>10} {_pct(first_ok):>13} "
                     f"{e.cost_usd:>9.4f}")
    lines.append("(phases: covered / detected by the stats engine / planted)")
    lines += ["", "Coverage",
              f"  planted phases covered: {_pct(v['phase_coverage_of_detected'])} of the {v['phases_detected']} the "
              f"stats engine detected ({_pct(v['phase_coverage_of_detected_by_model'])} by a model narrative); "
              f"{_pct(v['phase_coverage_of_planted'])} of all {v['phases_scored']} planted (stats recall included)",
              f"  goals: narrated {_pct(v['goals_narrated'])}, naming the scoring club "
              f"{_pct(v['goals_naming_scoring_club'])}, stating the score {_pct(v['goals_stating_score'])} "
              f"(of {v['goals']})",
              f"  recaps: {v['recaps']} ({v['recaps_from_model']} from the model); full-time recap states the final "
              f"score: {_pct(v['full_time_recaps_stating_final_score'])}",
              "", "Grounding",
              f"  first-draft claims passing the deterministic checker: {_pct(v['deterministic_grounding_rate'])} "
              f"(of {v['first_draft_claims']})",
              f"  semantic rejections among claims the semantic layer saw: {_pct(v['semantic_rejection_rate'])}",
              f"  unsupported claims before the verifier: {_pct(v['unsupported_before_verifier'])}; after: "
              f"{_pct(v['unsupported_after_verifier'])} of {v['shown_claims']} shown (0 by construction: the "
              "checker is the gate)",
              f"  injected bad drafts caught: {_pct(v['injected_bad_drafts_caught'])} "
              f"(of {v['injected_bad_drafts']})",
              f"  drafts per model job: {v['mean_drafts_per_model_job'] or 0:.2f}; jobs needing a retry: "
              f"{_pct(v['jobs_needing_a_retry'])}",
              "", "Recovery",
              f"  model jobs ending in a template: {_pct(v['fallback_rate'])} of {v['model_jobs']} "
              f"{v['fallbacks_by_reason'] or ''}",
              f"  semantic check skipped: {_pct(v['semantic_check_skipped_rate'])}; persona texts replaced by a "
              f"template: {_pct(v['persona_texts_replaced_by_template'])}",
              "", "Cost" + (" (fake client: token estimates priced at PITCHSIDE_PRICE_MODEL rates, not real spend)"
                            if summary.mode == "fake" else " (measured)"),
              f"  per match: mean ${v['cost_per_match_mean_usd']:.4f}, max ${v['cost_per_match_max_usd']:.4f}; "
              f"calls per match {v['calls_per_match_mean']:.1f}; reasoning share of output tokens "
              f"{_pct(v['reasoning_share_of_output'])}"]
    for role, row in sorted(v["tokens_by_role"].items()):
        lines.append(f"    {role:<13} calls {row['calls']:>5}  input {row['input']:>8}  output {row['output']:>7}  "
                     f"reasoning {row['reasoning']:>7}")
    if v["latency_ms_p50"] is not None and summary.mode != "fake":
        lines.append(f"  latency per call: p50 {v['latency_ms_p50']:.0f} ms, p95 {v['latency_ms_p95']:.0f} ms")
    lines += ["", "Checks"]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {name}" for name, ok in summary.checks]
    lines += ["", f"RESULT: {'PASS' if summary.passed else 'FAIL'}" + (
        "  (offline plumbing check, not model quality)" if summary.mode == "fake" else ""), rule, banner, rule]
    return "\n".join(lines)


def spotcheck(runs: Sequence[MatchRun], n: int = 20, seed: int = 1) -> str:
    """A sample of shown model claims with their evidence, for a person to label supported / unsupported."""
    claims = [(run, r, c) for run in runs for r in run.records if r.narrative.source == "model"
              for c in r.narrative.claims]
    sample = random.Random(seed).sample(claims, min(n, len(claims)))
    lines = ["# Spot check: shown model claims", "",
             "For each claim, mark **supported** or **unsupported** by its evidence, with a reason.", ""]
    for i, (run, record, claim) in enumerate(sample, 1):
        lines += [f"## {i}. {record.job_id} ({int(record.clock_s // 60) + 1}')", "", f"> {claim.text}", "",
                  "Evidence:"]
        for ref in claim.refs:
            item, player = record.ledger.item(ref), record.ledger.player(ref)
            if item:
                lines.append(f"- `{ref}` {item.metric} = {item.value:g} {item.unit} ({item.team_id or 'both clubs'}, "
                             f"{item.window_start_s:.0f}-{item.window_end_s:.0f} s): {item.say}")
            elif player:
                lines.append(f"- `{ref}` {player.name} ({player.team_id}, {player.involvement})")
        lines += ["", "Verdict: [ ] supported  [ ] unsupported   Reason: ______", ""]
    return "\n".join(lines)


def to_json(evals: Sequence[MatchEval], summary: Summary) -> dict[str, Any]:
    return {"mode": summary.mode, "label": OFFLINE_BANNER.splitlines()[0] if summary.mode == "fake" else LIVE_BANNER,
            "passed": summary.passed, "checks": [{"check": n, "passed": ok} for n, ok in summary.checks],
            "summary": summary.values, "matches": [asdict(e) for e in evals]}
