"""Seeded possession simulator that emits schema events.

The simulator is deliberately simple: one team has the ball, and at every step
the player holding it is either pressured, tackled, takes a shot or passes.
Squad quality, tactical *phases* and scripted goals (see ``storylines``) bend
those probabilities so that matches contain real, explainable patterns, such
as a pressing surge or a chaotic spell, instead of uniform noise.

The same ``(seed, clubs, storyline)`` always produces the identical match, which
is what makes replays, tests and agent evaluations reproducible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .schema import (
    GOAL_WIDTH_M,
    HALF_LENGTH_S,
    PITCH_LENGTH_M,
    PITCH_WIDTH_M,
    Event,
    MatchEndEvent,
    MatchStartEvent,
    PassEvent,
    PeriodEndEvent,
    PeriodStartEvent,
    Point,
    PossessionChangeEvent,
    PossessionReason,
    PressureEvent,
    Score,
    ShotEvent,
    Side,
    TackleEvent,
)
from .storylines import STORYLINES, ScriptedGoal, Storyline, Tactics, tactics_at
from .teams import CLUBS, DEFAULT_AWAY, DEFAULT_HOME, FORMATION_4_3_3, build_team

CENTRE = (PITCH_LENGTH_M / 2, PITCH_WIDTH_M / 2)
PRESSURE_EVENT_MIN = 0.45
KICKOFF_PLAYER_INDEX = 6  # central midfielder

# How much each position's line moves with the ball (0 = stays home).
_SHAPE_FOLLOW = {"GK": 0.0, "LB": 0.5, "CB": 0.45, "RB": 0.5, "CM": 0.8, "LW": 1.0, "ST": 1.0, "RW": 1.0}

XY = tuple[float, float]


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _point(xy: XY) -> Point:
    return Point(x=round(_clip(xy[0], 0, PITCH_LENGTH_M), 1), y=round(_clip(xy[1], 0, PITCH_WIDTH_M), 1))


@dataclass(frozen=True)
class MatchResult:
    events: list[Event]
    answer_key: dict[str, Any]


class MatchSimulator:
    def __init__(self, seed: int, home_key: str, away_key: str, storyline: Storyline) -> None:
        self.seed = seed
        self.storyline = storyline
        self.rng = random.Random(seed)
        self.teams = {Side.HOME: build_team(home_key, Side.HOME), Side.AWAY: build_team(away_key, Side.AWAY)}
        self.match_id = f"{home_key}_{away_key}_{storyline.name}_{seed:04d}"

        self.events: list[Event] = []
        self.score = {Side.HOME: 0, Side.AWAY: 0}
        self.pending_goals: list[ScriptedGoal] = sorted(storyline.goals, key=lambda g: g.minute)
        self.scripted_goal_events: list[tuple[ScriptedGoal, ShotEvent]] = []

        self.period = 1
        self.clock = 0.0
        self.limit = float(HALF_LENGTH_S)
        self.poss = Side.HOME
        self.holder = KICKOFF_PLAYER_INDEX
        self.ball: XY = CENTRE
        self.spell_start = 0.0
        self.spell_passes = 0
        self.last_pressure_clock = -99.0

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def run(self) -> MatchResult:
        self._emit_match_start()
        for period in (1, 2):
            self.period = period
            self.clock = float((period - 1) * HALF_LENGTH_S)
            self.limit = float(period * HALF_LENGTH_S)
            self._emit(PeriodStartEvent)
            kicking = Side.HOME if period == 1 else Side.AWAY
            self.poss = kicking.opponent
            self.spell_start = self.clock
            self.spell_passes = 0
            self._change_possession(kicking, KICKOFF_PLAYER_INDEX, CENTRE, "kickoff")
            while self.clock < self.limit - 1.0:
                self._step()
            self.clock = self.limit
            self._emit(PeriodEndEvent, score=self._score())
        self._emit(MatchEndEvent, final_score=self._score())
        return MatchResult(events=self.events, answer_key=self._answer_key())

    # ------------------------------------------------------------------ #
    # Emission helpers
    # ------------------------------------------------------------------ #

    def _emit(self, cls: type, **fields: Any) -> Any:
        event = cls(
            event_id=len(self.events),
            match_id=self.match_id,
            period=self.period,
            clock_s=round(self.clock, 1),
            **fields,
        )
        self.events.append(event)
        return event

    def _emit_match_start(self) -> None:
        weather = self.rng.choice(["clear", "overcast", "light rain", "windy", "cold and dry"])
        kickoff = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc) + timedelta(days=self.seed % 28)
        home, away = self.teams[Side.HOME], self.teams[Side.AWAY]
        self._emit(
            MatchStartEvent,
            home=home,
            away=away,
            venue=CLUBS[home.team_id].venue,
            competition="Synthetic League",
            weather=weather,
            kickoff_utc=kickoff,
        )

    def _score(self) -> Score:
        return Score(home=self.score[Side.HOME], away=self.score[Side.AWAY])

    def _advance(self, seconds: float) -> None:
        self.clock = min(self.clock + seconds, self.limit)

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #

    @staticmethod
    def _progress(side: Side, x: float) -> float:
        """How far up the pitch (0 own goal, 1 opponent goal) an x position is for `side`."""
        return x / PITCH_LENGTH_M if side is Side.HOME else 1 - x / PITCH_LENGTH_M

    @staticmethod
    def _goal_x(side: Side) -> float:
        return PITCH_LENGTH_M if side is Side.HOME else 0.0

    def _dist_to_goal(self, side: Side, xy: XY) -> float:
        return math.hypot(self._goal_x(side) - xy[0], xy[1] - PITCH_WIDTH_M / 2)

    def _player_xy(self, side: Side, index: int, ball_x: float, tactics: Tactics) -> XY:
        """Where a player of `side` stands given where the ball is."""
        slot = FORMATION_4_3_3[index]
        follow = _SHAPE_FOLLOW[slot.position]
        shift = ((self._progress(side, ball_x) - 0.5) * 0.34 + (tactics.attack_intent - 0.5) * 0.10) * follow
        x_frac = _clip(slot.x + shift, 0.03, 0.97)
        y_frac = slot.y if side is Side.HOME else 1 - slot.y
        x = x_frac * PITCH_LENGTH_M if side is Side.HOME else PITCH_LENGTH_M - x_frac * PITCH_LENGTH_M
        return x, y_frac * PITCH_WIDTH_M

    def _nearest(self, side: Side, xy: XY, tactics: Tactics) -> int:
        candidates = range(1, len(FORMATION_4_3_3))  # never the keeper
        return min(candidates, key=lambda i: math.dist(self._player_xy(side, i, xy[0], tactics), xy))

    @staticmethod
    def _xg(side: Side, xy: XY) -> float:
        dx = abs((PITCH_LENGTH_M if side is Side.HOME else 0.0) - xy[0])
        dy = xy[1] - PITCH_WIDTH_M / 2
        half = GOAL_WIDTH_M / 2
        angle = math.atan2(GOAL_WIDTH_M * dx, dx * dx + dy * dy - half * half)
        logit = -1.9 - 0.085 * math.hypot(dx, dy) + 1.9 * angle
        return round(1 / (1 + math.exp(-logit)), 3)

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #

    def _change_possession(self, new_side: Side, index: int, loc: XY, reason: PossessionReason) -> None:
        previous = self.poss
        self._emit(
            PossessionChangeEvent,
            team_id=self.teams[new_side].team_id,
            from_team_id=self.teams[previous].team_id,
            player_id=self.teams[new_side].players[index].player_id,
            location=_point(loc),
            reason=reason,
            previous_spell_s=round(max(self.clock - self.spell_start, 0.0), 1),
            previous_spell_passes=self.spell_passes,
        )
        self.poss, self.holder, self.ball = new_side, index, loc
        self.spell_start, self.spell_passes = self.clock, 0

    def _due_goal(self, side: Side, minute: float) -> Optional[ScriptedGoal]:
        for goal in self.pending_goals:
            if goal.side is side and minute >= goal.minute:
                return goal
        return None

    # ------------------------------------------------------------------ #
    # One step of play
    # ------------------------------------------------------------------ #

    def _step(self) -> None:
        side = self.poss
        opp = side.opponent
        minute = self.clock / 60
        tac = tactics_at(self.storyline, side, minute)
        otac = tactics_at(self.storyline, opp, minute)

        due = self._due_goal(side, minute)
        if due is not None:
            overdue = minute - due.minute
            tac = replace(
                tac,
                attack_intent=min(tac.attack_intent + 0.25, 0.98),
                shot_propensity=tac.shot_propensity + 1.0 + 0.3 * overdue,
            )

        holder = self.teams[side].players[self.holder]
        progress = self._progress(side, self.ball[0])
        tempo = max(0.55, 1 + 0.5 * (tac.tempo + otac.tempo))

        # Defensive pressure on the ball carrier.
        base = otac.press * (0.55 + 0.9 * (1 - progress)) + (0.10 if progress > 0.8 else 0.0)
        pressure = _clip(base + self.rng.gauss(0, 0.07), 0.0, 1.0)
        presser = self._pick_presser(opp, otac)

        if pressure >= PRESSURE_EVENT_MIN and self.clock - self.last_pressure_clock >= 3.0:
            self._emit(
                PressureEvent,
                team_id=self.teams[opp].team_id,
                player_id=self.teams[opp].players[presser].player_id,
                target_player_id=holder.player_id,
                location=_point(self.ball),
                intensity=round(pressure, 2),
            )
            self.last_pressure_clock = self.clock

        # Possible tackle.
        if self.rng.random() < _clip(0.04 + 0.5 * max(pressure - 0.35, 0.0), 0.0, 0.4):
            self._resolve_tackle(side, presser, pressure)
            return

        # Possible shot.
        dist_goal = self._dist_to_goal(side, self.ball)
        if dist_goal < 32 and progress > 0.6:
            zone = 1 - dist_goal / 32
            p_shot = (
                (0.03 + 0.26 * zone**1.3)
                * tac.shot_propensity
                * (0.6 + 0.8 * holder.shooting)
                * (1 - 0.3 * pressure)
            )
            if due is not None and dist_goal < 30:
                p_shot = max(p_shot, 0.35)
            if self.rng.random() < p_shot:
                self._take_shot(side, pressure, due)
                return

        self._pass(side, tac, otac, pressure, tempo)

    def _pick_presser(self, opp: Side, otac: Tactics) -> int:
        candidates = list(range(1, len(FORMATION_4_3_3)))
        weights = [
            math.exp(-math.dist(self._player_xy(opp, i, self.ball[0], otac), self.ball) / 9) for i in candidates
        ]
        return self.rng.choices(candidates, weights)[0]

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _resolve_tackle(self, side: Side, presser: int, pressure: float) -> None:
        opp = side.opponent
        tackler = self.teams[opp].players[presser]
        holder = self.teams[side].players[self.holder]
        self._advance(self.rng.uniform(0.5, 1.2))
        win_p = _clip(
            0.42 + 0.35 * (tackler.tackling - 0.65) + 0.35 * (pressure - 0.45) - 0.25 * (holder.pace - 0.7),
            0.15,
            0.80,
        )
        won = self.rng.random() < win_p
        self._emit(
            TackleEvent,
            team_id=self.teams[opp].team_id,
            player_id=tackler.player_id,
            tackled_player_id=holder.player_id,
            location=_point(self.ball),
            outcome="won" if won else "lost",
        )
        if won:
            self._change_possession(opp, presser, self.ball, "tackle")
            self._advance(self.rng.uniform(1.5, 3.5))
        else:
            self._advance(self.rng.uniform(1.0, 2.5))

    def _take_shot(self, side: Side, pressure: float, due: Optional[ScriptedGoal]) -> None:
        opp = side.opponent
        shooter = self.teams[side].players[self.holder]
        loc = self.ball
        dist = self._dist_to_goal(side, loc)
        xg = self._xg(side, loc)

        if due is not None:
            outcome = "goal"
        else:
            p_goal = _clip(xg * (0.75 + 0.5 * shooter.shooting) * (1 - 0.25 * pressure), 0.0, 0.9)
            if self.storyline.scripted_scoring:
                p_goal = 0.0  # the script decides every goal; a would-be goal becomes a save
            roll = self.rng.random()
            if roll < p_goal:
                outcome = "goal"
            else:
                outcome = self.rng.choices(
                    ["saved", "blocked", "off_target"],
                    [0.40 + 0.6 * xg, 0.25 + 0.2 * pressure, 0.35 - 0.2 * xg],
                )[0]

        self._advance(self.rng.uniform(0.6, 1.4))
        score_after = None
        if outcome == "goal":
            self.score[side] += 1
            score_after = self._score()
        speed = _clip(78 + 30 * shooter.shooting - 0.5 * dist + self.rng.gauss(0, 6), 55, 130)
        shot = self._emit(
            ShotEvent,
            team_id=self.teams[side].team_id,
            player_id=shooter.player_id,
            location=_point(loc),
            distance_to_goal_m=round(dist, 1),
            xg=xg,
            shot_speed_kmh=round(speed, 1),
            outcome=outcome,
            score_after=score_after,
        )
        if due is not None:
            self.pending_goals.remove(due)
            self.scripted_goal_events.append((due, shot))

        opp_keeper_xy = (4.0 if opp is Side.HOME else PITCH_LENGTH_M - 4.0, PITCH_WIDTH_M / 2)
        if outcome == "goal":
            self._advance(self.rng.uniform(25, 40))
            self._change_possession(opp, KICKOFF_PLAYER_INDEX, CENTRE, "kickoff")
        elif outcome == "saved":
            self._advance(self.rng.uniform(4, 8))
            self._change_possession(opp, 0, opp_keeper_xy, "shot_saved")
        elif outcome == "blocked":
            self._advance(self.rng.uniform(1.5, 3.0))
            self._change_possession(opp, self.rng.choice([2, 3]), loc, "shot_blocked")
        else:
            self._advance(self.rng.uniform(8, 14))
            self._change_possession(opp, 0, opp_keeper_xy, "goal_kick")

    def _pass(self, side: Side, tac: Tactics, otac: Tactics, pressure: float, tempo: float) -> None:
        opp = side.opponent
        team = self.teams[side]
        holder = team.players[self.holder]
        bx, by = self.ball
        progress = self._progress(side, bx)

        candidates = [i for i in range(len(FORMATION_4_3_3)) if i != self.holder]
        weights, positions = [], {}
        for i in candidates:
            pos = self._player_xy(side, i, bx, tac)
            positions[i] = pos
            forward = self._progress(side, pos[0]) - progress
            dist = math.dist(pos, (bx, by))
            weight = math.exp((1.2 + 2.5 * tac.attack_intent) * forward) * math.exp(-dist / 28)
            if dist < 4:
                weight *= 0.2
            if i == 0:
                weight *= 0.6 if progress < 0.35 else 0.03
            weights.append(weight)
        target = self.rng.choices(candidates, weights)[0]
        tx, ty = positions[target]

        dist = math.dist((tx, ty), (bx, by))
        forward = max(self._progress(side, tx) - progress, 0.0)
        difficulty = _clip(
            0.20 * min(dist / 45, 1)
            + 0.28 * min(forward / 0.35, 1)
            + 0.30 * pressure
            + 0.12 * self.rng.random()
            + 0.05 * min(abs(ty - by) / 34, 1),
            0.0,
            1.0,
        )
        p_complete = _clip(0.985 - 0.40 * difficulty + (holder.passing - 0.70) * 0.35 + tac.pass_accuracy, 0.15, 0.985)
        complete = self.rng.random() < p_complete

        spread = 2.0 if complete else 4.5
        end: XY = (
            _clip(tx + self.rng.gauss(0, spread), 0, PITCH_LENGTH_M),
            _clip(ty + self.rng.gauss(0, spread), 0, PITCH_WIDTH_M),
        )
        speed = _clip(26 + 54 * min(dist / 45, 1) + self.rng.gauss(0, 5), 18, 115)
        self._emit(
            PassEvent,
            team_id=team.team_id,
            player_id=holder.player_id,
            receiver_id=team.players[target].player_id,
            start=_point(self.ball),
            end=_point(end),
            distance_m=round(math.dist(end, (bx, by)), 1),
            ball_speed_kmh=round(speed, 1),
            outcome="complete" if complete else "incomplete",
            pressure=round(pressure, 2),
            difficulty=round(difficulty, 2),
        )

        if complete:
            self.holder, self.ball = target, end
            self.spell_passes += 1
            self._advance(self.rng.triangular(3, 11, 6) / tempo)
            return

        self._advance(self.rng.uniform(0.8, 1.8))
        if self.rng.random() < 0.35:  # the ball runs out of play
            out = (end[0], 0.0 if end[1] < PITCH_WIDTH_M / 2 else PITCH_WIDTH_M)
            self._change_possession(opp, self._nearest(opp, out, otac), out, "out_of_play")
            self._advance(self.rng.uniform(6, 12))
        else:
            self._change_possession(opp, self._nearest(opp, end, otac), end, "interception")
            self._advance(self.rng.uniform(2, 4))

    # ------------------------------------------------------------------ #
    # Ground truth for evaluating downstream agents
    # ------------------------------------------------------------------ #

    def _answer_key(self) -> dict[str, Any]:
        def team_ids(team: str) -> list[str]:
            if team == "both":
                return [self.teams[Side.HOME].team_id, self.teams[Side.AWAY].team_id]
            return [self.teams[Side(team)].team_id]

        goals = [
            {
                "event_id": e.event_id,
                "clock_s": e.clock_s,
                "team_id": e.team_id,
                "player_id": e.player_id,
                "score_after": e.score_after.model_dump() if e.score_after else None,
            }
            for e in self.events
            if isinstance(e, ShotEvent) and e.outcome == "goal"
        ]
        return {
            "match_id": self.match_id,
            "seed": self.seed,
            "storyline": self.storyline.name,
            "description": self.storyline.description,
            "phases": [
                {
                    "label": p.label,
                    "kind": p.kind,
                    "description": p.description,
                    "team_ids": team_ids(p.team),
                    "start_s": p.start_min * 60,
                    "end_s": p.end_min * 60,
                }
                for p in self.storyline.phases
            ],
            "scripted_goals": [
                {
                    "description": g.description,
                    "team_id": self.teams[g.side].team_id,
                    "scripted_minute": g.minute,
                    "event_id": shot.event_id,
                    "clock_s": shot.clock_s,
                }
                for g, shot in self.scripted_goal_events
            ],
            "goals": goals,
            "final_score": self._score().model_dump(),
        }


def generate_match(
    seed: int = 7,
    home: str = DEFAULT_HOME,
    away: str = DEFAULT_AWAY,
    storyline: str = "comeback",
) -> MatchResult:
    """Generate one complete synthetic match."""
    if storyline not in STORYLINES:
        raise ValueError(f"Unknown storyline {storyline!r}; choose from {sorted(STORYLINES)}")
    if home == away:
        raise ValueError("home and away must be different clubs")
    return MatchSimulator(seed, home, away, STORYLINES[storyline]).run()
