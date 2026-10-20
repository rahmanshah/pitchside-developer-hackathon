"""Turn findings and period ends into jobs, with the context frozen at that moment of the match.

Freezing the context (snapshot, recent moments, goals, player numbers) when the finding is detected keeps runs
deterministic and stops a model from seeing anything that happens later in the match.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Literal, Optional, Sequence, Union

from ..schema import Event, MatchEndEvent, MatchStartEvent, PeriodEndEvent, Score, ShotEvent
from ..stats.engine import Snapshot, StatsEngine
from ..stats.findings import Finding
from .contracts import GoalAt, Job, JobRecord, MatchFacts, Moment, RecapKind
from .ledger import PlayerTally, default_featured, finding_ledger, match_facts, recap_ledger

RECENT_MOMENTS = 5
STATS_EVERY_S = 30.0


def route(finding: Finding) -> Literal["model", "template"]:
    """Model-eligible findings and spell starts go to the agents; spell ends and the rest get a template."""
    return "model" if finding.can_trigger_model_call and finding.status != "end" else "template"


@dataclass(frozen=True)
class FindingPoint:
    finding: Finding
    job: Job
    route: Literal["model", "template"]


@dataclass(frozen=True)
class RecapPoint:
    kind: RecapKind
    make: Callable[[Sequence[JobRecord]], Job]  # given the records so far, the recap job


@dataclass(frozen=True)
class StatsPoint:
    snapshot: Snapshot


@dataclass(frozen=True)
class StartPoint:
    facts: MatchFacts
    featured: list[str]


Point = Union[StartPoint, FindingPoint, RecapPoint, StatsPoint]


def walk_match(events: Iterable[Event], featured: Optional[Sequence[str]] = None,
               stats_every_s: float = STATS_EVERY_S) -> Iterator[Point]:
    """Replay a match through the stats engine and yield what the pipeline acts on, in match order."""
    engine, tally = StatsEngine(), PlayerTally()
    facts: Optional[MatchFacts] = None
    events_by_id: dict[int, Event] = {}
    goals: list[GoalAt] = []
    seen: list[Finding] = []
    chosen: list[str] = list(featured or [])
    next_stats = 0.0
    for event in events:
        events_by_id[event.event_id] = event
        tally.update(event)
        if isinstance(event, MatchStartEvent):
            facts = match_facts(event)
            chosen = chosen or default_featured(facts)
            unknown = [p for p in chosen if facts.player(p) is None]
            if unknown:
                raise ValueError(f"favourite player(s) not in this match: {', '.join(unknown)}")
            engine.ingest(event)
            yield StartPoint(facts, chosen)
            continue
        assert facts is not None, "the stream must start with match_start"
        if isinstance(event, ShotEvent) and event.outcome == "goal" and event.score_after is not None:
            goals.append(GoalAt(clock_s=event.clock_s, team_id=event.team_id, player_id=event.player_id,
                                score=event.score_after))
        new = engine.ingest(event)
        if event.clock_s >= next_stats:
            yield StatsPoint(engine.snapshot())
            next_stats = (event.clock_s // stats_every_s + 1) * stats_every_s
        for finding in new:
            job = finding_job(finding, facts=facts, events_by_id=events_by_id, tally=tally,
                              snapshot=engine.snapshot(), earlier=seen, goals=goals, featured=chosen)
            seen.append(finding)
            yield FindingPoint(finding, job, route(finding))
        recap: Optional[RecapKind] = None
        if isinstance(event, PeriodEndEvent) and event.period == 1:
            recap = "half_time"
        elif isinstance(event, MatchEndEvent):
            recap = "full_time"
        if recap is not None:
            frozen = dict(facts=facts, snapshot=engine.snapshot(), tally=copy.deepcopy(tally),
                          score=engine.state.score, goals=list(goals), featured=list(chosen))
            yield RecapPoint(recap, lambda records, kind=recap, frozen=frozen: recap_job(kind, records=records,
                                                                                          **frozen))


def recap_job_id(match_id: str, kind: RecapKind) -> str:
    return f"{match_id}:recap:{kind}"


def finding_job(finding: Finding, *, facts: MatchFacts, events_by_id: dict[int, Event], tally: PlayerTally,
                snapshot: Snapshot, earlier: Sequence[Finding], goals: Sequence[GoalAt],
                featured: Sequence[str]) -> Job:
    recent = [Moment(finding_id=f.finding_id, kind=f.kind.value, status=f.status, team_id=f.team_id,
                     minute=int(f.detected_at_s // 60) + 1)
              for f in earlier if f.status != "end"][-RECENT_MOMENTS:]
    ledger = finding_ledger(finding, facts, events_by_id, tally)
    involved = [p.player_id for p in ledger.players]
    return Job(
        job_id=finding.finding_id, kind="finding", label=finding.kind.value, match_id=finding.match_id,
        finding=finding, team_id=finding.team_id, clock_s=finding.detected_at_s, window_start_s=finding.start_s,
        window_end_s=finding.end_s, score=finding.score, goals=list(goals), recent=recent, ledger=ledger,
        snapshot=snapshot, player_stats=[tally.get(p, facts.player(p).team_id) for p in dict.fromkeys(
            [*involved, *featured])], featured=list(featured), facts=facts,
    )


def recap_job(kind: RecapKind, *, facts: MatchFacts, snapshot: Snapshot, tally: PlayerTally, score: Score,
              goals: Sequence[GoalAt], featured: Sequence[str], records: Sequence[JobRecord]) -> Job:
    ledger, earlier = recap_ledger(snapshot, facts, tally, featured, [g.player_id for g in goals], records)
    return Job(
        job_id=recap_job_id(facts.match_id, kind), kind="recap", label=kind, match_id=facts.match_id,
        team_id=None, clock_s=snapshot.clock_s, window_start_s=0.0, window_end_s=snapshot.clock_s, score=score,
        goals=list(goals), ledger=ledger, snapshot=snapshot,
        player_stats=[s for s in tally.all() if s.player_id in {p.player_id for p in ledger.players}],
        featured=list(featured), earlier=earlier, facts=facts,
    )
