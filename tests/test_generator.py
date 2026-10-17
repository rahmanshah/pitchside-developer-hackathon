"""Behavioural tests for the match generator.

Written with unittest so they run anywhere; pytest collects them too.
"""

from __future__ import annotations

import statistics
import unittest

from pitchside.generator import generate_match
from pitchside.schema import (
    HALF_LENGTH_S,
    PassEvent,
    PossessionChangeEvent,
    PressureEvent,
    ShotEvent,
    TackleEvent,
    to_json_line,
)
from pitchside.storylines import STORYLINES

HOME, AWAY = "aur", "irv"
SEEDS = range(1, 7)


class StructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = generate_match(seed=11, storyline="comeback")
        cls.events = cls.result.events

    def test_same_seed_is_identical(self) -> None:
        again = generate_match(seed=11, storyline="comeback")
        self.assertEqual([to_json_line(e) for e in self.events], [to_json_line(e) for e in again.events])
        self.assertEqual(self.result.answer_key, again.answer_key)

    def test_different_seed_differs(self) -> None:
        other = generate_match(seed=12, storyline="comeback")
        self.assertNotEqual([to_json_line(e) for e in self.events], [to_json_line(e) for e in other.events])

    def test_bookends_and_ids(self) -> None:
        types = [e.type for e in self.events]
        self.assertEqual(types[0], "match_start")
        self.assertEqual(types[-1], "match_end")
        self.assertEqual(types.count("period_start"), 2)
        self.assertEqual(types.count("period_end"), 2)
        self.assertEqual([e.event_id for e in self.events], list(range(len(self.events))))
        self.assertEqual(len({e.match_id for e in self.events}), 1)

    def test_clock_is_monotonic_and_periods_match(self) -> None:
        clocks = [e.clock_s for e in self.events]
        self.assertEqual(clocks, sorted(clocks))
        self.assertGreaterEqual(clocks[0], 0)
        self.assertLessEqual(clocks[-1], 2 * HALF_LENGTH_S)
        for e in self.events:
            if e.clock_s < HALF_LENGTH_S:
                self.assertEqual(e.period, 1, e)
            elif e.clock_s > HALF_LENGTH_S:
                self.assertEqual(e.period, 2, e)

    def test_possession_chain_is_consistent(self) -> None:
        """Every action is performed by the team that actually has the ball."""
        possessor = None
        for e in self.events:
            if isinstance(e, PossessionChangeEvent):
                if possessor is not None:
                    self.assertEqual(e.from_team_id, possessor, e)
                self.assertNotEqual(e.team_id, e.from_team_id)
                possessor = e.team_id
            elif isinstance(e, (PassEvent, ShotEvent)):
                self.assertEqual(e.team_id, possessor, e)
            elif isinstance(e, (TackleEvent, PressureEvent)):
                self.assertNotEqual(e.team_id, possessor, e)

    def test_scores_add_up(self) -> None:
        home = away = 0
        for e in self.events:
            if isinstance(e, ShotEvent) and e.outcome == "goal":
                home += e.team_id == HOME
                away += e.team_id == AWAY
                self.assertEqual((e.score_after.home, e.score_after.away), (home, away))
            elif isinstance(e, ShotEvent):
                self.assertIsNone(e.score_after)
        final = self.events[-1].final_score
        self.assertEqual((final.home, final.away), (home, away))

    def test_player_ids_belong_to_the_right_team(self) -> None:
        for e in self.events:
            if isinstance(e, PassEvent):
                self.assertTrue(e.player_id.startswith(e.team_id + "_"))
                self.assertTrue(e.receiver_id.startswith(e.team_id + "_"))
                self.assertNotEqual(e.player_id, e.receiver_id)

    def test_pass_fields_are_physically_sensible(self) -> None:
        for e in self.events:
            if isinstance(e, PassEvent):
                self.assertGreater(e.ball_speed_kmh, 15)
                self.assertLess(e.ball_speed_kmh, 120)
            elif isinstance(e, ShotEvent):
                self.assertGreater(e.shot_speed_kmh, 50)
                self.assertLess(e.shot_speed_kmh, 135)


