"""Per-team and match-wide numbers, updated one event at a time.

``MatchState`` is the single place that turns raw events into numbers. It
keeps rolling windows (default: the last five minutes) and cumulative totals
for each team, plus match-wide measures of how settled play is. Detectors and,
later, the MCP tools read from it; nothing here makes decisions.

Field tilt is the share of final-third passes: of all passes played in an
attacking third, how many were this team's. Possession is time-based.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, ConfigDict

from ..schema import (
    PITCH_LENGTH_M,
    Event,
    MatchEndEvent,
    MatchStartEvent,
    PassEvent,
    Point,
    PossessionChangeEvent,
    PressureEvent,
    Score,
    ShotEvent,
    Side,
    TackleEvent,
)
from .windows import PossessionClock, RollingWindow

WINDOW_S = 300.0  # main rolling window
SHORT_WINDOW_S = 180.0  # quicker window for pressing
FINAL_THIRD = 2 / 3  # progress beyond which a pass is in the attacking third
MIN_RATE_SPAN_S = 60.0  # never divide a count by less than a minute


def progress(side: Side, x: float) -> float:
    """0 at a team's own goal line, 1 at the opponent's."""
    return x / PITCH_LENGTH_M if side is Side.HOME else 1 - x / PITCH_LENGTH_M


def forward_m(side: Side, start: Point, end: Point) -> float:
    """Metres gained towards the opponent's goal moving from `start` to `end`."""
    return (end.x - start.x) * side.attack_sign


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TeamNumbers(_Model):
    """One team's numbers over a window (or since kick-off)."""

    team_id: str
    window_start_s: float
    window_end_s: float
    possession_pct: float
    passes: int
    pass_completion_pct: Optional[float]
    pass_difficulty: Optional[float]
    final_third_passes: int
    field_tilt_pct: float
    shots: int
    xg: float
    goals: int
    pressures: int
    pressures_per_min: float
    tackles_won: int


class MatchNumbers(_Model):
    """Match-wide numbers over a window: how settled or broken the play is."""

    window_start_s: float
    window_end_s: float
    passes: int
    pass_completion_pct: Optional[float]
    turnovers: int
    turnovers_per_min: float
    mean_spell_s: Optional[float]


@dataclass
class Spell:
    """The current possession spell."""

    team_id: str
    start_s: float
    reason: str
    location: Point
    completed_passes: int = 0


class _TeamWindows:
    def __init__(self, span_s: float, short_span_s: float) -> None:
        self.passes = RollingWindow(span_s)
        self.completed = RollingWindow(span_s)
        self.difficulty = RollingWindow(span_s)
        self.final_third = RollingWindow(span_s)
        self.shots = RollingWindow(span_s)
        self.xg = RollingWindow(span_s)
        self.goals = RollingWindow(span_s)
        self.pressures = RollingWindow(span_s)
        self.pressures_short = RollingWindow(short_span_s)
        self.tackles_won = RollingWindow(span_s)

    def all(self) -> tuple[RollingWindow, ...]:
        return tuple(vars(self).values())


@dataclass
class _TeamTotals:
    passes: int = 0
    completed: int = 0
    difficulty: float = 0.0
    final_third: int = 0
    shots: int = 0
    xg: float = 0.0
    goals: int = 0
    pressures: int = 0
    tackles_won: int = 0


