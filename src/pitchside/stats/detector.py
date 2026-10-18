"""Key-moment detector: turns MatchState numbers into typed findings.

Every rule is plain arithmetic on the rolling numbers, with its thresholds in
``DetectorConfig``. Each finding carries the numbers that triggered it as
evidence, with their time windows, so a later agent can explain the moment
and a verifier can check the explanation.

Momentum evidence is always given from the finding team's point of view
(positive = that team on top).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..schema import Event, ShotEvent
from .findings import Evidence, Finding, FindingKind, FindingStatus, Unit
from .indices import momentum
from .metrics import MatchState


@dataclass(frozen=True)
class DetectorConfig:
    big_chance_xg: float = 0.25


class KeyMomentDetector:
    def __init__(self, state: MatchState, config: Optional[DetectorConfig] = None) -> None:
        self.state = state
        self.config = config or DetectorConfig()
        self._seq: dict[FindingKind, int] = {}

    def on_event(self, event: Event) -> list[Finding]:
        """Call after `state.update(event)`. Returns the findings this event produced."""
        found: list[Finding] = []
        if isinstance(event, ShotEvent):
            found.extend(self._on_shot(event))
        return found

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _finding(
        self,
        kind: FindingKind,
        status: FindingStatus,
        team_id: Optional[str],
        start_s: float,
        end_s: float,
        severity: float,
        evidence: list[Evidence],
        event_ids: Optional[list[int]] = None,
    ) -> Finding:
        seq = self._seq.get(kind, 0)
        self._seq[kind] = seq + 1
        return Finding(
            finding_id=f"{self.state.match_id}:{kind.value}:{seq}",
            match_id=self.state.match_id,
            kind=kind,
            status=status,
            team_id=team_id,
            start_s=start_s,
            end_s=end_s,
            detected_at_s=self.state.clock_s,
            event_ids=event_ids or [],
            severity=round(max(0.0, min(1.0, severity)), 3),
            score=self.state.score,
            evidence=evidence,
        )

    def _evidence(
        self,
        metric: str,
        value: float,
        unit: Unit,
        team_id: Optional[str] = None,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
        baseline: Optional[float] = None,
    ) -> Evidence:
        end = self.state.clock_s if end_s is None else end_s
        start = max(end - self.state.window_s, 0.0) if start_s is None else start_s
        return Evidence(
            metric=metric,
            team_id=team_id,
            value=round(value, 3),
            unit=unit,
            window_start_s=start,
            window_end_s=end,
            baseline=None if baseline is None else round(baseline, 3),
        )

    def _team_momentum(self, team_id: str) -> float:
        """Momentum over the main window from `team_id`'s point of view."""
        value = momentum(self.state.team(self.state.home_id), self.state.team(self.state.away_id))
        return value if team_id == self.state.home_id else -value

    # ------------------------------------------------------------------ #
    # Moments: goals and big chances
    # ------------------------------------------------------------------ #

    def _on_shot(self, shot: ShotEvent) -> list[Finding]:
        team, opp = shot.team_id, self.state.opponent(shot.team_id)
        spell = self.state.spell
        move_start = spell.start_s if spell is not None and spell.team_id == team else shot.clock_s
        totals, opp_totals = self.state.totals(team), self.state.totals(opp)
        evidence = [
            self._evidence("shot_xg", shot.xg, "xg", team, shot.clock_s, shot.clock_s),
            self._evidence("shot_distance", shot.distance_to_goal_m, "m", team, shot.clock_s, shot.clock_s),
            self._evidence("match_xg", totals.xg, "xg", team, 0.0, baseline=opp_totals.xg),
            self._evidence("match_shots", totals.shots, "count", team, 0.0, baseline=opp_totals.shots),
            self._evidence("momentum", self._team_momentum(team), "index", team),
        ]
        if shot.outcome == "goal":
            score = self.state.score
            mine, theirs = (score.home, score.away) if team == self.state.home_id else (score.away, score.home)
            # Equalisers and go-ahead goals change the result; others extend or narrow a margin.
            severity = 1.0 if mine - theirs in (0, 1) else 0.85
            return [self._finding(FindingKind.GOAL, "moment", team, move_start, shot.clock_s, severity, evidence,
                                  [shot.event_id])]
        if shot.xg >= self.config.big_chance_xg:
            return [self._finding(FindingKind.BIG_CHANCE, "moment", team, move_start, shot.clock_s,
                                  0.4 + shot.xg, evidence, [shot.event_id])]
        return []
