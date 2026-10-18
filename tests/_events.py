"""Builder for small hand-written matches used by the stats-engine tests.

Home ("aur") attacks towards x = 105, away ("irv") towards x = 0, as in the generator.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pitchside.schema import (
    MatchEndEvent,
    MatchStartEvent,
    PassEvent,
    PeriodEndEvent,
    PeriodStartEvent,
    Point,
    PossessionChangeEvent,
    PressureEvent,
    Score,
    ShotEvent,
    Side,
    TackleEvent,
)
from pitchside.teams import build_team

HOME, AWAY = "aur", "irv"


class MatchBuilder:
    def __init__(self, match_id: str = "test_match") -> None:
        self.match_id = match_id
        self.events: list = []
        self.score = {HOME: 0, AWAY: 0}
        self._possessor: str | None = None

    def _add(self, cls, clock_s: float, **fields):
        event = cls(
            event_id=len(self.events),
            match_id=self.match_id,
            period=1 if clock_s < 2700 or cls is PeriodEndEvent and clock_s == 2700 else 2,
            clock_s=clock_s,
            **fields,
        )
        self.events.append(event)
        return event

    @staticmethod
    def _other(team: str) -> str:
        return AWAY if team == HOME else HOME

    def start(self) -> "MatchBuilder":
        self._add(
            MatchStartEvent, 0.0,
            home=build_team(HOME, Side.HOME), away=build_team(AWAY, Side.AWAY),
            venue="Test Ground", competition="Test", weather="clear",
            kickoff_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self._add(PeriodStartEvent, 0.0)
        return self.change(0.0, HOME, 52.5, reason="kickoff")

    def change(self, t: float, team: str, x: float, y: float = 34, reason: str = "interception",
               spell_s: float = 10.0, passes: int = 0) -> "MatchBuilder":
        self._add(
            PossessionChangeEvent, t, team_id=team, from_team_id=self._other(team),
            player_id=f"{team}_06", location=Point(x=x, y=y), reason=reason,
            previous_spell_s=spell_s, previous_spell_passes=passes,
        )
        self._possessor = team
        return self

    def pass_(self, t: float, team: str, x: float, end_x: float | None = None, y: float = 34,
              outcome: str = "complete", difficulty: float = 0.3, pressure: float = 0.2) -> "MatchBuilder":
        end_x = x if end_x is None else end_x
        self._add(
            PassEvent, t, team_id=team, player_id=f"{team}_08", receiver_id=f"{team}_09",
            start=Point(x=x, y=y), end=Point(x=end_x, y=y), distance_m=abs(end_x - x),
            ball_speed_kmh=50.0, outcome=outcome, pressure=pressure, difficulty=difficulty,
        )
        return self

    def shot(self, t: float, team: str, x: float, y: float = 34, xg: float = 0.1,
             outcome: str = "saved") -> "MatchBuilder":
        score_after = None
        if outcome == "goal":
            self.score[team] += 1
            score_after = Score(home=self.score[HOME], away=self.score[AWAY])
        self._add(
            ShotEvent, t, team_id=team, player_id=f"{team}_09", location=Point(x=x, y=y),
            distance_to_goal_m=abs((105 if team == HOME else 0) - x), xg=xg, shot_speed_kmh=90.0,
            outcome=outcome, score_after=score_after,
        )
        return self

    def press(self, t: float, team: str, x: float = 52.5, intensity: float = 0.6) -> "MatchBuilder":
        self._add(
            PressureEvent, t, team_id=team, player_id=f"{team}_06",
            target_player_id=f"{self._other(team)}_06", location=Point(x=x, y=34), intensity=intensity,
        )
        return self

    def tackle(self, t: float, team: str, x: float = 52.5, outcome: str = "won") -> "MatchBuilder":
        self._add(
            TackleEvent, t, team_id=team, player_id=f"{team}_04", tackled_player_id=f"{self._other(team)}_08",
            location=Point(x=x, y=34), outcome=outcome,
        )
        return self

    def end(self, t: float = 5400.0) -> "MatchBuilder":
        score = Score(home=self.score[HOME], away=self.score[AWAY])
        self._add(MatchEndEvent, t, final_score=score)
        return self
