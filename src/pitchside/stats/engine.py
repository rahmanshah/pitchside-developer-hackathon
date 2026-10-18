"""The stats engine: feed it events one at a time, get numbers and findings back.

    engine = StatsEngine()
    for event in stream:
        for finding in engine.ingest(event):
            ...
    engine.snapshot()  # every current number, e.g. for an MCP tool

Deterministic: the same events always give the same findings.
"""

from __future__ import annotations

from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict

from ..schema import Event, Score
from .detector import DetectorConfig, KeyMomentDetector
from .findings import Finding
from .indices import chaos, momentum
from .metrics import SHORT_WINDOW_S, WINDOW_S, MatchNumbers, MatchState, TeamNumbers


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str
    clock_s: float
    score: Score
    window_s: float
    teams: dict[str, TeamNumbers]  # rolling window
    totals: dict[str, TeamNumbers]  # since kick-off
    match: MatchNumbers
    momentum: float  # -1..+1, positive = home on top
    chaos: float  # -1 controlled .. +1 chaotic


class StatsEngine:
    def __init__(
        self,
        config: Optional[DetectorConfig] = None,
        window_s: float = WINDOW_S,
        short_window_s: float = SHORT_WINDOW_S,
    ) -> None:
        self.state = MatchState(window_s=window_s, short_window_s=short_window_s)
        self.detector = KeyMomentDetector(self.state, config)
        self.findings: list[Finding] = []

    def ingest(self, event: Event) -> list[Finding]:
        """Update the numbers with one event and return any new findings."""
        self.state.update(event)
        new = self.detector.on_event(event)
        self.findings.extend(new)
        return new

    def snapshot(self) -> Snapshot:
        state = self.state
        if state.match_id is None:
            raise ValueError("no match_start ingested yet")
        home, away = state.team(state.home_id), state.team(state.away_id)
        match = state.match()
        return Snapshot(
            match_id=state.match_id,
            clock_s=state.clock_s,
            score=state.score,
            window_s=state.window_s,
            teams={home.team_id: home, away.team_id: away},
            totals={tid: state.totals(tid) for tid in (state.home_id, state.away_id)},
            match=match,
            momentum=momentum(home, away),
            chaos=chaos(match),
        )


def detect(events: Iterable[Event], config: Optional[DetectorConfig] = None) -> list[Finding]:
    """Run a whole match through a fresh engine and return every finding."""
    engine = StatsEngine(config)
    for event in events:
        engine.ingest(event)
    return engine.findings
