"""Stats tools: read-only functions over a ``TimelineView`` that return typed results.

The same functions back the interpreter's local function tools and the MCP server (``pitchside.mcp_server``), so
what a model sees is identical whichever way it reaches them. A view is already bounded to one moment of one match
(see ``timeline.py``), so no function here can return anything from after that moment.

Every number comes back as a ``StatItem`` (metric, club, value, unit, time window, optional baseline). The agents
turn each item into an evidence-ledger entry with an id before a model sees it, so a number from a tool can be
checked exactly like a number from a finding.

Metric names are the ones findings use (``field_tilt``, ``pressures_per_min``, ``xg`` ...); since-kick-off values
add the suffix ``_total`` (``possession_total``).
"""

from __future__ import annotations

from typing import Callable, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Score
from .findings import Finding, FindingKind, Unit
from .metrics import TeamNumbers
from .timeline import TimelineError, TimelineView

Scope = Literal["rolling", "since_kickoff"]
DEFAULT_MOMENTS = 8
MAX_MOMENTS = 30


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StatItem(_Model):
    """One computed number."""

    metric: str = Field(description='Metric name, e.g. "field_tilt"; since-kick-off values end in "_total".')
    team_id: Optional[str] = Field(description="The club it is about; null for match-wide numbers.")
    value: float
    unit: Unit = Field(description="pct, per_min, xg, index, s, count or m.")
    window_start_s: float = Field(description="Start of the time window it covers (match seconds).")
    window_end_s: float = Field(description="End of the time window; never later than as_of_s.")
    baseline: Optional[float] = Field(default=None, description="What it is compared against, if anything.")


class StatsResult(_Model):
    match_id: str
    as_of_s: float = Field(description="The moment asked about (match seconds).")
    data_through_s: float = Field(description="Clock of the last event included; at most as_of_s.")
    score: Score = Field(description="The score at that moment.")
    scope: Scope = Field(description="rolling = the last few minutes; since_kickoff = the whole match so far.")
    items: list[StatItem]
    note: str = Field(default="", description="How to read the numbers.")


class MomentSummary(_Model):
    finding_id: str = Field(description="Pass this to evidence_lookup for the numbers behind it.")
    kind: FindingKind
    status: Literal["moment", "start", "end"] = Field(description="Spells have a start and, later, an end.")
    team_id: Optional[str] = Field(description="The club it is about; null for both.")
    minute: int = Field(description="Minute of play when it was detected (1 = first minute).")
    detected_at_s: float
    start_s: float
    end_s: float
    severity: float = Field(description="0 to 1: how notable it is.")
    score: Score = Field(description="The score when it was detected.")


class KeyMomentsResult(_Model):
    match_id: str
    as_of_s: float
    data_through_s: float
    moments: list[MomentSummary] = Field(description="Oldest first; the most recent ones if there are more than limit.")
    matching_so_far: int = Field(description="How many matching moments had been detected by as_of_s.")


class EvidenceResult(_Model):
    match_id: str
    as_of_s: float
    data_through_s: float
    finding: MomentSummary
    items: list[StatItem] = Field(description="The numbers the stats engine attached to the finding.")


# --------------------------------------------------------------------------- #
# Building results
# --------------------------------------------------------------------------- #

TeamSpec = tuple[str, str, Unit]  # (metric name, TeamNumbers attribute, unit)
NOTE_ROLLING = "Rolling window: the last few minutes before as_of_s (5 minutes for team numbers)."
NOTE_TOTAL = "Since kick-off, up to as_of_s."


def _result(view: TimelineView, scope: Scope, items: list[StatItem], note: str = "") -> StatsResult:
    return StatsResult(match_id=view.match_id, as_of_s=view.as_of_s, data_through_s=view.data_through_s,
                       score=view.score, scope=scope, items=items,
                       note=note or (NOTE_ROLLING if scope == "rolling" else NOTE_TOTAL))


def _team_items(view: TimelineView, scope: Scope, specs: Sequence[TeamSpec]) -> list[StatItem]:
    teams = view.snapshot.teams if scope == "rolling" else view.snapshot.totals
    items = []
    for team_id, numbers in teams.items():
        for metric, attr, unit in specs:
            value = getattr(numbers, attr)
            if value is None:
                continue
            items.append(StatItem(metric=metric if scope == "rolling" else f"{metric}_total", team_id=team_id,
                                  value=round(float(value), 3), unit=unit, window_start_s=numbers.window_start_s,
                                  window_end_s=numbers.window_end_s))
    return items


def _team_tool(specs: Sequence[TeamSpec]) -> Callable[[TimelineView, Scope], StatsResult]:
    def tool(view: TimelineView, scope: Scope = "rolling") -> StatsResult:
        return _result(view, scope, _team_items(view, scope, specs))
    return tool


possession = _team_tool([("possession", "possession_pct", "pct")])
field_tilt = _team_tool([("field_tilt", "field_tilt_pct", "pct"), ("final_third_passes", "final_third_passes", "count")])
pressure = _team_tool([("pressures_per_min", "pressures_per_min", "per_min"), ("pressures", "pressures", "count"),
                       ("tackles_won", "tackles_won", "count")])
passing = _team_tool([("passes", "passes", "count"), ("pass_completion", "pass_completion_pct", "pct"),
                      ("pass_difficulty", "pass_difficulty", "index")])