class MatchState:
    """Running numbers for one match. Feed it every event, in order, with `update`."""

    def __init__(self, window_s: float = WINDOW_S, short_window_s: float = SHORT_WINDOW_S) -> None:
        self.window_s = window_s
        self.short_window_s = short_window_s
        self.match_id: Optional[str] = None
        self.clock_s = 0.0
        self.sides: dict[str, Side] = {}
        self.team_ids: dict[Side, str] = {}
        self.score = Score(home=0, away=0)
        self.finished = False
        self.spell: Optional[Spell] = None

        self._win: dict[str, _TeamWindows] = {}
        self._tot: dict[str, _TeamTotals] = {}
        self._possession = PossessionClock(keep_s=max(window_s, short_window_s) * 2)
        self._turnovers = RollingWindow(window_s)
        self._spell_lengths = RollingWindow(window_s)
        self._match_passes = RollingWindow(window_s)
        self._match_completed = RollingWindow(window_s)

    # ------------------------------------------------------------------ #
    # Updating
    # ------------------------------------------------------------------ #

    def update(self, event: Event) -> None:
        if isinstance(event, MatchStartEvent):
            self._start(event)
            return
        if self.match_id is None:
            raise ValueError("the first event of a match must be match_start")
        if event.match_id != self.match_id:
            raise ValueError(f"event from match {event.match_id!r} fed to state for {self.match_id!r}")
        self.clock_s = event.clock_s
        for windows in self._win.values():
            for window in windows.all():
                window.advance(event.clock_s)
        for window in (self._turnovers, self._spell_lengths, self._match_passes, self._match_completed):
            window.advance(event.clock_s)

        if isinstance(event, PassEvent):
            self._on_pass(event)
        elif isinstance(event, ShotEvent):
            self._on_shot(event)
        elif isinstance(event, PossessionChangeEvent):
            self._on_possession_change(event)
        elif isinstance(event, PressureEvent):
            self._win[event.team_id].pressures.add(event.clock_s)
            self._win[event.team_id].pressures_short.add(event.clock_s)
            self._tot[event.team_id].pressures += 1
        elif isinstance(event, TackleEvent) and event.outcome == "won":
            self._win[event.team_id].tackles_won.add(event.clock_s)
            self._tot[event.team_id].tackles_won += 1
        elif isinstance(event, MatchEndEvent):
            self.finished = True

    def _start(self, event: MatchStartEvent) -> None:
        if self.match_id is not None:
            raise ValueError("match_start seen twice")
        self.match_id = event.match_id
        self.clock_s = event.clock_s
        for team in (event.home, event.away):
            self.sides[team.team_id] = team.side
            self.team_ids[team.side] = team.team_id
            self._win[team.team_id] = _TeamWindows(self.window_s, self.short_window_s)
            self._tot[team.team_id] = _TeamTotals()

    def _on_pass(self, e: PassEvent) -> None:
        win, tot = self._win[e.team_id], self._tot[e.team_id]
        complete = e.outcome == "complete"
        final_third = progress(self.sides[e.team_id], e.start.x) > FINAL_THIRD
        win.passes.add(e.clock_s)
        win.difficulty.add(e.clock_s, e.difficulty)
        self._match_passes.add(e.clock_s)
        tot.passes += 1
        tot.difficulty += e.difficulty
        if complete:
            win.completed.add(e.clock_s)
            self._match_completed.add(e.clock_s)
            tot.completed += 1
            if self.spell is not None and self.spell.team_id == e.team_id:
                self.spell.completed_passes += 1
        if final_third:
            win.final_third.add(e.clock_s)
            tot.final_third += 1

    def _on_shot(self, e: ShotEvent) -> None:
        win, tot = self._win[e.team_id], self._tot[e.team_id]
        win.shots.add(e.clock_s)
        win.xg.add(e.clock_s, e.xg)
        tot.shots += 1
        tot.xg += e.xg
        if e.outcome == "goal":
            win.goals.add(e.clock_s)
            tot.goals += 1
            if e.score_after is not None:
                self.score = e.score_after

    def _on_possession_change(self, e: PossessionChangeEvent) -> None:
        self._possession.switch(e.team_id, e.clock_s)
        if e.reason != "kickoff":
            self._turnovers.add(e.clock_s)
            self._spell_lengths.add(e.clock_s, e.previous_spell_s)
        self.spell = Spell(team_id=e.team_id, start_s=e.clock_s, reason=e.reason, location=e.location)

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    @property
    def home_id(self) -> str:
        return self.team_ids[Side.HOME]

    @property
    def away_id(self) -> str:
        return self.team_ids[Side.AWAY]

    def opponent(self, team_id: str) -> str:
        return self.team_ids[self.sides[team_id].opponent]

    def _rate_span_s(self, window: RollingWindow) -> float:
        return max(window.covered_s(), MIN_RATE_SPAN_S)

    def pressure_rate(self, team_id: str, short: bool = False) -> float:
        """Pressure events per minute by `team_id` over the main (or short) window."""
        w = self._win[team_id].pressures_short if short else self._win[team_id].pressures
        return w.count / (self._rate_span_s(w) / 60)

    def pressure_rate_total(self, team_id: str) -> float:
        """Pressure events per minute since kick-off."""
        return self._tot[team_id].pressures / (max(self.clock_s, MIN_RATE_SPAN_S) / 60)

    def team(self, team_id: str) -> TeamNumbers:
        """Rolling-window numbers for one team."""
        win = self._win[team_id]
        other = self._win[self.opponent(team_id)]
        end = self.clock_s
        start = max(end - self.window_s, 0.0)
        covered = end - start
        ft, ft_other = win.final_third.count, other.final_third.count
        return TeamNumbers(
            team_id=team_id,
            window_start_s=start,
            window_end_s=end,
            possession_pct=_share(self._possession.seconds(team_id, start, end), covered),
            passes=win.passes.count,
            pass_completion_pct=_ratio(win.completed.count, win.passes.count),
            pass_difficulty=_round(win.difficulty.mean),
            final_third_passes=ft,
            field_tilt_pct=_share(ft, ft + ft_other),
            shots=win.shots.count,
            xg=round(win.xg.sum, 3),
            goals=win.goals.count,
            pressures=win.pressures.count,
            pressures_per_min=round(self.pressure_rate(team_id), 3),
            tackles_won=win.tackles_won.count,
        )

    def totals(self, team_id: str) -> TeamNumbers:
        """Cumulative numbers for one team since kick-off."""
        tot, other = self._tot[team_id], self._tot[self.opponent(team_id)]
        end = self.clock_s
        return TeamNumbers(
            team_id=team_id,
            window_start_s=0.0,
            window_end_s=end,
            possession_pct=_share(self._possession.total(team_id, end), end),
            passes=tot.passes,
            pass_completion_pct=_ratio(tot.completed, tot.passes),
            pass_difficulty=_round(tot.difficulty / tot.passes) if tot.passes else None,
            final_third_passes=tot.final_third,
            field_tilt_pct=_share(tot.final_third, tot.final_third + other.final_third),
            shots=tot.shots,
            xg=round(tot.xg, 3),
            goals=tot.goals,
            pressures=tot.pressures,
            pressures_per_min=round(self.pressure_rate_total(team_id), 3),
            tackles_won=tot.tackles_won,
        )

    def match(self) -> MatchNumbers:
        """Rolling-window match-wide numbers."""
        end = self.clock_s
        return MatchNumbers(
            window_start_s=max(end - self.window_s, 0.0),
            window_end_s=end,
            passes=self._match_passes.count,
            pass_completion_pct=_ratio(self._match_completed.count, self._match_passes.count),
            turnovers=self._turnovers.count,
            turnovers_per_min=round(self._turnovers.count / (self._rate_span_s(self._turnovers) / 60), 3),
            mean_spell_s=_round(self._spell_lengths.mean),
        )


def _ratio(part: float, whole: float) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


def _share(part: float, whole: float) -> float:
    """Percentage share; an empty window counts as an even split."""
    return round(100 * part / whole, 1) if whole else 50.0


def _round(value: Optional[float]) -> Optional[float]:
    return round(value, 3) if value is not None else None
