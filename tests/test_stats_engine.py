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
