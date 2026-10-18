"""Tests for the stats engine: moment findings, determinism and snapshots."""

from __future__ import annotations

import unittest

from _events import AWAY, HOME, MatchBuilder

from pitchside.generator import generate_match
from pitchside.stats import FindingKind, StatsEngine, detect


def _kinds(findings, kind):
    return [f for f in findings if f.kind is kind]


class GoalAndBigChanceTests(unittest.TestCase):
    def setUp(self) -> None:
        b = MatchBuilder().start()
        b.pass_(10, HOME, 60, 85).shot(20, HOME, 92, xg=0.31, outcome="goal")  # 1-0, go-ahead
        b.change(50, AWAY, 52.5, reason="kickoff")
        b.shot(80, AWAY, 12, xg=0.25, outcome="saved")  # big chance, exactly on the threshold
        b.change(85, HOME, 4, reason="shot_saved")
        b.shot(120, HOME, 80, xg=0.24, outcome="off_target")  # not big
        b.change(130, AWAY, 4, reason="goal_kick")
        b.change(150, HOME, 40, reason="interception")
        b.shot(160, HOME, 95, xg=0.6, outcome="goal")  # 2-0, extends the lead
        self.findings = detect(b.end(200).events)

    def test_goals_carry_the_shot_and_score(self) -> None:
        goals = _kinds(self.findings, FindingKind.GOAL)
        self.assertEqual([g.team_id for g in goals], [HOME, HOME])
        first, second = goals
        self.assertEqual(first.status, "moment")
        self.assertEqual((first.score.home, first.score.away), (1, 0))
        self.assertEqual((second.score.home, second.score.away), (2, 0))
        self.assertEqual(first.start_s, 0.0)  # the move began at kick-off
        self.assertEqual(second.start_s, 150.0)
        self.assertEqual((first.severity, second.severity), (1.0, 0.85))
        self.assertEqual(first.finding_id, "test_match:goal:0")
        self.assertEqual(second.finding_id, "test_match:goal:1")

    def test_goal_evidence(self) -> None:
        first = _kinds(self.findings, FindingKind.GOAL)[0]
        evidence = {e.metric: e for e in first.evidence}
        self.assertEqual(evidence["shot_xg"].value, 0.31)
        self.assertEqual((evidence["shot_xg"].window_start_s, evidence["shot_xg"].window_end_s), (20, 20))
        self.assertEqual(evidence["match_xg"].value, 0.31)
        self.assertEqual(evidence["match_xg"].baseline, 0.0)  # opponent's xG
        self.assertGreater(evidence["momentum"].value, 0)

    def test_big_chance_threshold_and_goals_are_not_double_counted(self) -> None:
        chances = _kinds(self.findings, FindingKind.BIG_CHANCE)
        self.assertEqual([(c.team_id, c.event_ids) for c in chances], [(AWAY, [6])])
        self.assertAlmostEqual(chances[0].severity, 0.65)


