"""Key-moment detector: turns MatchState numbers into typed findings.

Every rule is plain arithmetic on the rolling numbers, with its thresholds in
``DetectorConfig``. Each finding carries the numbers that triggered it as
evidence, with their time windows, so a later agent can explain the moment
and a verifier can check the explanation.

Two kinds of finding:

* **Moments** (goal, big chance, counter-attack) fire once, on the event.
* **Spells** (pressure surge, siege, momentum shift, chaos spell) use
  hysteresis: a ``start`` finding once the "on" condition has held for a
  while, an ``end`` finding once the "off" condition has held for a while,
  then a cooldown. One spell of play therefore yields one start, not a
  flurry of repeats.

Momentum evidence is always given from the finding team's point of view
(positive = that team on top).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..schema import PITCH_LENGTH_M, PITCH_WIDTH_M, Event, MatchEndEvent, PassEvent, ShotEvent
from .findings import COUNTER_MAX_SEVERITY, Evidence, Finding, FindingKind, FindingStatus, Unit
from .indices import (
    TYPICAL_COMPLETION_PCT,
    TYPICAL_SPELL_S,
    TYPICAL_TURNOVERS_PER_MIN,
    chaos,
    chaos_parts,
    momentum,
)
from .metrics import MatchNumbers, MatchState, TeamNumbers, forward_m, goal_x, in_box, progress


@dataclass(frozen=True)
class DetectorConfig:
    """Thresholds, tuned on seeds 1-6 of each storyline only (seeds 101-120 are held out)."""

    big_chance_xg: float = 0.25

    # Spells in general
    warmup_s: float = 300.0  # no spell findings until the main window is full
    hold_duty: float = 0.9  # a spell starts when "on" now and on for this share of its hold time
    release_s: float = 120.0  # how long the "off" condition must hold to end a spell
    cooldown_s: float = 180.0  # quiet time after a spell ends before the same spell can restart

    # Pressure surge: pressure events per minute by one team over the short window
    surge_on_per_min: float = 3.4
    surge_off_per_min: float = 2.0
    surge_hold_s: float = 60.0

    # Siege: one team presses high in the opponent's half and owns the attacking third while
    # the other sits in a low block and has all but stopped pressing (main window)
    siege_min_press_per_min: float = 1.6
    siege_max_opp_press_per_min: float = 0.2
    siege_tilt_on_pct: float = 55.0
    siege_off_press_per_min: float = 1.0  # ends when the attackers stop pressing...
    siege_off_opp_press_per_min: float = 1.0  # ...or the defenders start pressing again
    siege_hold_s: float = 120.0

    # Momentum shift: momentum index (team's view) stays high
    momentum_on: float = 0.55
    momentum_off: float = 0.15
    momentum_hold_s: float = 120.0

    # Chaos spell: match-wide chaos index, and passing must actually be breaking down
    # (pressing alone also raises turnovers and shortens spells)
    chaos_on: float = 0.8
    chaos_min_completion_part: float = 0.5
    chaos_off: float = 0.5
    chaos_hold_s: float = 90.0

    # Counter-attack: ball won in open play, then a quick, direct finish (a shot, or a completed
    # pass into the box). Tuned on seeds 1-40 only; see docs/DECISIONS.md.
    counter_regains: tuple[str, ...] = ("tackle", "interception", "shot_blocked")
    counter_max_s: float = 12.0
    counter_max_passes: int = 4
    counter_min_forward_m: float = 30.0
    counter_box_entry: bool = True
    counter_min_severity: float = 0.0  # severity is reported, 0.1 to 0.3; never triggers a model call alone


@dataclass
class _SpellTracker:
    """Hysteresis state for one spell rule (one kind, one team or both)."""

    history: deque[tuple[float, bool]] = field(default_factory=deque)  # (clock, on) per evaluation
    off_since: Optional[float] = None
    start: Optional[Finding] = None
    cooldown_until: float = 0.0

    @property
    def active(self) -> bool:
        return self.start is not None

    def record(self, clock: float, on: bool, hold_s: float) -> None:
        self.history.append((clock, on))
        # keep one sample at or before the cutoff so the window's start state is known
        while len(self.history) > 1 and self.history[1][0] <= clock - hold_s:
            self.history.popleft()

    def on_fraction(self, clock: float, hold_s: float) -> float:
        """Share of the last `hold_s` seconds the condition was on (0 if not observed that long)."""
        if not self.history or clock - self.history[0][0] < hold_s:
            return 0.0
        cutoff = clock - hold_s
        on_time = 0.0
        samples = list(self.history)
        for (t0, on0), (t1, _) in zip(samples, samples[1:] + [(clock, False)]):
            if on0:
                on_time += max(t1 - max(t0, cutoff), 0.0)
        return on_time / hold_s


@dataclass
class _View:
    """Numbers for one evaluation step, computed once and shared by all rules."""

    teams: dict[str, TeamNumbers]
    match: MatchNumbers
    momentum: float  # home's view


class KeyMomentDetector:
    def __init__(self, state: MatchState, config: Optional[DetectorConfig] = None) -> None:
        self.state = state
        self.config = config or DetectorConfig()
        self._seq: dict[FindingKind, int] = {}
        self._spells: dict[tuple[FindingKind, Optional[str]], _SpellTracker] = {}

    def on_event(self, event: Event) -> list[Finding]:
        """Call after `state.update(event)`. Returns the findings this event produced."""
        if self.state.match_id is None or not self.state.team_ids:
            return []
        found: list[Finding] = []
        if isinstance(event, ShotEvent):
            found.extend(self._on_shot(event))
        if isinstance(event, (ShotEvent, PassEvent)):
            found.extend(self._counter_attack(event))
        if isinstance(event, MatchEndEvent):
            found.extend(self._close_all_spells())
        elif self.state.clock_s >= self.config.warmup_s:
            found.extend(self._check_spells())
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

    def _team_momentum(self, team_id: str, home_view: Optional[float] = None) -> float:
        """Momentum over the main window from `team_id`'s point of view."""
        if home_view is None:
            home_view = momentum(self.state.team(self.state.home_id), self.state.team(self.state.away_id))
        return home_view if team_id == self.state.home_id else -home_view

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
        if spell is not None and spell.team_id == team:
            # How the possession began, so a counter-goal can be described from verified numbers.
            evidence += [
                self._evidence("seconds_since_turnover", shot.clock_s - spell.start_s, "s", team, spell.start_s,
                               shot.clock_s),
                self._evidence("passes_in_spell", spell.completed_passes, "count", team, spell.start_s,
                               shot.clock_s),
                self._evidence("metres_gained", forward_m(self.state.sides[team], spell.location, shot.location),
                               "m", team, spell.start_s, shot.clock_s),
                self._evidence("possession_won_in_open_play",
                               1.0 if spell.reason in self.config.counter_regains else 0.0, "count", team,
                               spell.start_s, spell.start_s),
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

    def _counter_attack(self, event: Event) -> list[Finding]:
        """A quick, direct break: ball won in open play, then a shot or a pass into the box.

        Fires at most once per possession spell, on whichever finish comes first.
        """
        cfg, spell = self.config, self.state.spell
        if spell is None or spell.counter_emitted or spell.team_id != event.team_id:
            return []
        if spell.reason not in cfg.counter_regains:
            return []
        if isinstance(event, ShotEvent):
            shot, finish_xy, finish_dist = event, event.location, event.distance_to_goal_m
        elif isinstance(event, PassEvent) and event.outcome == "complete" and cfg.counter_box_entry:
            side = self.state.sides[event.team_id]
            if not in_box(side, event.end):
                return []
            shot, finish_xy = None, event.end
            finish_dist = math.hypot(goal_x(side) - event.end.x, event.end.y - PITCH_WIDTH_M / 2)
        else:
            return []

        team, start = event.team_id, spell.start_s
        seconds = event.clock_s - start
        side = self.state.sides[team]
        gained = forward_m(side, spell.location, finish_xy)
        if (seconds > cfg.counter_max_s or spell.completed_passes > cfg.counter_max_passes
                or gained < cfg.counter_min_forward_m):
            return []
        xg = shot.xg if shot is not None else 0.0
        # Low by design: base 0.1, +0.05 if it ended in a shot, up to +0.1 for xG, +0.05 if it scored.
        severity = min(
            0.1 + (0.05 if shot is not None else 0.0) + 0.1 * min(xg / 0.5, 1.0)
            + (0.05 if shot and shot.outcome == "goal" else 0.0),
            COUNTER_MAX_SEVERITY,
        )
        if severity < cfg.counter_min_severity:
            return []
        spell.counter_emitted = True
        evidence = [
            self._evidence("seconds_to_finish", seconds, "s", team, start, event.clock_s),
            self._evidence("passes_in_move", spell.completed_passes, "count", team, start, event.clock_s),
            self._evidence("metres_gained", gained, "m", team, start, event.clock_s),
            self._evidence("regain_distance_from_own_goal", progress(side, spell.location.x) * PITCH_LENGTH_M, "m",
                           team, start, start),
            self._evidence("finish_distance_to_goal", finish_dist, "m", team, event.clock_s, event.clock_s),
            self._evidence("ended_with_shot", 1.0 if shot is not None else 0.0, "count", team, event.clock_s,
                           event.clock_s),
        ]
        if shot is not None:
            evidence.append(self._evidence("shot_xg", shot.xg, "xg", team, event.clock_s, event.clock_s))
        return [self._finding(FindingKind.COUNTER_ATTACK, "moment", team, start, event.clock_s, severity, evidence,
                              [spell.event_id, event.event_id])]

    # ------------------------------------------------------------------ #
    # Spells
    # ------------------------------------------------------------------ #

    def _check_spells(self) -> list[Finding]:
        state, cfg = self.state, self.config
        home, away = state.team(state.home_id), state.team(state.away_id)
        view = _View(teams={home.team_id: home, away.team_id: away}, match=state.match(),
                     momentum=momentum(home, away))
        found: list[Finding] = []
        for team_id in (state.home_id, state.away_id):
            opp_id = state.opponent(team_id)
            mine, theirs = view.teams[team_id], view.teams[opp_id]
            team_mom = self._team_momentum(team_id, view.momentum)

            press = state.pressure_rate(team_id, short=True)
            found += self._step_spell(
                FindingKind.PRESSURE_SURGE, team_id, cfg.surge_hold_s,
                on=press >= cfg.surge_on_per_min,
                off=press < cfg.surge_off_per_min,
                strength=lambda: (press - cfg.surge_off_per_min) / (2 * cfg.surge_on_per_min - 2 * cfg.surge_off_per_min),
                evidence=lambda: self._surge_evidence(team_id, view),
            )

            found += self._step_spell(
                FindingKind.SIEGE, team_id, cfg.siege_hold_s,
                on=(mine.pressures_per_min >= cfg.siege_min_press_per_min
                    and theirs.pressures_per_min <= cfg.siege_max_opp_press_per_min
                    and mine.field_tilt_pct >= cfg.siege_tilt_on_pct),
                off=(mine.pressures_per_min < cfg.siege_off_press_per_min
                     or theirs.pressures_per_min > cfg.siege_off_opp_press_per_min),
                strength=lambda: (mine.field_tilt_pct - 50) / 50,
                evidence=lambda: self._siege_evidence(team_id, view),
            )

            found += self._step_spell(
                FindingKind.MOMENTUM_SHIFT, team_id, cfg.momentum_hold_s,
                on=team_mom >= cfg.momentum_on,
                off=team_mom < cfg.momentum_off,
                strength=lambda: team_mom,
                evidence=lambda: self._momentum_evidence(team_id, view),
            )

        chaos_now = chaos(view.match)
        found += self._step_spell(
            FindingKind.CHAOS_SPELL, None, cfg.chaos_hold_s,
            on=(chaos_now >= cfg.chaos_on
                and chaos_parts(view.match)["completion"] >= cfg.chaos_min_completion_part),
            off=chaos_now < cfg.chaos_off,
            strength=lambda: chaos_now,
            evidence=lambda: self._chaos_evidence(view),
        )
        return found

    def _step_spell(
        self,
        kind: FindingKind,
        team_id: Optional[str],
        hold_s: float,
        on: bool,
        off: bool,
        strength: Callable[[], float],
        evidence: Callable[[], list[Evidence]],
    ) -> list[Finding]:
        tracker = self._spells.setdefault((kind, team_id), _SpellTracker())
        clock = self.state.clock_s
        if not tracker.active:
            tracker.record(clock, on, hold_s)
            if clock < tracker.cooldown_until or not on:
                return []
            if tracker.on_fraction(clock, hold_s) < self.config.hold_duty:
                return []
            start = self._finding(kind, "start", team_id, max(clock - self.state.window_s, 0.0), clock,
                                  strength(), evidence())
            tracker.start, tracker.off_since = start, None
            tracker.history.clear()
            return [start]

        if not off:
            tracker.off_since = None
            return []
        if tracker.off_since is None:
            tracker.off_since = clock
        if clock - tracker.off_since < self.config.release_s:
            return []
        return [self._end_spell(tracker, tracker.off_since, evidence())]

    def _end_spell(self, tracker: _SpellTracker, end_s: float, evidence: list[Evidence]) -> Finding:
        start = tracker.start
        assert start is not None
        duration = self._evidence("spell_duration", end_s - start.start_s, "s", start.team_id, start.start_s, end_s)
        end = self._finding(start.kind, "end", start.team_id, start.start_s, end_s, start.severity,
                            [duration, *evidence])
        tracker.start, tracker.off_since = None, None
        tracker.history.clear()
        tracker.cooldown_until = self.state.clock_s + self.config.cooldown_s
        return end

    def _close_all_spells(self) -> list[Finding]:
        """At full time every running spell ends."""
        found = []
        for tracker in self._spells.values():
            if tracker.active:
                found.append(self._end_spell(tracker, self.state.clock_s, []))
        return found

    # ------------------------------------------------------------------ #
    # Evidence per spell kind
    # ------------------------------------------------------------------ #

    def _surge_evidence(self, team_id: str, view: _View) -> list[Evidence]:
        state = self.state
        opp = view.teams[state.opponent(team_id)]
        short_start = max(state.clock_s - state.short_window_s, 0.0)
        return [
            self._evidence("pressures_per_min", state.pressure_rate(team_id, short=True), "per_min", team_id,
                           short_start, baseline=state.pressure_rate_before(team_id, short=True)),
            self._evidence("tackles_won", view.teams[team_id].tackles_won, "count", team_id),
            self._evidence("opponent_pass_completion", opp.pass_completion_pct or 0.0, "pct", opp.team_id,
                           baseline=state.totals(opp.team_id).pass_completion_pct),
            self._evidence("momentum", self._team_momentum(team_id, view.momentum), "index", team_id),
        ]

    def _siege_evidence(self, team_id: str, view: _View) -> list[Evidence]:
        mine, theirs = view.teams[team_id], view.teams[self.state.opponent(team_id)]
        return [
            self._evidence("field_tilt", mine.field_tilt_pct, "pct", team_id, baseline=50.0),
            self._evidence("shots", mine.shots, "count", team_id, baseline=theirs.shots),
            self._evidence("xg", mine.xg, "xg", team_id, baseline=theirs.xg),
            self._evidence("possession", mine.possession_pct, "pct", team_id, baseline=50.0),
            self._evidence("pressures_per_min", mine.pressures_per_min, "per_min", team_id,
                           baseline=theirs.pressures_per_min),
            self._evidence("opponent_final_third_passes", theirs.final_third_passes, "count", theirs.team_id),
        ]

    def _momentum_evidence(self, team_id: str, view: _View) -> list[Evidence]:
        mine, theirs = view.teams[team_id], view.teams[self.state.opponent(team_id)]
        return [
            self._evidence("momentum", self._team_momentum(team_id, view.momentum), "index", team_id),
            self._evidence("field_tilt", mine.field_tilt_pct, "pct", team_id, baseline=50.0),
            self._evidence("shots", mine.shots, "count", team_id, baseline=theirs.shots),
            self._evidence("xg", mine.xg, "xg", team_id, baseline=theirs.xg),
            self._evidence("pressures_per_min", mine.pressures_per_min, "per_min", team_id,
                           baseline=theirs.pressures_per_min),
        ]

    def _chaos_evidence(self, view: _View) -> list[Evidence]:
        m = view.match
        return [
            self._evidence("chaos_index", chaos(m), "index"),
            self._evidence("turnovers_per_min", m.turnovers_per_min, "per_min", baseline=TYPICAL_TURNOVERS_PER_MIN),
            self._evidence("pass_completion", m.pass_completion_pct or 0.0, "pct", baseline=TYPICAL_COMPLETION_PCT),
            self._evidence("mean_spell", m.mean_spell_s or 0.0, "s", baseline=TYPICAL_SPELL_S),
        ]
