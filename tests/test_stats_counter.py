"""Tests for counter-attack findings."""

from __future__ import annotations

import unittest

from _events import AWAY, HOME, MatchBuilder

from pitchside.stats import FindingKind, detect


def _counters(builder: MatchBuilder):
    return [f for f in detect(builder.end(5400).events) if f.kind is FindingKind.COUNTER_ATTACK]


def _base() -> MatchBuilder:
    b = MatchBuilder().start()
    b.pass_(10, HOME, 50, 60).pass_(40, HOME, 60, 75)
    return b


class CounterAttackTests(unittest.TestCase):
    def test_quick_direct_break_after_an_interception(self) -> None:
        # away win the ball on their own 30 m line (x = 75) and shoot 12 s later from x = 14
        b = _base().change(50, AWAY, 75, reason="interception", spell_s=50, passes=2)
        b.pass_(54, AWAY, 75, 45).pass_(58, AWAY, 45, 18).shot(62, AWAY, 14, xg=0.22, outcome="saved")
        counters = _counters(b)
        self.assertEqual(len(counters), 1)
        c = counters[0]
        self.assertEqual((c.team_id, c.status, c.start_s, c.end_s), (AWAY, "moment", 50, 62))
        self.assertEqual(c.event_ids, [b.events[5].event_id, b.events[8].event_id])  # regain, shot
        ev = {e.metric: e.value for e in c.evidence}
        self.assertEqual(ev["seconds_to_shot"], 12)
        self.assertEqual(ev["passes_before_shot"], 2)
        self.assertEqual(ev["metres_gained"], 61)
        self.assertEqual(ev["regain_distance_from_own_goal"], 30)
        self.assertAlmostEqual(c.severity, 0.62)

    def test_counter_that_scores_is_also_a_goal(self) -> None:
        b = _base().change(50, AWAY, 60, reason="tackle")
        b.pass_(53, AWAY, 60, 25).shot(57, AWAY, 12, xg=0.4, outcome="goal")
        findings = detect(b.end(5400).events)
        kinds = sorted(f.kind.value for f in findings)
        self.assertEqual(kinds, ["counter_attack", "goal"])

    def test_slow_build_up_is_not_a_counter(self) -> None:
        b = _base().change(50, AWAY, 75, reason="interception")
        b.pass_(60, AWAY, 75, 45).pass_(70, AWAY, 45, 18).shot(75, AWAY, 14, xg=0.2)  # 25 s
        self.assertEqual(_counters(b), [])

    def test_too_many_passes_is_not_a_counter(self) -> None:
        b = _base().change(50, AWAY, 75, reason="interception")
        for i, x in enumerate((75, 65, 55, 45, 35)):
            b.pass_(51 + 2 * i, AWAY, x, x - 10)
        b.shot(63, AWAY, 20, xg=0.1)
        self.assertEqual(_counters(b), [])

    def test_dead_ball_restarts_are_not_counters(self) -> None:
        for reason in ("goal_kick", "out_of_play", "shot_saved", "kickoff"):
            b = _base().change(50, AWAY, 100, reason=reason)
            b.pass_(53, AWAY, 100, 40).shot(60, AWAY, 15, xg=0.2)
            self.assertEqual(_counters(b), [], reason)

    def test_regain_on_the_edge_of_the_box_is_not_a_counter(self) -> None:
        b = _base().change(50, HOME, 88, reason="tackle")  # home win it high, shoot 6 m further on
        b.shot(54, HOME, 94, xg=0.3)
        self.assertEqual(_counters(b), [])


if __name__ == "__main__":
    unittest.main()
