"""Tests for spell findings: pressure surge, siege, chaos spell, hysteresis and lifecycle."""

from __future__ import annotations

import unittest

from _events import AWAY, HOME, MatchBuilder

from pitchside.stats import FindingKind, detect


class Script:
    """Collects timed actions and builds them in clock order."""

    def __init__(self) -> None:
        self.actions: list[tuple[float, int, object]] = []

    def at(self, t: float, action) -> None:
        self.actions.append((t, len(self.actions), action))

    def calm_play(self, t0: float, t1: float, team: str = HOME) -> None:
        """Settled possession in midfield: a completed pass every 6 s."""
        t = t0
        while t < t1:
            self.at(t, lambda b, t=t: b.pass_(t, team, 50, 55))
            t += 6

    def press(self, times, team: str, x: float = 52.5) -> None:
        for t in times:
            self.at(t, lambda b, t=t: b.press(t, team, x=x))

    def run(self, end_s: float):
        b = MatchBuilder().start()
        for _, _, action in sorted(self.actions, key=lambda a: (a[0], a[1])):
            action(b)
        return detect(b.end(end_s).events)


def _of(findings, kind, status=None):
    return [f for f in findings if f.kind is kind and (status is None or f.status == status)]


class PressureSurgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        s = Script()
        s.calm_play(1, 1800)
        s.press([t + 0.5 for t in range(600, 1200, 15)], AWAY, x=40)  # 4 a minute, 10'-20'
        cls.findings = s.run(1800)

    def test_surge_starts_after_the_hold_and_ends_after_release(self) -> None:
        starts = _of(self.findings, FindingKind.PRESSURE_SURGE, "start")
        ends = _of(self.findings, FindingKind.PRESSURE_SURGE, "end")
        self.assertEqual([f.team_id for f in starts], [AWAY])
        self.assertEqual(len(ends), 1)
        # 3.4/min over a 3-minute window needs ~11 pressures (from 10:00), then a 60 s hold
        self.assertTrue(660 < starts[0].detected_at_s < 840, starts[0].detected_at_s)
        self.assertTrue(1200 < ends[0].end_s < 1400, ends[0].end_s)
        self.assertGreater(ends[0].detected_at_s, ends[0].end_s)  # release time passed first
        self.assertEqual(ends[0].start_s, starts[0].start_s)

    def test_surge_evidence_compares_with_the_match_so_far(self) -> None:
        start = _of(self.findings, FindingKind.PRESSURE_SURGE, "start")[0]
        rate = {e.metric: e for e in start.evidence}["pressures_per_min"]
        self.assertGreaterEqual(rate.value, 3.4)
        # baseline = rate before the judged window, so the surge does not inflate its own baseline
        self.assertLess(rate.baseline, 0.5)
        self.assertEqual(rate.window_end_s - rate.window_start_s, 180)

    def test_calm_play_alone_produces_no_findings(self) -> None:
        s = Script()
        s.calm_play(1, 1800)
        self.assertEqual(s.run(1800), [])


class HysteresisTests(unittest.TestCase):
    def test_nothing_before_the_windows_are_full(self) -> None:
        s = Script()
        s.calm_play(1, 900)
        s.press([t + 0.5 for t in range(5, 100, 5)], AWAY)  # frantic pressing in the first 100 s only
        self.assertEqual(_of(s.run(900), FindingKind.PRESSURE_SURGE), [])

    def test_spells_wait_for_the_warmup(self) -> None:
        s = Script()
        s.calm_play(1, 900)
        s.press([t + 0.5 for t in range(5, 280, 5)], AWAY)  # still inside the 3-minute window at 300 s
        surges = _of(s.run(900), FindingKind.PRESSURE_SURGE, "start")
        self.assertEqual(len(surges), 1)
        self.assertGreaterEqual(surges[0].detected_at_s, 300)

    def test_a_short_burst_does_not_start_a_spell(self) -> None:
        s = Script()
        s.calm_play(1, 1800)
        s.press([t + 0.5 for t in range(600, 640, 4)], AWAY)  # ten pressures in 40 s, then nothing
        self.assertEqual(_of(s.run(1800), FindingKind.PRESSURE_SURGE), [])

    def test_full_time_closes_running_spells(self) -> None:
        s = Script()
        s.calm_play(1, 1200)
        s.press([t + 0.5 for t in range(600, 1200, 12)], AWAY)
        end = _of(s.run(1200), FindingKind.PRESSURE_SURGE, "end")
        self.assertEqual(len(end), 1)
        self.assertEqual((end[0].end_s, end[0].detected_at_s), (1200, 1200))


class SiegeTests(unittest.TestCase):
    def test_pressing_high_against_a_passive_low_block(self) -> None:
        s = Script()
        s.calm_play(1, 600)
        for t in range(600, 1500, 6):  # home pass in the attacking third; away never press
            s.at(t, lambda b, t=t: b.pass_(t, HOME, 80, 85))
        s.press([t + 0.5 for t in range(600, 1500, 30)], HOME, x=80)  # two a minute
        findings = s.run(1500)
        starts = _of(findings, FindingKind.SIEGE, "start")
        self.assertEqual([f.team_id for f in starts], [HOME])
        ev = {e.metric: e for e in starts[0].evidence}
        self.assertGreaterEqual(ev["field_tilt"].value, 55)
        self.assertGreaterEqual(ev["pressures_per_min"].value, 1.6)
        self.assertEqual(ev["pressures_per_min"].baseline, 0.0)  # the opponent's pressing
        self.assertEqual(_of(findings, FindingKind.PRESSURE_SURGE), [])  # 2/min is not a surge

    def test_no_siege_when_both_sides_press(self) -> None:
        s = Script()
        s.calm_play(1, 600)
        for t in range(600, 1500, 6):
            s.at(t, lambda b, t=t: b.pass_(t, HOME, 80, 85))
        s.press([t + 0.5 for t in range(600, 1500, 30)], HOME, x=80)
        s.press([t + 0.7 for t in range(600, 1500, 60)], AWAY, x=80)  # away still press once a minute
        self.assertEqual(_of(s.run(1500), FindingKind.SIEGE), [])


class ChaosTests(unittest.TestCase):
    def test_turnovers_and_failed_passes_make_a_chaos_spell(self) -> None:
        s = Script()
        s.calm_play(1, 600)

        def broken(b, t, team):
            b.change(t, team, 52.5, spell_s=15, passes=1)
            b.pass_(t + 4, team, 50, 60)
            b.pass_(t + 9, team, 60, 70, outcome="incomplete")

        for i, t in enumerate(range(600, 1200, 15)):  # ball changes hands every 15 s; half the passes fail
            s.at(t, lambda b, t=t, team=(AWAY if i % 2 == 0 else HOME): broken(b, t, team))
        s.at(1200, lambda b: b.change(1200, HOME, 52.5, spell_s=15))
        s.calm_play(1201, 1800)
        findings = s.run(1800)
        starts = _of(findings, FindingKind.CHAOS_SPELL, "start")
        self.assertEqual(len(starts), 1)
        self.assertIsNone(starts[0].team_id)
        ev = {e.metric: e for e in starts[0].evidence}
        self.assertGreaterEqual(ev["chaos_index"].value, 0.8)
        self.assertLess(ev["pass_completion"].value, ev["pass_completion"].baseline)
        self.assertEqual(len(_of(findings, FindingKind.CHAOS_SPELL, "end")), 1)


if __name__ == "__main__":
    unittest.main()
