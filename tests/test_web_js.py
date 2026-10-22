"""The web UI's pure replay modules (web/js/replay.js, timeline.js, honesty.js), run in V8 via mini-racer.

Covers the clock (play, pause, speed changes, holds, seeking back and forth), the replay timeline (cross-checked
with the Python builder), time -> active overlays, score and stats at any time (cross-checked with the events),
and the honesty labels. No browser and no Node: the modules have no imports and no DOM.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from py_mini_racer import MiniRacer

from pitchside.jsonl import read_events
from pitchside.render.overlay import OverlayDocument

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
RUN = WEB / "data" / "comeback"
PURE = ("replay.js", "timeline.js", "honesty.js")


def load_modules() -> MiniRacer:
    ctx = MiniRacer()
    for name in PURE:
        source = (WEB / "js" / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*import\s", source, re.M), f"{name} must stay import-free"
        assert "document." not in source and "window." not in source, f"{name} must not touch the DOM"
        ctx.eval(re.sub(r"^export\s+", "", source, flags=re.M))
    return ctx


class JsCase(unittest.TestCase):
    ctx: MiniRacer

    @classmethod
    def setUpClass(cls) -> None:
        cls.ctx = load_modules()

    def js(self, expr: str):
        return json.loads(self.ctx.eval(f"JSON.stringify({expr})"))

    def run_js(self, code: str) -> None:
        self.ctx.eval(code)


class ClockTests(JsCase):
    def test_play_pause_speed_and_seek(self) -> None:
        self.run_js("var c = new ReplayClock({duration: 100, speed: 1});")
        self.assertEqual(self.js("c.time(5000)"), 0)
        self.run_js("c.play(1000);")
        self.assertAlmostEqual(self.js("c.time(3000)"), 2.0)
        self.run_js("c.setSpeed(4, 3000);")  # no jump: carries on from 2.0 at 4x
        self.assertAlmostEqual(self.js("c.time(3000)"), 2.0)
        self.assertAlmostEqual(self.js("c.time(4000)"), 6.0)
        self.run_js("c.pause(4500);")
        self.assertAlmostEqual(self.js("c.time(99999)"), 8.0)
        self.run_js("c.seek(50, 10000);")
        self.assertAlmostEqual(self.js("c.time(12000)"), 50.0)
        self.run_js("c.seek(10, 12000); c.play(20000);")
        self.assertAlmostEqual(self.js("c.time(21000)"), 14.0)
        self.run_js("c.seek(3, 21000);")  # seeking back while playing
        self.assertAlmostEqual(self.js("c.time(22000)"), 7.0)

    def test_the_clock_stops_at_the_end_and_play_restarts(self) -> None:
        self.run_js("var e = new ReplayClock({duration: 100, speed: 10}); e.play(0);")
        self.assertEqual(self.js("e.time(60000)"), 100)
        self.assertTrue(self.js("e.ended(60000)"))
        self.run_js("e.play(61000);")
        self.assertAlmostEqual(self.js("e.time(62000)"), 10.0)

    def test_seeks_are_clamped(self) -> None:
        self.run_js("var s = new ReplayClock({duration: 100}); s.seek(-5, 0);")
        self.assertEqual(self.js("s.time(0)"), 0)
        self.run_js("s.seek(500, 0);")
        self.assertEqual(self.js("s.time(0)"), 100)

    def test_a_fast_replay_holds_at_a_lower_third(self) -> None:
        self.run_js("var h = new ReplayClock({duration: 100, speed: 4, holds: [{at: 10, ms: 2000}]}); h.play(0);")
        self.assertAlmostEqual(self.js("h.time(2000)"), 8.0)
        self.assertAlmostEqual(self.js("h.time(2500)"), 10.0)  # reaches the hold
        self.assertAlmostEqual(self.js("h.time(4000)"), 10.0)
        self.assertTrue(self.js("h.holding(4000)"))
        self.assertAlmostEqual(self.js("h.time(5500)"), 14.0)  # 2 s held, then 1 s at 4x
        self.assertFalse(self.js("h.holding(5500)"))

    def test_holds_apply_only_faster_than_real_time_and_not_at_the_seek_point(self) -> None:
        self.run_js("var r = new ReplayClock({duration: 100, speed: 1, holds: [{at: 10, ms: 2000}]}); r.play(0);")
        self.assertAlmostEqual(self.js("r.time(11000)"), 11.0)
        self.run_js("r.setSpeed(4, 11000); r.seek(10, 11000);")
        self.assertAlmostEqual(self.js("r.time(12000)"), 14.0)  # no hold at the point it was seeked to
        self.run_js("r.seek(5, 12000);")  # scrub back before it: the hold applies again
        self.assertAlmostEqual(self.js("r.time(14000)"), 10.0)

    def test_holds_can_be_switched_off_and_a_speed_change_ends_one(self) -> None:
        self.run_js("var o = new ReplayClock({duration: 100, speed: 4, holds: [{at: 10, ms: 2000}],"
                    " holdsEnabled: false}); o.play(0);")
        self.assertAlmostEqual(self.js("o.time(3000)"), 12.0)
        self.run_js("var p = new ReplayClock({duration: 100, speed: 4, holds: [{at: 10, ms: 2000}]}); p.play(0);"
                    " p.setSpeed(8, 3000);")
        self.assertAlmostEqual(self.js("p.time(4000)"), 18.0)


class TimelineTests(JsCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.doc = OverlayDocument.model_validate_json((RUN / "overlay.json").read_text(encoding="utf-8"))
        cls.ctx.eval(f"var TL = makeTimeline({json.dumps(cls.doc.timeline.model_dump())});")

    def test_clock_to_timeline_matches_the_python_builder(self) -> None:
        for clock, period in ((0, None), (100.5, None), (2700, 1), (2700, 2), (2700, None), (3600, None),
                              (5400, 2), (4285.4, 2)):
            arg = "undefined" if period is None else period
            self.assertAlmostEqual(self.js(f"TL.toTimeline({clock}, {arg})"),
                                   self.doc.timeline.to_timeline(clock, period), places=6, msg=(clock, period))

    def test_timeline_to_clock_freezes_in_the_break_and_after_the_whistle(self) -> None:
        self.assertEqual(self.js("clockLabel(TL.at(0))"), "00:00")
        self.assertEqual(self.js("clockLabel(TL.at(2699.5))"), "44:59")
        self.assertEqual(self.js("[TL.at(2720).phase, TL.at(2720).clock, clockLabel(TL.at(2720))]"),
                         ["break", 2700, "HT"])
        self.assertEqual(self.js("[TL.at(2805).period, TL.at(2805).clock, clockLabel(TL.at(2805))]"),
                         [2, 2760, "46:00"])
        self.assertEqual(self.js(f"clockLabel(TL.at({self.doc.timeline.duration_s}))"), "FT")
        self.assertEqual(self.js("TL.at(-5).clock"), 0)
        self.assertEqual(self.js("minuteLabel(4285.4)"), "72'")

    def test_url_times_accept_seconds_or_match_clock(self) -> None:
        self.assertAlmostEqual(self.js("parseTime('63:20', TL)"), 63 * 60 + 20 + 45)
        self.assertAlmostEqual(self.js("parseTime('45:00', TL)"), 2700)
        self.assertAlmostEqual(self.js("parseTime('45:00@2', TL)"), 2745)
        self.assertAlmostEqual(self.js("parseTime('3800', TL)"), 3800)
        self.assertEqual(self.js("parseTime('99999', TL)"), self.doc.timeline.duration_s)
        self.assertIsNone(self.js("parseTime('soon', TL)"))
        self.assertIsNone(self.js("parseTime('', TL)"))


class OverlayTests(JsCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.doc = OverlayDocument.model_validate_json((RUN / "overlay.json").read_text(encoding="utf-8"))
        cls.ctx.eval(f"var OV = {json.dumps(cls.doc.model_dump(mode='json')['overlays'])};")

    def expected_lower(self, t: float):
        hits = [o for o in self.doc.overlays for p in o.placements
                if p.anchor == "lower_third" and p.show_at_s <= t < p.hide_at_s]
        return hits[0].overlay_id if hits else None

    def lower_at(self, t: float):
        return self.js(f"(lowerThirdAt(OV, {t}) || {{overlay: {{overlay_id: null}}}}).overlay.overlay_id")

    def test_show_and_hide_boundaries(self) -> None:
        self.run_js("var SYN = ["
                    "{overlay_id: 'a', priority: 0.5, show_at_s: 10, placements: ["
                    "{anchor: 'lower_third', show_at_s: 10, hide_at_s: 20},"
                    "{anchor: 'side_panel', show_at_s: 20, hide_at_s: 100}]},"
                    "{overlay_id: 'b', priority: 0.9, show_at_s: 30, placements: ["
                    "{anchor: 'side_panel', show_at_s: 30, hide_at_s: 100}]}];")
        ids = "activePlacements(SYN, {t}, '{anchor}').map(x => x.overlay.overlay_id)"
        self.assertEqual(self.js(ids.format(t=9.99, anchor="lower_third")), [])
        self.assertEqual(self.js(ids.format(t=10, anchor="lower_third")), ["a"])
        self.assertEqual(self.js(ids.format(t=19.99, anchor="lower_third")), ["a"])
        self.assertEqual(self.js(ids.format(t=20, anchor="lower_third")), [])
        self.assertEqual(self.js(ids.format(t=20, anchor="side_panel")), ["a"])
        self.assertEqual(self.js(ids.format(t=40, anchor="side_panel")), ["b", "a"])  # priority first
        self.assertEqual(self.js("latestStarted(SYN, 35).overlay_id"), "b")
        self.assertIsNone(self.js("latestStarted(SYN, 5)"))

    def test_every_lower_third_is_shown_at_its_time_and_never_two_at_once(self) -> None:
        for o in self.doc.overlays:
            for p in o.placements:
                if p.anchor == "lower_third":
                    self.assertEqual(self.lower_at((p.show_at_s + p.hide_at_s) / 2), o.overlay_id)
        counts = self.js("Array.from({length: 1100}, (_, i) => activePlacements(OV, i * 5, 'lower_third').length)")
        self.assertLessEqual(max(counts), 1)

    def test_scrubbing_back_and_forth_matches_the_contract(self) -> None:
        duration = self.doc.timeline.duration_s
        times = [5000, 100, 3000, 2722, 4300.2, 0, duration, 4290, 4285.4, 2700, 1200]
        for t in times + times[::-1]:
            self.assertEqual(self.lower_at(t), self.expected_lower(t), t)


class StateTests(JsCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.doc = OverlayDocument.model_validate_json((RUN / "overlay.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (RUN / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        stats = [json.loads(line) for line in (RUN / "stats.jsonl").read_text(encoding="utf-8").splitlines() if line]
        cls.manifest = json.loads((RUN / "manifest.json").read_text(encoding="utf-8"))
        cls.ctx.eval(f"var TL = makeTimeline({json.dumps(cls.doc.timeline.model_dump())});"
                     f"var EV = indexEvents({json.dumps(events)}, TL);"
                     f"var SN = indexSnapshots({json.dumps(stats)}, TL);")
        tl = cls.doc.timeline
        cls.goals = sorted((tl.to_timeline(e.clock_s, e.period), e.score_after.home, e.score_after.away)
                           for e in read_events(RUN / "events.jsonl") if e.type == "shot" and e.outcome == "goal")
        cls.snap_times = sorted(tl.to_timeline(s["clock_s"]) for s in stats)

    def expected_score(self, t: float) -> dict:
        done = [g for g in self.goals if g[0] <= t]
        return {"home": done[-1][1], "away": done[-1][2]} if done else {"home": 0, "away": 0}

    def test_score_at_any_time_in_any_order(self) -> None:
        self.assertGreaterEqual(len(self.goals), 2)
        times = [0.0, self.doc.timeline.duration_s]
        for t, _, _ in self.goals:
            times += [t - 0.01, t, t + 30]
        for t in times + sorted(times, reverse=True) + times[::3]:
            self.assertEqual(self.js(f"scoreAt(EV, {t})"), self.expected_score(t), t)
        self.assertEqual(self.js(f"scoreAt(EV, {self.doc.timeline.duration_s})"), self.manifest["final_score"])

    def test_stats_snapshot_is_the_latest_at_or_before_t(self) -> None:
        for t in (0.0, 29.9, 30.0, 2700.0, 2744.0, 2775.0, 5000.0, self.doc.timeline.duration_s, 600.0):
            done = [s for s in self.snap_times if s <= t]
            got = self.js(f"(snapshotAt(SN, {t}) || {{t: null}}).t")
            self.assertEqual(got, done[-1] if done else None, t)

    def test_events_between_and_ball_position(self) -> None:
        recent = self.js("eventsBetween(EV, 600, 720).map(e => e.t)")
        self.assertTrue(recent and all(600 < t <= 720 for t in recent))
        self.assertEqual(recent, sorted(recent))
        for t in (0, 1234.5, 2722, 4285.4, 5490):
            ball = self.js(f"ballAt(EV, {t})")
            self.assertTrue(0 <= ball["x"] <= 105 and 0 <= ball["y"] <= 68, (t, ball))
        shot = self.js("EV.goals[0]")
        self.assertEqual(self.js(f"[ballAt(EV, {shot['t']}).x, ballAt(EV, {shot['t']}).y]"),
                         [shot["location"]["x"], shot["location"]["y"]])


class HonestyTests(JsCase):
    def badge(self, manifest: dict, source: dict | None = None) -> dict:
        return self.js(f"honesty({json.dumps(manifest)}, {json.dumps(source)})")

    def test_badges_follow_the_manifest_mode(self) -> None:
        live = {"label": "L", "pipeline": {"llm_mode": "foundry"}, "faults": {"injected": False, "fired": []}}
        fake = {"label": "F", "pipeline": {"llm_mode": "fake"}, "faults": {"injected": False, "fired": []}}
        self.assertEqual(self.badge(live)["badge"], "Recorded run generated by the live pipeline")
        self.assertEqual(self.badge(fake)["badge"], "Offline demo (fake model client)")
        self.assertFalse(self.badge(live)["costIsEstimate"])
        self.assertTrue(self.badge(fake)["costIsEstimate"])
        self.assertEqual(self.badge(fake)["label"], "F")

    def test_unknown_or_inconsistent_runs_are_never_called_live(self) -> None:
        for manifest in ({}, {"pipeline": {"llm_mode": "live"}}, {"pipeline": {}}):
            self.assertEqual(self.badge(manifest)["badge"], "Unverified run: model mode unknown")
        live = {"label": "L", "pipeline": {"llm_mode": "foundry"}}
        self.assertEqual(self.badge(live, {"llm_mode": "fake", "label": "L"})["mode"], "unknown")
        self.assertEqual(self.badge(live, {"llm_mode": "foundry", "label": "L"})["mode"], "foundry")

    def test_source_labels_never_call_fake_text_model_output(self) -> None:
        roles = ["interpreter", "explainer", "verifier", "personalizer"]
        fake = json.loads((WEB / "data" / "comeback" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(self.badge(fake)["sourceLabels"], dict.fromkeys(roles, "Offline stand-in"))
        live = {"label": "L", "pipeline": {"llm_mode": "foundry",
                                           "deployments": {"explainer": "luna-eu", "personalizer": "luna-eu-2"}}}
        labels = self.badge(live)["sourceLabels"]
        self.assertEqual((labels["explainer"], labels["personalizer"]), ("luna-eu", "luna-eu-2"))
        self.assertEqual(labels["verifier"], "unnamed deployment")
        self.assertEqual(set(self.badge({})["sourceLabels"].values()), {"Unverified source"})
        for labels in (self.badge(fake)["sourceLabels"], self.badge({})["sourceLabels"]):
            self.assertFalse(any("model" == v.lower() for v in labels.values()))

    def test_injected_faults_are_counted(self) -> None:
        manifest = json.loads((WEB / "data" / "faults-demo" / "manifest.json").read_text(encoding="utf-8"))
        h = self.badge(manifest)
        self.assertTrue(h["faultsInjected"])
        self.assertEqual(h["faultsFired"], len(manifest["faults"]["fired"]))
        self.assertEqual(h["faultPlan"], "demo")


if __name__ == "__main__":
    unittest.main()
