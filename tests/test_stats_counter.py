"""Tests for counter-attack findings."""

from __future__ import annotations

import unittest

from _events import AWAY, HOME, MatchBuilder

from pitchside.generator import generate_match
from pitchside.stats import FindingKind, detect
from pitchside.stats.findings import COUNTER_MAX_SEVERITY, LLM_TRIGGER_SEVERITY


def _counters(builder: MatchBuilder):
    return [f for f in detect(builder.end(5400).events) if f.kind is FindingKind.COUNTER_ATTACK]


def _base() -> MatchBuilder:
    b = MatchBuilder().start()
    b.pass_(10, HOME, 50, 60).pass_(40, HOME, 60, 75)
    return b


class CounterAttackTests(unittest.TestCase):
    def test_quick_direct_break_ending_in_a_shot(self) -> None:
        # away win the ball on their own 30 m line (x = 75) and shoot 12 s later from x = 14
        b = _base().change(50, AWAY, 75, reason="interception", spell_s=50, passes=2)
        b.pass_(54, AWAY, 75, 45).pass_(58, AWAY, 45, 18).shot(62, AWAY, 14, xg=0.22, outcome="saved")
        counters = _counters(b)
        self.assertEqual(len(counters), 1)
        c = counters[0]
        self.assertEqual((c.team_id, c.status, c.start_s, c.end_s), (AWAY, "moment", 50, 62))
        self.assertEqual(c.event_ids, [b.events[5].event_id, b.events[8].event_id])  # regain, shot
        ev = {e.metric: e.value for e in c.evidence}
        self.assertEqual(ev["seconds_to_finish"], 12)
        self.assertEqual(ev["passes_in_move"], 2)
        self.assertEqual(ev["metres_gained"], 61)
        self.assertEqual(ev["regain_distance_from_own_goal"], 30)
        self.assertEqual(ev["ended_with_shot"], 1)
        self.assertEqual(ev["shot_xg"], 0.22)
        self.assertAlmostEqual(c.severity, 0.194)  # 0.1 base + 0.05 shot + 0.1 * (0.22 / 0.5)

    def test_quick_break_ending_with_a_pass_into_the_box(self) -> None:
        b = _base().change(50, AWAY, 75, reason="tackle", spell_s=50, passes=2)
        b.pass_(54, AWAY, 75, 45).pass_(58, AWAY, 45, 12)  # completed pass ends inside the box (x <= 16.5)
        b.shot(60, AWAY, 11, xg=0.4, outcome="saved")  # the shot that follows must not be a second counter
        counters = _counters(b)
        self.assertEqual(len(counters), 1)
        c = counters[0]
        self.assertEqual((c.start_s, c.end_s), (50, 58))
        ev = {e.metric: e.value for e in c.evidence}
        self.assertEqual(ev["ended_with_shot"], 0)
        self.assertNotIn("shot_xg", ev)
        self.assertEqual(ev["metres_gained"], 63)
        self.assertAlmostEqual(c.severity, 0.1)  # box entry without a shot: the floor
        self.assertEqual(c.event_ids, [b.events[5].event_id, b.events[7].event_id])  # regain, the entry pass

    def test_incomplete_or_wide_passes_do_not_count_as_box_entries(self) -> None:
        b = _base().change(50, AWAY, 75, reason="tackle")
        b.pass_(54, AWAY, 75, 12, outcome="incomplete")  # into the box but lost
        b.pass_(56, AWAY, 45, 12, y=5)  # x is right, but wide of the penalty area
        self.assertEqual(_counters(b), [])

    def test_counter_that_scores_is_also_a_goal(self) -> None:
        b = _base().change(50, AWAY, 60, reason="tackle")
        b.pass_(53, AWAY, 60, 25).shot(57, AWAY, 12, xg=0.4, outcome="goal")
        findings = detect(b.end(5400).events)
        kinds = sorted(f.kind.value for f in findings)
        self.assertEqual(kinds, ["counter_attack", "goal"])
        counter = next(f for f in findings if f.kind is FindingKind.COUNTER_ATTACK)
        self.assertAlmostEqual(counter.severity, 0.28)  # even a counter that scores stays low
        goal = next(f for f in findings if f.kind is FindingKind.GOAL)
        self.assertTrue(goal.can_trigger_model_call)  # the goal finding is what gets narrated
        self.assertFalse(counter.can_trigger_model_call)

    def test_slow_build_up_is_not_a_counter(self) -> None:
        b = _base().change(50, AWAY, 75, reason="interception")
        b.pass_(55, AWAY, 75, 45).pass_(60, AWAY, 45, 18).shot(63, AWAY, 14, xg=0.2)  # 13 s
        self.assertEqual(_counters(b), [])

    def test_too_many_passes_is_not_a_counter(self) -> None:
        b = _base().change(50, AWAY, 90, reason="interception")
        for i, x in enumerate((90, 80, 70, 60, 50)):  # five completed passes in 10 s
            b.pass_(51 + 2 * i, AWAY, x, x - 10)
        b.shot(61, AWAY, 20, xg=0.1)
        self.assertEqual(_counters(b), [])

    def test_short_break_is_not_a_counter(self) -> None:
        b = _base().change(50, AWAY, 40, reason="interception")
        b.pass_(53, AWAY, 40, 15)  # quick and into the box, but only 25 m gained from the regain point
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