shots_and_xg = _team_tool([("shots", "shots", "count"), ("xg", "xg", "xg"), ("goals", "goals", "count")])


def match_totals(view: TimelineView) -> StatsResult:
    """Since kick-off, per club: possession, shots, xG and pass completion."""
    return _result(view, "since_kickoff", _team_items(view, "since_kickoff", [
        ("possession", "possession_pct", "pct"), ("shots", "shots", "count"), ("xg", "xg", "xg"),
        ("pass_completion", "pass_completion_pct", "pct")]))


def recent_window(view: TimelineView) -> StatsResult:
    """The rolling window, per club: field tilt, possession, shots, xG and pressures per minute."""
    return _result(view, "rolling", _team_items(view, "rolling", [
        ("field_tilt", "field_tilt_pct", "pct"), ("possession", "possession_pct", "pct"), ("shots", "shots", "count"),
        ("xg", "xg", "xg"), ("pressures_per_min", "pressures_per_min", "per_min")]))


def momentum(view: TimelineView) -> StatsResult:
    """Momentum from each club's point of view: -1 to +1, positive means that club is on top."""
    snap = view.snapshot
    home_id, away_id = list(snap.teams)
    n: TeamNumbers = snap.teams[home_id]
    items = [StatItem(metric="momentum", team_id=home_id, value=round(snap.momentum, 3), unit="index",
                      window_start_s=n.window_start_s, window_end_s=n.window_end_s),
             StatItem(metric="momentum", team_id=away_id, value=round(-snap.momentum, 3), unit="index",
                      window_start_s=n.window_start_s, window_end_s=n.window_end_s)]
    return _result(view, "rolling", items, "Positive: that club is on top over the last few minutes; the two are "
                                           "mirror images.")


def control_vs_chaos(view: TimelineView) -> StatsResult:
    """How settled or broken the play is, over the last 8 minutes: -1 controlled to +1 chaotic."""
    snap, m = view.snapshot, view.snapshot.match
    start, end = m.window_start_s, m.window_end_s
    items = [StatItem(metric="chaos_index", team_id=None, value=round(snap.chaos, 3), unit="index",
                      window_start_s=start, window_end_s=end),
             StatItem(metric="turnovers_per_min", team_id=None, value=round(m.turnovers_per_min, 3), unit="per_min",
                      window_start_s=start, window_end_s=end)]
    if m.pass_completion_pct is not None:
        items.append(StatItem(metric="pass_completion", team_id=None, value=round(m.pass_completion_pct, 3),
                              unit="pct", window_start_s=start, window_end_s=end))
    if m.mean_spell_s is not None:
        items.append(StatItem(metric="mean_spell", team_id=None, value=round(m.mean_spell_s, 3), unit="s",
                              window_start_s=start, window_end_s=end))
    return _result(view, "rolling", items, "Match-wide, both clubs together. chaos_index -1 controlled .. +1 chaotic.")


def _summary(finding: Finding) -> MomentSummary:
    return MomentSummary(finding_id=finding.finding_id, kind=finding.kind, status=finding.status,
                         team_id=finding.team_id, minute=int(finding.detected_at_s // 60) + 1,
                         detected_at_s=finding.detected_at_s, start_s=finding.start_s, end_s=finding.end_s,
                         severity=finding.severity, score=finding.score)


def key_moments(view: TimelineView, kinds: Optional[Sequence[FindingKind]] = None, min_severity: float = 0.0,
                limit: int = DEFAULT_MOMENTS) -> KeyMomentsResult:
    """Key moments the stats engine had detected by as_of_s (goals, big chances, spells), oldest first."""
    if not 1 <= limit <= MAX_MOMENTS:
        raise TimelineError(f"limit must be between 1 and {MAX_MOMENTS}")
    wanted = set(kinds) if kinds else None
    matching = [f for f in view.findings if (wanted is None or f.kind in wanted) and f.severity >= min_severity]
    return KeyMomentsResult(match_id=view.match_id, as_of_s=view.as_of_s, data_through_s=view.data_through_s,
                            moments=[_summary(f) for f in matching[-limit:]], matching_so_far=len(matching))


def evidence_lookup(view: TimelineView, finding_id: str) -> EvidenceResult:
    """The numbers behind one finding detected by as_of_s. A finding not detected yet is reported as unknown."""
    finding = next((f for f in view.findings if f.finding_id == finding_id), None)
    if finding is None:
        # Same message whether the id never existed or belongs to the future: the answer must not reveal which.
        raise TimelineError(f"no finding {finding_id!r} is known as of this moment (key_moments lists them)")
    items = [StatItem(metric=e.metric, team_id=e.team_id, value=e.value, unit=e.unit,
                      window_start_s=e.window_start_s, window_end_s=e.window_end_s, baseline=e.baseline)
             for e in finding.evidence]
    return EvidenceResult(match_id=view.match_id, as_of_s=view.as_of_s, data_through_s=view.data_through_s,
                          finding=_summary(finding), items=items)


# The tools the interpreter has when it uses local function tools (PITCHSIDE_TOOLS=local).
LOCAL_TOOLS: dict[str, Callable[[TimelineView], StatsResult]] = {"match_totals": match_totals,
                                                                 "recent_window": recent_window}
