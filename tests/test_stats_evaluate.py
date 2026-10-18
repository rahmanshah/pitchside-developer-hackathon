"""Tests for scoring findings against answer keys, and the held-out evaluation bars."""

from __future__ import annotations

import unittest

from pitchside.schema import Score
from pitchside.stats import Evidence, Finding, FindingKind
from pitchside.stats.evaluate import (
    TUNING_SEEDS,
    check_bars,
    evaluate_match,
    format_table,
    run_evaluation,
    summarise,
)

HOME, AWAY = "aur", "irv"


def _f(kind: FindingKind, at_s: float, team: str | None = HOME, status: str = "start", seq: int = 0,
       event_ids: list[int] | None = None) -> Finding:
    if kind in (FindingKind.GOAL, FindingKind.BIG_CHANCE, FindingKind.COUNTER_ATTACK):
        status = "moment"
    return Finding(
        finding_id=f"m:{kind.value}:{seq}", match_id="m", kind=kind, status=status, team_id=team,
        start_s=max(at_s - 300, 0), end_s=at_s, detected_at_s=at_s, event_ids=event_ids or [], severity=0.5,
        score=Score(home=0, away=0),
        evidence=[Evidence(metric="x", value=1, unit="count", window_start_s=0, window_end_s=at_s)],
    )


def _key(phases: list[dict], goals: list[int] | None = None) -> dict:
    return {
        "match_id": "m", "seed": 1, "storyline": "test",
        "phases": [{"label": p.get("label", p["kind"]), "kind": p["kind"], "description": "",
                    "team_ids": p.get("team_ids", [HOME]), "start_s": p["start_s"], "end_s": p["end_s"]}
                   for p in phases],
        "goals": [{"event_id": g} for g in (goals or [])],
    }


SIEGE_PHASE = {"kind": "siege", "start_s": 1200, "end_s": 2400}


class SpellMatchingTests(unittest.TestCase):
    def status(self, at_s: float, phase=SIEGE_PHASE, kind=FindingKind.SIEGE, team=HOME):
        result = evaluate_match([_f(kind, at_s, team)], _key([phase]))
        return result.phases[0].status, result.phases[0].lag_s

    def test_hit_window_is_minus_one_to_plus_eight_minutes(self) -> None:
        self.assertEqual(self.status(1200 - 60), ("hit", -60))
        self.assertEqual(self.status(1200 + 480), ("hit", 480))
        self.assertEqual(self.status(1200 + 481), ("missed", None))

    def test_premature_is_counted_separately(self) -> None:
        self.assertEqual(self.status(1200 - 61), ("premature", -61))
        self.assertEqual(self.status(1200 - 120), ("premature", -120))
        self.assertEqual(self.status(1200 - 121), ("missed", None))

    def test_a_later_hit_beats_a_premature_detection(self) -> None:
        findings = [_f(FindingKind.SIEGE, 1100, seq=0), _f(FindingKind.SIEGE, 1300, seq=1)]
        phase = evaluate_match(findings, _key([SIEGE_PHASE])).phases[0]
        self.assertEqual((phase.status, phase.lag_s, phase.finding_id), ("hit", 100, "m:siege:1"))

    def test_kind_and_team_must_match(self) -> None:
        self.assertEqual(self.status(1300, kind=FindingKind.MOMENTUM_SHIFT)[0], "missed")
        self.assertEqual(self.status(1300, team=AWAY)[0], "missed")

    def test_pressure_surge_counts_for_a_momentum_shift(self) -> None:
        phase = {"kind": "momentum_shift", "start_s": 3300, "end_s": 5400}
        self.assertEqual(self.status(3500, phase, FindingKind.PRESSURE_SURGE)[0], "hit")
        self.assertEqual(self.status(3500, phase, FindingKind.MOMENTUM_SHIFT)[0], "hit")

    def test_both_team_phase_takes_a_both_team_finding(self) -> None:
        phase = {"kind": "chaos", "start_s": 1800, "end_s": 3600, "team_ids": [HOME, AWAY]}
        self.assertEqual(self.status(2000, phase, FindingKind.CHAOS_SPELL, None)[0], "hit")

    def test_end_findings_do_not_count(self) -> None:
        result = evaluate_match([_f(FindingKind.SIEGE, 1300, status="end")], _key([SIEGE_PHASE]))
        self.assertEqual(result.phases[0].status, "missed")

    def test_low_block_is_reported_not_scored(self) -> None:
        phase = {"kind": "low_block", "label": "away_low_block", "start_s": 1080, "end_s": 2280, "team_ids": [AWAY]}
        result = evaluate_match([], _key([phase]))
        self.assertEqual(result.phases[0].status, "not_scored")
        summary = summarise([result])["test"]
        self.assertEqual(summary.kinds, {})
        self.assertEqual(summary.not_scored, {"away_low_block": 1})


