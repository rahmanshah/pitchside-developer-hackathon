"""The stats tools: right numbers, typed results, and no way to see past the bound."""

from __future__ import annotations

import json
import unittest

from pitchside.stats import tools
from pitchside.stats.findings import FindingKind
from pitchside.stats.timeline import MatchTimeline, TimelineError, TimelineView

from _agents import match

EVENTS = match(1, "comeback").events
OTHER = match(2, "siege").events
TIMELINE = MatchTimeline(EVENTS)
MOMENTS = (150.0, 700.0, 1391.2, 2700.0, 3600.0, 4318.9, 5000.0, 6000.0)


def every_tool(view: TimelineView) -> dict[str, str]:
    """Run every tool (both scopes where there is one) and return the JSON each produced."""
    out = {}
    for name in ("possession", "field_tilt", "pressure", "passing", "shots_and_xg"):
        for scope in ("rolling", "since_kickoff"):
            out[f"{name}/{scope}"] = getattr(tools, name)(view, scope).model_dump_json()
    for name in ("match_totals", "recent_window", "momentum", "control_vs_chaos"):
        out[name] = getattr(tools, name)(view).model_dump_json()
    out["key_moments"] = tools.key_moments(view, limit=30).model_dump_json()
    for finding in view.findings:
        out[f"evidence/{finding.finding_id}"] = tools.evidence_lookup(view, finding.finding_id).model_dump_json()
    return out


def poisoned_timeline(t: float) -> MatchTimeline:
    prefix = [e for e in EVENTS if e.clock_s <= t]
    rest = [e.model_copy(update={"match_id": EVENTS[0].match_id, "event_id": prefix[-1].event_id + 1 + n,
                                 "clock_s": t + 1 + n * 0.1})
            for n, e in enumerate(x for x in OTHER if x.type in ("pass", "shot", "tackle"))][:400]
    return MatchTimeline(prefix + rest)


def timestamps(value, found=None):
    """Every number stored under a time-like key anywhere in a result."""
    found = [] if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if k.endswith("_s") and isinstance(v, (int, float)):
                found.append((k, v))
            timestamps(v, found)
    elif isinstance(value, list):
        for v in value:
            timestamps(v, found)
    return found


class NoFutureLeakageTests(unittest.TestCase):
    def test_no_tool_result_changes_when_everything_after_the_bound_is_replaced(self) -> None:
        for t in (700.0, 2700.0, 4318.9):
            poisoned = poisoned_timeline(t)
            for as_of in (t / 2, t):
                with self.subTest(t=t, as_of=as_of):
                    self.assertEqual(every_tool(poisoned.as_of(as_of)), every_tool(TIMELINE.as_of(as_of)))

    def test_no_time_in_any_result_is_after_as_of_s(self) -> None:
        for as_of in MOMENTS:
            for name, text in every_tool(TIMELINE.as_of(as_of)).items():
                data = json.loads(text)
                for key, value in timestamps(data):
                    if key == "as_of_s":
                        continue
                    with self.subTest(as_of=as_of, tool=name, key=key):
                        self.assertLessEqual(value, as_of)

    def test_key_moments_never_lists_a_finding_detected_after_as_of_s(self) -> None:
        everything = [f for f in TIMELINE.as_of(99999).findings]
        for as_of in MOMENTS:
            shown = tools.key_moments(TIMELINE.as_of(as_of), limit=30)
            expected = [f.finding_id for f in everything if f.detected_at_s <= as_of]
            self.assertEqual([m.finding_id for m in shown.moments], expected[-30:])
            self.assertEqual(shown.matching_so_far, len(expected))

    def test_evidence_for_a_future_finding_looks_exactly_like_evidence_for_an_unknown_one(self) -> None:
        late = TIMELINE.as_of(99999).findings[-1]
        view = TIMELINE.as_of(late.detected_at_s - 60)
        with self.assertRaises(TimelineError) as future:
            tools.evidence_lookup(view, late.finding_id)
        with self.assertRaises(TimelineError) as never:
            tools.evidence_lookup(view, late.finding_id.replace(":", ":x"))
        self.assertEqual(str(future.exception).replace(late.finding_id, "ID"),
                         str(never.exception).replace(late.finding_id.replace(":", ":x"), "ID"))
        self.assertEqual(tools.evidence_lookup(TIMELINE.as_of(late.detected_at_s), late.finding_id).finding.finding_id,
                         late.finding_id)


class NumbersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = TIMELINE.as_of(4400.0)
        self.snap = self.view.snapshot
        self.home, self.away = list(self.snap.teams)

    def values(self, result) -> dict[tuple[str, str | None], float]:
        return {(i.metric, i.team_id): i.value for i in result.items}

    def test_team_tools_report_the_engines_numbers_for_both_clubs(self) -> None:
        for team in (self.home, self.away):
            n, t = self.snap.teams[team], self.snap.totals[team]
            v = self.values(tools.field_tilt(self.view))
            self.assertEqual(v[("field_tilt", team)], round(n.field_tilt_pct, 3))
            self.assertEqual(self.values(tools.possession(self.view))[("possession", team)], round(n.possession_pct, 3))
            self.assertEqual(self.values(tools.pressure(self.view))[("pressures_per_min", team)],
                             round(n.pressures_per_min, 3))
            self.assertEqual(self.values(tools.shots_and_xg(self.view, "since_kickoff"))[("xg_total", team)],
                             round(t.xg, 3))
            self.assertEqual(self.values(tools.passing(self.view))[("pass_completion", team)],
                             round(n.pass_completion_pct, 3))
            self.assertIn(("pass_difficulty", team), self.values(tools.passing(self.view)))

    def test_momentum_is_a_mirror_image_and_chaos_is_match_wide(self) -> None:
        m = self.values(tools.momentum(self.view))
        self.assertEqual(m[("momentum", self.home)], -m[("momentum", self.away)])
        self.assertEqual(m[("momentum", self.home)], round(self.snap.momentum, 3))
        c = self.values(tools.control_vs_chaos(self.view))
        self.assertEqual(c[("chaos_index", None)], round(self.snap.chaos, 3))
        self.assertTrue(all(i.team_id is None for i in tools.control_vs_chaos(self.view).items))

    def test_totals_use_the_total_suffix_and_start_at_kickoff(self) -> None:
        result = tools.match_totals(self.view)
        self.assertEqual({i.metric for i in result.items},
                         {"possession_total", "shots_total", "xg_total", "pass_completion_total"})
        self.assertTrue(all(i.window_start_s == 0.0 and i.window_end_s == self.snap.clock_s for i in result.items))
        self.assertEqual(result.scope, "since_kickoff")
        self.assertEqual(result.score, self.snap.score)

    def test_evidence_lookup_returns_the_findings_own_numbers(self) -> None:
        goal = next(f for f in self.view.findings if f.kind is FindingKind.GOAL)
        result = tools.evidence_lookup(self.view, goal.finding_id)
        self.assertEqual([(i.metric, i.value, i.unit, i.baseline) for i in result.items],
                         [(e.metric, e.value, e.unit, e.baseline) for e in goal.evidence])
        self.assertEqual((result.finding.kind, result.finding.minute), (FindingKind.GOAL, int(goal.detected_at_s // 60) + 1))

    def test_key_moments_filters_and_limits(self) -> None:
        goals = tools.key_moments(self.view, [FindingKind.GOAL])
        self.assertTrue(goals.moments and all(m.kind is FindingKind.GOAL for m in goals.moments))
        strong = tools.key_moments(self.view, min_severity=0.9, limit=2)
        self.assertLessEqual(len(strong.moments), 2)
        self.assertTrue(all(m.severity >= 0.9 for m in strong.moments))
        for bad in (0, 31):
            with self.assertRaises(TimelineError):
                tools.key_moments(self.view, limit=bad)

    def test_local_tools_run_on_a_frozen_snapshot_and_match_the_timeline(self) -> None:
        local = TimelineView.from_snapshot(self.snap)
        self.assertEqual(tools.match_totals(local).items, tools.match_totals(self.view).items)
        self.assertEqual(tools.recent_window(local).items, tools.recent_window(self.view).items)
        self.assertEqual(local.findings, ())

    def test_every_result_is_typed_with_known_units(self) -> None:
        units = {"pct", "per_min", "xg", "index", "s", "count", "m"}
        for text in every_tool(self.view).values():
            data = json.loads(text)
            for item in data.get("items", []):
                self.assertIn(item["unit"], units)
                self.assertIsInstance(item["value"], float)


if __name__ == "__main__":
    unittest.main()
