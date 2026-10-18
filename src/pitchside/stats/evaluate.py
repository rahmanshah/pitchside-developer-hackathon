"""Score stats-engine findings against a generator answer key.

Matching rules (fixed before looking at the held-out seeds):

* A planted phase is scored only if its kind has a matching finding kind
  (``SCORED_KINDS``). ``low_block`` is reported as "not scored".
* A finding can only detect a phase if its kind is accepted for that phase
  kind and its team is one of the phase's teams (or it is a both-teams finding).
  Only ``start`` and ``moment`` findings count; the time is ``detected_at_s``.
* Spell phases: a **hit** is a detection with lag (detected_at_s - phase
  start) in [-1 min, +8 min]. A detection with lag in [-2 min, -1 min) is
  **premature**: counted separately and not a hit.
* ``counter_attack`` phases: a hit is a detection between phase start and
  phase end + 1 min. Nothing counts as premature.
* Recall = hits / scored phases.
* Goals: a goal finding must point at each goal's shot ``event_id``.
* A finding is **outside** the planted windows if it is a spell start or a
  counter-attack and its detection time is outside every planted phase
  (any kind, including ``low_block``) widened to [start - 2 min, end + 1 min].
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional, Sequence

from ..generator import generate_match
from .detector import DetectorConfig
from .engine import detect
from .findings import SPELL_KINDS, Finding, FindingKind

ACCEPTED: dict[str, frozenset[FindingKind]] = {
    "momentum_shift": frozenset({FindingKind.MOMENTUM_SHIFT, FindingKind.PRESSURE_SURGE}),
    "siege": frozenset({FindingKind.SIEGE}),
    "chaos": frozenset({FindingKind.CHAOS_SPELL}),
    "counter_attack": frozenset({FindingKind.COUNTER_ATTACK}),
}
SCORED_KINDS = frozenset(ACCEPTED)
MOMENT_PHASE_KINDS = frozenset({"counter_attack"})

HIT_EARLY_S = 60.0  # spell hit window starts 1 min before the phase
PREMATURE_EARLY_S = 120.0  # [-2 min, -1 min) is premature
HIT_LATE_S = 480.0  # spell hit window ends 8 min after phase start
MOMENT_LATE_S = 60.0  # counter hit window ends 1 min after phase end
OUTSIDE_EARLY_S = 120.0
OUTSIDE_LATE_S = 60.0

PhaseStatus = Literal["hit", "premature", "missed", "not_scored"]

STORYLINES = ("balanced", "comeback", "siege", "end_to_end")
TUNING_SEEDS = range(1, 7)  # thresholds were tuned on these only
HELD_OUT_SEEDS = range(101, 121)  # the bars are checked on these

# Pass bars, fixed after tuning and before the first held-out run.
MIN_RECALL = 0.80  # per storyline, over its scored phases
MAX_BALANCED_SPELLS_PER_MATCH = 1.0
MAX_OUTSIDE_SPELLS_PER_MATCH = 1.0  # per storyline
MAX_OUTSIDE_COUNTERS_PER_MATCH = 4.0  # quick transitions happen in normal play; a sanity cap only


@dataclass(frozen=True)
class PhaseResult:
    label: str
    kind: str
    team_ids: tuple[str, ...]
    start_s: float
    end_s: float
    status: PhaseStatus
    lag_s: Optional[float] = None  # hits and premature detections
    finding_id: Optional[str] = None


@dataclass(frozen=True)
class MatchEvaluation:
    match_id: str
    storyline: str
    seed: int
    phases: list[PhaseResult]
    goals_found: int
    goals_total: int
    outside: list[Finding]  # spell starts and counters outside every planted window
    spell_starts: int  # all spell start findings in the match


def _detections(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.status in ("start", "moment")]


def _team_ok(finding: Finding, team_ids: tuple[str, ...]) -> bool:
    return finding.team_id is None or finding.team_id in team_ids


def evaluate_match(findings: list[Finding], answer_key: dict[str, Any]) -> MatchEvaluation:
    detections = _detections(findings)
    phases: list[PhaseResult] = []
    for phase in answer_key["phases"]:
        kind, start, end = phase["kind"], float(phase["start_s"]), float(phase["end_s"])
        team_ids = tuple(phase["team_ids"])
        base = dict(label=phase["label"], kind=kind, team_ids=team_ids, start_s=start, end_s=end)
        if kind not in SCORED_KINDS:
            phases.append(PhaseResult(**base, status="not_scored"))
            continue
        candidates = sorted(
            (f for f in detections if f.kind in ACCEPTED[kind] and _team_ok(f, team_ids)),
            key=lambda f: f.detected_at_s,
        )
        if kind in MOMENT_PHASE_KINDS:
            hit = next((f for f in candidates if start <= f.detected_at_s <= end + MOMENT_LATE_S), None)
            premature = None
        else:
            hit = next((f for f in candidates if start - HIT_EARLY_S <= f.detected_at_s <= start + HIT_LATE_S), None)
            premature = next(
                (f for f in candidates if start - PREMATURE_EARLY_S <= f.detected_at_s < start - HIT_EARLY_S), None
            )
        if hit is not None:
            phases.append(PhaseResult(**base, status="hit", lag_s=hit.detected_at_s - start, finding_id=hit.finding_id))
        elif premature is not None:
            phases.append(PhaseResult(**base, status="premature", lag_s=premature.detected_at_s - start,
                                      finding_id=premature.finding_id))
        else:
            phases.append(PhaseResult(**base, status="missed"))

    goal_ids = {f.event_ids[0] for f in findings if f.kind is FindingKind.GOAL and f.event_ids}
    goals = answer_key["goals"]
    windows = [(p["start_s"] - OUTSIDE_EARLY_S, p["end_s"] + OUTSIDE_LATE_S) for p in answer_key["phases"]]
    outside = [
        f for f in detections
        if (f.kind in SPELL_KINDS or f.kind is FindingKind.COUNTER_ATTACK)
        and not any(lo <= f.detected_at_s <= hi for lo, hi in windows)
    ]
    return MatchEvaluation(
        match_id=answer_key["match_id"],
        storyline=answer_key["storyline"],
        seed=answer_key["seed"],
        phases=phases,
        goals_found=sum(g["event_id"] in goal_ids for g in goals),
        goals_total=len(goals),
        outside=outside,
        spell_starts=sum(f.kind in SPELL_KINDS and f.status == "start" for f in findings),
    )


# ---------------------------------------------------------------------- #
# Aggregation and the eval-stats table
# ---------------------------------------------------------------------- #


@dataclass
class KindSummary:
    phases: int = 0
    hits: int = 0
    premature: int = 0
    missed: int = 0
    lags_s: list[float] = field(default_factory=list)

    @property
    def recall(self) -> Optional[float]:
        return self.hits / self.phases if self.phases else None

    @property
    def median_lag_s(self) -> Optional[float]:
        return statistics.median(self.lags_s) if self.lags_s else None

    @property
    def worst_lag_s(self) -> Optional[float]:
        return max(self.lags_s, key=abs) if self.lags_s else None


@dataclass
class StorylineSummary:
    storyline: str
    matches: int = 0
    kinds: dict[str, KindSummary] = field(default_factory=dict)
    not_scored: dict[str, int] = field(default_factory=dict)
    goals_found: int = 0
    goals_total: int = 0
    outside_by_kind: dict[str, int] = field(default_factory=dict)
    spell_starts: list[int] = field(default_factory=list)

    @property
    def scored(self) -> KindSummary:
        total = KindSummary()
        for k in self.kinds.values():
            total.phases += k.phases
            total.hits += k.hits
            total.premature += k.premature
            total.missed += k.missed
            total.lags_s += k.lags_s
        return total

    @property
    def outside_total(self) -> int:
        return sum(self.outside_by_kind.values())

    @property
    def outside_spells(self) -> int:
        return sum(n for k, n in self.outside_by_kind.items() if FindingKind(k) in SPELL_KINDS)

    @property
    def outside_counters(self) -> int:
        return self.outside_by_kind.get(FindingKind.COUNTER_ATTACK.value, 0)


def summarise(evaluations: Iterable[MatchEvaluation]) -> dict[str, StorylineSummary]:
    out: dict[str, StorylineSummary] = {}
    for ev in evaluations:
        s = out.setdefault(ev.storyline, StorylineSummary(ev.storyline))
        s.matches += 1
        s.goals_found += ev.goals_found
        s.goals_total += ev.goals_total
        s.spell_starts.append(ev.spell_starts)
        for f in ev.outside:
            s.outside_by_kind[f.kind.value] = s.outside_by_kind.get(f.kind.value, 0) + 1
        for p in ev.phases:
            if p.status == "not_scored":
                s.not_scored[p.label] = s.not_scored.get(p.label, 0) + 1
                continue
            k = s.kinds.setdefault(p.kind, KindSummary())
            k.phases += 1
            if p.status == "hit":
                k.hits += 1
                k.lags_s.append(p.lag_s)
            elif p.status == "premature":
                k.premature += 1
            else:
                k.missed += 1
    return out


def run_evaluation(
    seeds: Iterable[int],
    storylines: Sequence[str] = STORYLINES,
    config: Optional[DetectorConfig] = None,
) -> list[MatchEvaluation]:
    """Generate each (storyline, seed) match in memory, detect, and score it. No files, no network."""
    evaluations = []
    for storyline in storylines:
        for seed in seeds:
            result = generate_match(seed=seed, storyline=storyline)
            evaluations.append(evaluate_match(detect(result.events, config), result.answer_key))
    return evaluations


def check_bars(summaries: dict[str, StorylineSummary]) -> list[tuple[str, bool]]:
    """Every pass bar as (description, passed)."""
    checks = []
    for name in sorted(summaries):
        s = summaries[name]
        if s.kinds:
            recall = s.scored.recall or 0.0
            checks.append((f"{name}: recall {recall:.0%} >= {MIN_RECALL:.0%}", recall >= MIN_RECALL))
        checks.append((f"{name}: goals found {s.goals_found}/{s.goals_total}", s.goals_found == s.goals_total))
        per_match = s.outside_spells / s.matches
        checks.append((f"{name}: spell findings outside planted windows {per_match:.2f}/match "
                       f"<= {MAX_OUTSIDE_SPELLS_PER_MATCH}", per_match <= MAX_OUTSIDE_SPELLS_PER_MATCH))
        per_match = s.outside_counters / s.matches
        checks.append((f"{name}: counter-attacks outside planted windows {per_match:.2f}/match "
                       f"<= {MAX_OUTSIDE_COUNTERS_PER_MATCH}", per_match <= MAX_OUTSIDE_COUNTERS_PER_MATCH))
    if "balanced" in summaries:
        mean = statistics.mean(summaries["balanced"].spell_starts)
        checks.append((f"balanced: spell findings {mean:.2f}/match <= {MAX_BALANCED_SPELLS_PER_MATCH}",
                       mean <= MAX_BALANCED_SPELLS_PER_MATCH))
    return checks


def _min(seconds: Optional[float]) -> str:
    return "-" if seconds is None else f"{seconds / 60:+.1f}"


def format_table(summaries: dict[str, StorylineSummary], seeds: str = "") -> str:
    lines = [f"Stats engine vs answer keys{(' (seeds ' + seeds + ')') if seeds else ''}", ""]
    header = f"{'storyline':<11} {'kind':<15} {'phases':>6} {'hits':>5} {'recall':>7} {'premat.':>7} " \
             f"{'missed':>6} {'lag med':>8} {'lag worst':>9}"
    lines += [header, "-" * len(header)]
    for name in sorted(summaries):
        s = summaries[name]
        for kind in sorted(s.kinds):
            k = s.kinds[kind]
            lines.append(
                f"{name:<11} {kind:<15} {k.phases:>6} {k.hits:>5} {k.recall:>7.0%} {k.premature:>7} "
                f"{k.missed:>6} {_min(k.median_lag_s):>8} {_min(k.worst_lag_s):>9}"
            )
        if s.kinds:
            t = s.scored
            lines.append(
                f"{name:<11} {'ALL SCORED':<15} {t.phases:>6} {t.hits:>5} {t.recall:>7.0%} {t.premature:>7} "
                f"{t.missed:>6} {_min(t.median_lag_s):>8} {_min(t.worst_lag_s):>9}"
            )
        for label, n in sorted(s.not_scored.items()):
            lines.append(f"{name:<11} {label:<15} {n:>6}   (not scored: no matching finding kind)")
    lines += ["", "lag = detected_at - phase start, in minutes. Hit window: spells -1..+8 min; "
              "counter_attack phase start..end+1 min. Premature: -2..-1 min (not a hit).", ""]

    lines.append(f"{'storyline':<11} {'matches':>7} {'goals':>9} {'outside/match':>13}  outside by kind")
    for name in sorted(summaries):
        s = summaries[name]
        by_kind = ", ".join(f"{k}={v}" for k, v in sorted(s.outside_by_kind.items())) or "-"
        lines.append(
            f"{name:<11} {s.matches:>7} {s.goals_found:>4}/{s.goals_total:<4} "
            f"{s.outside_total / s.matches:>13.2f}  {by_kind}"
        )
    if "balanced" in summaries:
        b = summaries["balanced"]
        lines += ["", f"balanced: spell findings per match = {statistics.mean(b.spell_starts):.2f} "
                      f"(per match: {b.spell_starts})"]
    lines.append("outside = spell starts and counter-attacks detected outside every planted window "
                 "(-2 min .. +1 min around each phase).")
    lines += ["", "Bars:"]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {text}" for text, ok in check_bars(summaries)]
    return "\n".join(lines)