class CounterInAShortWindowTests(unittest.TestCase):
    """A 6-minute counter phase (48'-54'): hits only from phase start to phase end + 1 min."""

    PHASE = {"kind": "counter_attack", "start_s": 2880, "end_s": 3240, "team_ids": [AWAY]}

    def status(self, at_s: float, team: str = AWAY) -> str:
        return evaluate_match([_f(FindingKind.COUNTER_ATTACK, at_s, team)], _key([self.PHASE])).phases[0].status

    def test_inside_the_phase_and_the_extra_minute(self) -> None:
        self.assertEqual(self.status(2880), "hit")
        self.assertEqual(self.status(3100), "hit")
        self.assertEqual(self.status(3240 + 60), "hit")

    def test_outside_the_short_window(self) -> None:
        self.assertEqual(self.status(3240 + 61), "missed")
        self.assertEqual(self.status(2879), "missed")  # no early tolerance and no "premature" for counters
        self.assertEqual(self.status(2870), "missed")

    def test_other_team_does_not_count(self) -> None:
        self.assertEqual(self.status(3000, HOME), "missed")


class OutsideAndGoalTests(unittest.TestCase):
    def test_outside_uses_every_planted_window_widened(self) -> None:
        phases = [SIEGE_PHASE, {"kind": "low_block", "start_s": 4000, "end_s": 4600, "team_ids": [AWAY]}]
        findings = [
            _f(FindingKind.MOMENTUM_SHIFT, 1200 - 120, seq=0),  # widened siege window: inside
            _f(FindingKind.MOMENTUM_SHIFT, 1200 - 121, seq=1),  # outside
            _f(FindingKind.CHAOS_SPELL, 4300, None),  # inside the (unscored) low-block window
            _f(FindingKind.COUNTER_ATTACK, 2400 + 61, AWAY),  # outside
            _f(FindingKind.BIG_CHANCE, 500),  # moments other than counters are never "outside"
            _f(FindingKind.PRESSURE_SURGE, 300, status="end"),  # ends are not detections
        ]
        outside = evaluate_match(findings, _key(phases)).outside
        self.assertEqual([f.finding_id for f in outside], ["m:momentum_shift:1", "m:counter_attack:0"])

    def test_goals_match_by_shot_event_id(self) -> None:
        findings = [_f(FindingKind.GOAL, 100, event_ids=[42]), _f(FindingKind.GOAL, 200, seq=1, event_ids=[77])]
        result = evaluate_match(findings, _key([], goals=[42, 99]))
        self.assertEqual((result.goals_found, result.goals_total), (1, 2))


class TuningSeedTests(unittest.TestCase):
    """Smoke test of the full pipeline on the tuning seeds (the held-out bars are a separate check)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.summaries = summarise(run_evaluation(TUNING_SEEDS))

    def test_table_renders_every_storyline_and_bar(self) -> None:
        table = format_table(self.summaries)
        for name in ("balanced", "comeback", "siege", "end_to_end", "not scored", "premat.", "Bars:"):
            self.assertIn(name, table)
        self.assertEqual(len(check_bars(self.summaries)), 16)

    def test_every_goal_is_found(self) -> None:
        for s in self.summaries.values():
            self.assertEqual(s.goals_found, s.goals_total)


if __name__ == "__main__":
    unittest.main()