class CounterSeverityTests(unittest.TestCase):
    """Counter-attacks are context: they must never trigger a model call on their own."""

    def test_severity_is_low_and_capped(self) -> None:
        self.assertLess(COUNTER_MAX_SEVERITY, LLM_TRIGGER_SEVERITY)
        # the best possible counter: a goal with a huge xG, one pass, 100% of the cap formula
        b = _base().change(50, AWAY, 90, reason="interception")
        b.pass_(53, AWAY, 90, 50).shot(56, AWAY, 4, xg=0.9, outcome="goal")
        counter = _counters(b)[0]
        self.assertLessEqual(counter.severity, COUNTER_MAX_SEVERITY)
        self.assertFalse(counter.can_trigger_model_call)

    def test_more_dangerous_counters_rank_higher_but_stay_below_the_floor(self) -> None:
        def severity(**shot) -> float:
            b = _base().change(50, AWAY, 90, reason="interception")
            if shot:
                b.shot(56, AWAY, 30, **shot)
            else:
                b.pass_(56, AWAY, 90, 12)  # box entry, no shot
            return _counters(b)[0].severity

        entry, weak, strong = severity(), severity(xg=0.05), severity(xg=0.4)
        self.assertLess(entry, weak)
        self.assertLess(weak, strong)
        self.assertLess(strong, LLM_TRIGGER_SEVERITY)

    def test_no_counter_in_real_matches_can_trigger_a_model_call(self) -> None:
        total = 0
        for storyline in ("balanced", "siege", "end_to_end"):
            for seed in (1, 2, 3):
                for f in detect(generate_match(seed=seed, storyline=storyline).events):
                    if f.kind is FindingKind.COUNTER_ATTACK:
                        total += 1
                        self.assertLessEqual(f.severity, COUNTER_MAX_SEVERITY, f.finding_id)
                        self.assertFalse(f.can_trigger_model_call, f.finding_id)
        self.assertGreater(total, 0)

    def test_goals_and_big_chances_can_trigger(self) -> None:
        b = _base().change(50, AWAY, 60, reason="tackle")
        b.shot(55, AWAY, 12, xg=0.4, outcome="saved")
        big = [f for f in detect(b.end(5400).events) if f.kind is FindingKind.BIG_CHANCE]
        self.assertTrue(big and big[0].can_trigger_model_call)


if __name__ == "__main__":
    unittest.main()
