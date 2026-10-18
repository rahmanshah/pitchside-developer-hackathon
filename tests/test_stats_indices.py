"""Tests for the momentum and control-vs-chaos indices."""

from __future__ import annotations

import unittest

from pitchside.stats.indices import chaos, chaos_parts, momentum
from pitchside.stats.metrics import MatchNumbers, TeamNumbers


def _team(team_id: str, final_third: int = 5, shots: int = 1, xg: float = 0.1, press: float = 1.0) -> TeamNumbers:
    return TeamNumbers(
        team_id=team_id, window_start_s=0, window_end_s=300, possession_pct=50, passes=20,
        pass_completion_pct=84, pass_difficulty=0.4, final_third_passes=final_third, field_tilt_pct=50,
        shots=shots, xg=xg, goals=0, pressures=int(press * 5), pressures_per_min=press, tackles_won=1,
    )


def _match(turnovers: float = 1.8, completion: float | None = 84, spell: float | None = 32) -> MatchNumbers:
    return MatchNumbers(
        window_start_s=0, window_end_s=300, passes=40, pass_completion_pct=completion,
        turnovers=int(turnovers * 5), turnovers_per_min=turnovers, mean_spell_s=spell,
    )


class MomentumTests(unittest.TestCase):
    def test_even_contest_is_zero(self) -> None:
        self.assertEqual(momentum(_team("h"), _team("a")), 0.0)

    def test_dominance_is_positive_and_symmetric(self) -> None:
        strong = _team("h", final_third=14, shots=3, xg=0.6, press=4.0)
        weak = _team("a", final_third=2, shots=0, xg=0.0, press=0.2)
        value = momentum(strong, weak)
        self.assertGreater(value, 0.5)
        self.assertLessEqual(value, 1.0)
        self.assertEqual(momentum(weak, strong), -value)

    def test_one_shot_in_a_quiet_window_is_not_extreme(self) -> None:
        quiet_h = _team("h", final_third=0, shots=1, xg=0.05, press=0)
        quiet_a = _team("a", final_third=0, shots=0, xg=0.0, press=0)
        self.assertLess(momentum(quiet_h, quiet_a), 0.25)


class ChaosTests(unittest.TestCase):
    def test_typical_play_is_zero(self) -> None:
        self.assertEqual(chaos(_match()), 0.0)

    def test_broken_play_is_chaotic(self) -> None:
        self.assertGreaterEqual(chaos(_match(turnovers=3.8, completion=68, spell=15)), 0.9)

    def test_controlled_play_is_negative(self) -> None:
        self.assertLess(chaos(_match(turnovers=1.0, completion=92, spell=45)), -0.4)

    def test_components_are_bounded_and_missing_data_is_neutral(self) -> None:
        parts = chaos_parts(_match(turnovers=20, completion=None, spell=None))
        self.assertEqual(parts, {"turnovers": 1.0, "completion": 0.0, "spell_length": 0.0})


if __name__ == "__main__":
    unittest.main()
