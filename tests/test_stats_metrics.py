"""Tests for MatchState: the numbers behind every finding."""

from __future__ import annotations

import unittest

from _events import AWAY, HOME, MatchBuilder

from pitchside.generator import generate_match
from pitchside.schema import PassEvent, ShotEvent
from pitchside.stats.metrics import MatchState


def _state(builder: MatchBuilder, **kwargs) -> MatchState:
    state = MatchState(**kwargs)
    for event in builder.events:
        state.update(event)
    return state


class PossessionAndPassingTests(unittest.TestCase):
    def setUp(self) -> None:
        b = MatchBuilder().start()
        b.pass_(5, HOME, 40, 60).pass_(12, HOME, 60, 80).pass_(20, HOME, 80, 90, outcome="incomplete",
                                                                difficulty=0.7)
        b.change(60, AWAY, 90, spell_s=60, passes=2)
        b.pass_(70, AWAY, 30, 20).pass_(80, AWAY, 20, 10)  # away final third is x < 35
        b.change(90, HOME, 15, reason="tackle", spell_s=30, passes=2)
        b.pass_(120, HOME, 50, 50)
        self.state = _state(b)

    def test_time_based_possession(self) -> None:
        self.assertEqual(self.state.team(HOME).possession_pct, 75.0)  # 0-60 and 90-120 of 120 s
        self.assertEqual(self.state.team(AWAY).possession_pct, 25.0)
        self.assertEqual(self.state.totals(HOME).possession_pct, 75.0)

    def test_completion_and_difficulty(self) -> None:
        home = self.state.team(HOME)
        self.assertEqual(home.passes, 4)
        self.assertEqual(home.pass_completion_pct, 75.0)
        self.assertAlmostEqual(home.pass_difficulty, (0.3 * 3 + 0.7) / 4)

    def test_field_tilt_counts_attacking_third_passes(self) -> None:
        # home: one pass starting beyond x = 70; away: one pass starting below x = 35, one at x = 30
        self.assertEqual(self.state.team(HOME).final_third_passes, 1)
        self.assertEqual(self.state.team(AWAY).final_third_passes, 2)
        self.assertAlmostEqual(self.state.team(HOME).field_tilt_pct, 33.3)

    def test_turnovers_skip_kickoffs(self) -> None:
        match = self.state.match()
        self.assertEqual(match.turnovers, 2)
        self.assertEqual(match.turnovers_per_min, 1.0)  # 2 in a 120 s window
        self.assertEqual(match.mean_spell_s, 45.0)
        self.assertAlmostEqual(match.pass_completion_pct, 83.3)  # 5 of 6, both teams

    def test_current_spell(self) -> None:
        spell = self.state.spell
        self.assertEqual((spell.team_id, spell.reason, spell.start_s, spell.completed_passes), (HOME, "tackle", 90, 1))


class ShotsPressureAndWindowTests(unittest.TestCase):
    def test_shots_xg_goals_and_score(self) -> None:
        b = MatchBuilder().start()
        b.shot(30, HOME, 95, xg=0.3, outcome="goal").change(60, AWAY, 52.5, reason="kickoff")
        b.shot(100, AWAY, 10, xg=0.05)
        state = _state(b)
        self.assertEqual((state.team(HOME).shots, state.team(HOME).xg, state.team(HOME).goals), (1, 0.3, 1))
        self.assertEqual((state.score.home, state.score.away), (1, 0))

    def test_pressure_rates_short_window_and_total(self) -> None:
        b = MatchBuilder().start()
        for t in range(400, 580, 20):  # 9 pressures in the last 3 minutes
            b.press(t, AWAY)
        b.pass_(600, HOME, 30)
        state = _state(b)
        # window is (420, 600]: the pressures at 400 and 420 have fallen out
        self.assertAlmostEqual(state.pressure_rate(AWAY, short=True), 7 / 3)
        self.assertAlmostEqual(state.pressure_rate(AWAY), 9 / 5)
        self.assertAlmostEqual(state.pressure_rate_total(AWAY), 9 / 10)
        self.assertAlmostEqual(state.pressure_rate_before(AWAY, short=True), 2 / 7)  # 400, 420 in 0-420 s
        self.assertEqual(state.team(AWAY).pressures, 9)

    def test_no_baseline_without_a_minute_of_history(self) -> None:
        state = _state(MatchBuilder().start().press(100, AWAY))
        self.assertIsNone(state.pressure_rate_before(AWAY))

    def test_numbers_expire_with_the_window(self) -> None:
        b = MatchBuilder().start().shot(30, HOME, 95, xg=0.4).change(40, AWAY, 3, reason="shot_saved")
        b.pass_(500, AWAY, 20)
        state = _state(b)
        self.assertEqual(state.team(HOME).shots, 0)
        self.assertEqual(state.totals(HOME).shots, 1)

    def test_rates_never_divide_by_less_than_a_minute(self) -> None:
        state = _state(MatchBuilder().start().press(5, HOME))
        self.assertEqual(state.pressure_rate(HOME), 1.0)


class ValidationTests(unittest.TestCase):
    def test_needs_match_start_first(self) -> None:
        b = MatchBuilder().start()
        with self.assertRaises(ValueError):
            MatchState().update(b.events[1])

    def test_rejects_other_matches(self) -> None:
        state = _state(MatchBuilder().start())
        other = MatchBuilder("other").start()
        with self.assertRaises(ValueError):
            state.update(other.events[-1])


class GeneratedMatchTests(unittest.TestCase):
    def test_totals_agree_with_raw_events(self) -> None:
        events = generate_match(seed=2, storyline="balanced").events
        state = MatchState()
        for event in events:
            state.update(event)
        for team in (HOME, AWAY):
            passes = [e for e in events if isinstance(e, PassEvent) and e.team_id == team]
            shots = [e for e in events if isinstance(e, ShotEvent) and e.team_id == team]
            totals = state.totals(team)
            self.assertEqual(totals.passes, len(passes))
            self.assertEqual(totals.shots, len(shots))
            self.assertAlmostEqual(totals.xg, sum(s.xg for s in shots), places=3)
        possession = state.totals(HOME).possession_pct + state.totals(AWAY).possession_pct
        self.assertAlmostEqual(possession, 100.0, places=0)
        self.assertTrue(state.finished)


if __name__ == "__main__":
    unittest.main()
