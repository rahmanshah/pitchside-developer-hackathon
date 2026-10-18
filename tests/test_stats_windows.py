"""Tests for rolling windows and the possession clock."""

from __future__ import annotations

import unittest

from pitchside.stats.windows import PossessionClock, RollingWindow


class RollingWindowTests(unittest.TestCase):
    def test_sums_only_recent_samples(self) -> None:
        w = RollingWindow(60)
        w.add(10, 0.2)
        w.add(50, 0.3)
        self.assertAlmostEqual(w.sum, 0.5)
        w.advance(70)  # sample at 10 is now exactly span old -> out
        self.assertAlmostEqual(w.sum, 0.3)
        self.assertEqual(w.count, 1)
        w.advance(200)
        self.assertEqual((w.sum, w.count, w.mean), (0.0, 0, None))

    def test_mean_and_coverage(self) -> None:
        w = RollingWindow(300)
        for t, v in ((5, 0.5), (6, 0.7), (7, 0.9)):
            w.add(t, v)
        self.assertAlmostEqual(w.mean, 0.7)
        self.assertEqual(w.covered_s(), 7)  # match is 7 s old: window not full yet
        w.advance(1000)
        self.assertEqual(w.covered_s(), 300)
        self.assertEqual(w.covered_s(start_s=900), 100)

    def test_rejects_time_travel_and_bad_span(self) -> None:
        w = RollingWindow(60)
        w.add(30)
        with self.assertRaises(ValueError):
            w.add(20)
        with self.assertRaises(ValueError):
            RollingWindow(0)

    def test_no_float_drift_after_emptying(self) -> None:
        w = RollingWindow(1)
        for i in range(1000):
            w.add(i * 2, 0.1)
        w.advance(10_000)
        self.assertEqual(w.sum, 0.0)


class PossessionClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = PossessionClock()
        self.clock.switch("home", 0)
        self.clock.switch("away", 60)
        self.clock.switch("home", 90)

    def test_window_overlap(self) -> None:
        self.assertEqual(self.clock.seconds("home", 0, 120), 90)
        self.assertEqual(self.clock.seconds("away", 0, 120), 30)
        self.assertEqual(self.clock.seconds("away", 70, 120), 20)
        self.assertEqual(self.clock.seconds("home", 100, 120), 20)  # open spell

    def test_cumulative_totals(self) -> None:
        self.assertEqual(self.clock.total("home", 100), 70)
        self.assertEqual(self.clock.total("away", 100), 30)
        self.assertEqual(self.clock.team, "home")
        self.assertEqual(self.clock.since_s, 90)

    def test_old_spells_are_pruned(self) -> None:
        clock = PossessionClock(keep_s=100)
        clock.switch("home", 0)
        clock.switch("away", 50)
        clock.switch("home", 400)
        self.assertEqual(clock.seconds("home", 300, 450), 50)
        self.assertEqual(clock.total("home", 450), 100)  # totals survive pruning


if __name__ == "__main__":
    unittest.main()
