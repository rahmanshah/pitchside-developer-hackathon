"""The timeline is where "no future leakage" is enforced, so it is tested against the engine itself."""

from __future__ import annotations

import math
import unittest

from pitchside.stats.engine import StatsEngine
from pitchside.stats.timeline import MatchStore, MatchTimeline, TimelineError

from _agents import match

EVENTS = match(1, "comeback").events
OTHER = match(2, "siege").events
TIMELINE = MatchTimeline(EVENTS)


def engine_at(events, clock_s: float, event_id=None):
    """Ground truth: feed a fresh engine only the events inside the bound."""
    engine, found = StatsEngine(), []
    for event in events:
        if event.clock_s > clock_s or (event_id is not None and event.event_id > event_id):
            break
        found.extend(engine.ingest(event))
    return engine.snapshot(), found


class AsOfTests(unittest.TestCase):
    def test_a_view_equals_what_a_fresh_engine_knew_at_that_moment(self) -> None:
        for clock in (0.0, 61.5, 600.0, 1391.2, 2700.0, 4318.9, 5230.2, 99999.0):
            snapshot, found = engine_at(EVENTS, clock)
            view = TIMELINE.as_of(clock)
            with self.subTest(clock=clock):
                self.assertEqual(view.snapshot, snapshot)
                self.assertEqual(list(view.findings), found)
                self.assertLessEqual(view.data_through_s, clock)
                self.assertEqual(view.as_of_s, clock)

    def test_the_bound_is_inclusive_of_the_event_at_exactly_that_time(self) -> None:
        goal = next(f for f in TIMELINE.as_of(99999).findings if f.kind.value == "goal")
        self.assertEqual(TIMELINE.as_of(goal.detected_at_s).data_through_s, goal.detected_at_s)
        before = TIMELINE.as_of(goal.detected_at_s - 0.05)
        self.assertNotIn(goal.finding_id, [f.finding_id for f in before.findings])
        self.assertIn(goal.finding_id, [f.finding_id for f in TIMELINE.as_of(goal.detected_at_s).findings])

    def test_event_id_ties_make_the_view_tighter_never_wider(self) -> None:
        # Find a finding detected on an event that shares its clock with a later event.
        engine, tied = StatsEngine(), None
        for index, event in enumerate(EVENTS):
            for finding in engine.ingest(event):
                later_same_clock = [e for e in EVENTS[index + 1:] if e.clock_s == event.clock_s]
                if later_same_clock and tied is None:
                    tied = (finding, event)
        self.assertIsNotNone(tied, "seed 1 comeback has one finding detected on a tied clock reading")
        finding, event = tied
        loose = TIMELINE.as_of(finding.detected_at_s)
        exact = TIMELINE.as_of(finding.detected_at_s, event.event_id)
        snapshot, _ = engine_at(EVENTS, finding.detected_at_s, event.event_id)
        self.assertEqual(exact.snapshot, snapshot)
        self.assertNotEqual(loose.snapshot, exact.snapshot)
        self.assertEqual(TIMELINE.cut(finding.detected_at_s, 10**9), TIMELINE.cut(finding.detected_at_s))
        self.assertLess(TIMELINE.cut(finding.detected_at_s, event.event_id), TIMELINE.cut(finding.detected_at_s))

    def test_nothing_after_the_bound_can_change_a_view(self) -> None:
        """Replace everything after T with another match's events: every view at or before T is identical."""
        for t in (300.0, 1391.2, 2700.0, 4000.0):
            prefix = [e for e in EVENTS if e.clock_s <= t]
            rest = [e.model_copy(update={"match_id": EVENTS[0].match_id, "event_id": prefix[-1].event_id + 1 + n,
                                         "clock_s": t + 1 + n * 0.1})
                    for n, e in enumerate(x for x in OTHER if x.type in ("pass", "shot", "tackle"))][:400]
            poisoned = MatchTimeline(prefix + rest)
            for clock in (0.0, t / 3, t / 2, t):
                with self.subTest(t=t, clock=clock):
                    self.assertEqual(poisoned.as_of(clock), TIMELINE.as_of(clock))

    def test_a_view_exposes_no_timestamp_after_the_bound(self) -> None:
        for clock in (300.0, 1500.0, 3000.0, 4400.0):
            view = TIMELINE.as_of(clock)
            self.assertLessEqual(view.snapshot.clock_s, clock)
            for team in (*view.snapshot.teams.values(), *view.snapshot.totals.values()):
                self.assertLessEqual(team.window_end_s, clock)
            self.assertLessEqual(view.snapshot.match.window_end_s, clock)
            for f in view.findings:
                self.assertLessEqual(f.detected_at_s, clock)
                self.assertLessEqual(f.end_s, clock)
                self.assertTrue(all(e.window_end_s <= clock for e in f.evidence))

    def test_a_spell_has_no_end_until_it_has_ended(self) -> None:
        full = TIMELINE.as_of(99999).findings
        end = next(f for f in full if f.status == "end")
        start = next(f for f in full if f.status == "start" and f.kind == end.kind and f.team_id == end.team_id)
        mid = TIMELINE.as_of((start.detected_at_s + end.detected_at_s) / 2).findings
        self.assertIn(start.finding_id, [f.finding_id for f in mid])
        self.assertNotIn(end.finding_id, [f.finding_id for f in mid])


class BoundValidationTests(unittest.TestCase):
    def test_bad_bounds_are_rejected(self) -> None:
        for bad in (-1.0, math.nan, math.inf, "10", None, True):
            with self.subTest(bad=bad), self.assertRaises(TimelineError):
                TIMELINE.as_of(bad)  # type: ignore[arg-type]
        for bad in (-1, 1.5, "3", True):
            with self.subTest(event_id=bad), self.assertRaises(TimelineError):
                TIMELINE.as_of(100.0, bad)  # type: ignore[arg-type]

    def test_a_timeline_needs_an_ordered_match(self) -> None:
        with self.assertRaises(TimelineError):
            MatchTimeline([])
        with self.assertRaises(TimelineError):
            MatchTimeline(EVENTS[1:])
        with self.assertRaises(TimelineError):
            MatchTimeline([*EVENTS[:3], EVENTS[1]])


class StoreTests(unittest.TestCase):
    def test_single_match_needs_no_id_several_do(self) -> None:
        store = MatchStore()
        with self.assertRaises(TimelineError):
            store.get()
        store.add(EVENTS)
        self.assertEqual(store.get().match_id, EVENTS[0].match_id)
        store.add(OTHER)
        self.assertEqual(store.ids(), sorted({EVENTS[0].match_id, OTHER[0].match_id}))
        with self.assertRaises(TimelineError) as ctx:
            store.get()
        self.assertIn("match_id is required", str(ctx.exception))
        with self.assertRaises(TimelineError):
            store.get("nope")


if __name__ == "__main__":
    unittest.main()
