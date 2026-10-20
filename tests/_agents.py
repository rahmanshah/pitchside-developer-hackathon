"""Shared helpers for the agent tests: one generated match per (seed, storyline), cached."""

from __future__ import annotations

from functools import lru_cache

from pitchside.agents.contracts import MatchFacts
from pitchside.agents.ledger import PlayerTally, match_facts
from pitchside.generator import MatchResult, generate_match
from pitchside.schema import Event
from pitchside.stats.engine import StatsEngine
from pitchside.stats.findings import Finding


@lru_cache(maxsize=None)
def match(seed: int = 1, storyline: str = "comeback") -> MatchResult:
    return generate_match(seed=seed, storyline=storyline)


@lru_cache(maxsize=None)
def findings(seed: int = 1, storyline: str = "comeback") -> tuple[Finding, ...]:
    engine = StatsEngine()
    out = []
    for event in match(seed, storyline).events:
        out.extend(engine.ingest(event))
    return tuple(out)


def facts(seed: int = 1, storyline: str = "comeback") -> MatchFacts:
    return match_facts(match(seed, storyline).events[0])


def events_by_id(seed: int = 1, storyline: str = "comeback") -> dict[int, Event]:
    return {e.event_id: e for e in match(seed, storyline).events}


def tally_until(seed: int, storyline: str, clock_s: float) -> PlayerTally:
    tally = PlayerTally()
    for event in match(seed, storyline).events:
        if event.clock_s > clock_s:
            break
        tally.update(event)
    return tally


def first(kind: str, seed: int = 1, storyline: str = "comeback", status: str = "moment") -> Finding:
    return next(f for f in findings(seed, storyline) if f.kind.value == kind and f.status == status)