class GoalOriginEvidenceTests(unittest.TestCase):
    """A goal finding says how its possession began, so a counter-goal can be told from verified numbers."""

    @staticmethod
    def _goal(b: MatchBuilder):
        return _kinds(detect(b.end(5400).events), FindingKind.GOAL)[0]

    @staticmethod
    def _ev(finding) -> dict:
        return {e.metric: e for e in finding.evidence}

    def test_counter_goal_carries_turnover_passes_and_metres(self) -> None:
        b = MatchBuilder().start().pass_(10, HOME, 50, 60)
        b.change(50, AWAY, 70, reason="interception", spell_s=40, passes=1)
        b.pass_(54, AWAY, 70, 40).pass_(58, AWAY, 40, 22).shot(61, AWAY, 12, xg=0.35, outcome="goal")
        ev = self._ev(self._goal(b))
        self.assertEqual(ev["seconds_since_turnover"].value, 11)
        self.assertEqual(ev["passes_in_spell"].value, 2)
        self.assertEqual(ev["metres_gained"].value, 58)  # away attack towards x = 0: 70 -> 12
        self.assertEqual(ev["possession_won_in_open_play"].value, 1)
        for name in ("seconds_since_turnover", "passes_in_spell", "metres_gained"):
            self.assertEqual((ev[name].window_start_s, ev[name].window_end_s), (50, 61), name)
            self.assertEqual(ev[name].team_id, AWAY)

    def test_goal_from_a_restart_says_it_was_not_won_in_open_play(self) -> None:
        b = MatchBuilder().start()
        b.change(50, HOME, 60, reason="goal_kick")
        for i in range(8):
            b.pass_(55 + 8 * i, HOME, 60 + 3 * i, 63 + 3 * i)
        b.shot(125, HOME, 90, xg=0.2, outcome="goal")
        ev = self._ev(self._goal(b))
        self.assertEqual(ev["possession_won_in_open_play"].value, 0)
        self.assertEqual(ev["passes_in_spell"].value, 8)
        self.assertEqual(ev["seconds_since_turnover"].value, 75)
        self.assertEqual(ev["metres_gained"].value, 30)

    def test_numbers_agree_with_the_counter_finding_for_the_same_move(self) -> None:
        b = MatchBuilder().start().pass_(10, HOME, 50, 60)
        b.change(50, AWAY, 70, reason="tackle")
        b.pass_(53, AWAY, 70, 40).shot(57, AWAY, 12, xg=0.35, outcome="goal")
        findings = detect(b.end(5400).events)
        goal = self._ev(_kinds(findings, FindingKind.GOAL)[0])
        counter = {e.metric: e for e in _kinds(findings, FindingKind.COUNTER_ATTACK)[0].evidence}
        self.assertEqual(goal["seconds_since_turnover"].value, counter["seconds_to_finish"].value)
        self.assertEqual(goal["passes_in_spell"].value, counter["passes_in_move"].value)
        self.assertEqual(goal["metres_gained"].value, counter["metres_gained"].value)

    def test_big_chances_carry_it_too_and_real_goals_always_do(self) -> None:
        for goal in _kinds(detect(generate_match(seed=4, storyline="comeback").events), FindingKind.GOAL):
            ev = self._ev(goal)
            for name in ("seconds_since_turnover", "passes_in_spell", "metres_gained", "possession_won_in_open_play"):
                self.assertIn(name, ev, goal.finding_id)
            self.assertGreaterEqual(ev["seconds_since_turnover"].value, 0)
            self.assertEqual(goal.start_s, ev["seconds_since_turnover"].window_start_s)


class GeneratedMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = generate_match(seed=4, storyline="comeback")
        cls.findings = detect(cls.result.events)

    def test_every_goal_in_the_answer_key_is_found(self) -> None:
        goal_ids = [g["event_id"] for g in self.result.answer_key["goals"]]
        found = [f.event_ids[0] for f in _kinds(self.findings, FindingKind.GOAL)]
        self.assertEqual(found, goal_ids)

    def test_deterministic(self) -> None:
        again = detect(generate_match(seed=4, storyline="comeback").events)
        self.assertEqual([f.model_dump_json() for f in again], [f.model_dump_json() for f in self.findings])

    def test_incremental_ingest_equals_batch(self) -> None:
        engine = StatsEngine()
        streamed = []
        for event in self.result.events:
            streamed.extend(engine.ingest(event))
        self.assertEqual(streamed, self.findings)
        self.assertEqual(engine.findings, self.findings)

    def test_findings_never_come_from_the_future(self) -> None:
        for f in self.findings:
            self.assertLessEqual(f.start_s, f.end_s)
            self.assertLessEqual(f.end_s, f.detected_at_s)
            for e in f.evidence:
                self.assertLessEqual(e.window_end_s, f.detected_at_s)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_reports_both_teams(self) -> None:
        engine = StatsEngine()
        for event in generate_match(seed=1, storyline="balanced").events[:300]:
            engine.ingest(event)
        snap = engine.snapshot()
        self.assertEqual(set(snap.teams), {HOME, AWAY})
        self.assertAlmostEqual(sum(t.possession_pct for t in snap.teams.values()), 100.0, places=0)
        self.assertTrue(-1 <= snap.momentum <= 1 and -1 <= snap.chaos <= 1)

    def test_snapshot_before_kickoff_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            StatsEngine().snapshot()


if __name__ == "__main__":
    unittest.main()