class InputValidationTests(unittest.TestCase):
    def test_unknown_storyline(self) -> None:
        with self.assertRaises(ValueError):
            generate_match(storyline="nope")

    def test_same_club_both_sides(self) -> None:
        with self.assertRaises(ValueError):
            generate_match(home="aur", away="aur")


class RealismTests(unittest.TestCase):
    """The balanced control should look like football, not noise."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.matches = [generate_match(seed=s, storyline="balanced").events for s in SEEDS]

    def test_volumes_and_completion(self) -> None:
        for events in self.matches:
            passes = [e for e in events if isinstance(e, PassEvent)]
            shots = [e for e in events if isinstance(e, ShotEvent)]
            completion = sum(p.outcome == "complete" for p in passes) / len(passes)
            self.assertTrue(500 <= len(passes) <= 1000, len(passes))
            self.assertTrue(0.70 <= completion <= 0.92, completion)
            self.assertTrue(8 <= len(shots) <= 45, len(shots))

    def test_average_goals_per_match_is_plausible(self) -> None:
        goals = [sum(isinstance(e, ShotEvent) and e.outcome == "goal" for e in ev) for ev in self.matches]
        self.assertTrue(0.5 <= statistics.mean(goals) <= 5.0, goals)


class StorylineTests(unittest.TestCase):
    """Planted narratives must actually happen, and be visible in the data."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = {
            name: [generate_match(seed=s, storyline=name) for s in SEEDS]
            for name in ("comeback", "siege", "end_to_end")
        }

    def test_scripted_goals_land_after_their_minute(self) -> None:
        for name, runs in self.runs.items():
            storyline = STORYLINES[name]
            for run in runs:
                scripted = run.answer_key["scripted_goals"]
                self.assertEqual(len(scripted), len(storyline.goals), (name, run.answer_key["seed"]))
                for planted in scripted:
                    self.assertGreaterEqual(planted["clock_s"], planted["scripted_minute"] * 60)
                    # not absurdly late either: within ten minutes of the script
                    self.assertLessEqual(planted["clock_s"], (planted["scripted_minute"] + 10) * 60)

    def test_scripted_scoring_makes_the_script_the_only_scoring(self) -> None:
        expected = {"comeback": (2, 1), "siege": (2, 1), "end_to_end": (2, 2)}
        for name, runs in self.runs.items():
            for run in runs:
                final = run.answer_key["final_score"]
                self.assertEqual((final["home"], final["away"]), expected[name], name)
                self.assertEqual(len(run.answer_key["goals"]), len(STORYLINES[name].goals))

    def _shots_per_minute(self, run, team: str, start_s: float, end_s: float) -> float:
        n = sum(
            isinstance(e, ShotEvent) and e.team_id == team and start_s <= e.clock_s < end_s for e in run.events
        )
        return n / ((end_s - start_s) / 60)

    def test_comeback_press_surge_is_visible(self) -> None:
        early = statistics.mean(self._shots_per_minute(r, HOME, 0, 2400) for r in self.runs["comeback"])
        surge = statistics.mean(self._shots_per_minute(r, HOME, 3300, 5400) for r in self.runs["comeback"])
        self.assertGreater(surge, 2 * early)

    def test_siege_shot_volume_is_visible(self) -> None:
        before = statistics.mean(self._shots_per_minute(r, HOME, 0, 1080) for r in self.runs["siege"])
        during = statistics.mean(self._shots_per_minute(r, HOME, 1080, 2280) for r in self.runs["siege"])
        self.assertGreater(during, 2 * before)

    def test_chaos_lowers_pass_completion(self) -> None:
        def completion(run, start_s: float, end_s: float) -> float:
            passes = [e for e in run.events if isinstance(e, PassEvent) and start_s <= e.clock_s < end_s]
            return sum(p.outcome == "complete" for p in passes) / len(passes)

        calm = statistics.mean(completion(r, 0, 1800) for r in self.runs["end_to_end"])
        chaos = statistics.mean(completion(r, 1800, 3600) for r in self.runs["end_to_end"])
        self.assertLess(chaos, calm - 0.05)


if __name__ == "__main__":
    unittest.main()
