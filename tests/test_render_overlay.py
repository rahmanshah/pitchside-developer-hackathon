"""The overlay contract: replay timeline, lower-third resolution, texts, honest source info, audit badges, schema."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema

from pitchside.agents.bundle import load_bundle
from pitchside.render.overlay import (
    BREAK_S, END_S, MAX_DELAY_S, OVERLAY_FILE, OverlayDocument, _Draft, dump_overlay, overlay_from_dir,
    overlay_json_schema, resolve_lower_third,
)

from _render import bundle_dir

ROOT = Path(__file__).resolve().parents[1]


def lower(doc: OverlayDocument) -> list[tuple[float, float, str]]:
    return sorted((p.show_at_s, p.hide_at_s, o.overlay_id) for o in doc.overlays for p in o.placements
                  if p.anchor == "lower_third")


class TimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = overlay_from_dir(bundle_dir())

    def test_segments_add_a_half_time_break_and_a_full_time_hold(self) -> None:
        segs = [(s.kind, s.period, s.start_s, s.end_s, s.clock_start_s, s.clock_end_s)
                for s in self.doc.timeline.segments]
        self.assertEqual(segs, [("play", 1, 0.0, 2700.0, 0.0, 2700.0),
                                ("break", 1, 2700.0, 2700.0 + BREAK_S, 2700.0, 2700.0),
                                ("play", 2, 2700.0 + BREAK_S, 5400.0 + BREAK_S, 2700.0, 5400.0),
                                ("end", 2, 5400.0 + BREAK_S, 5400.0 + BREAK_S + END_S, 5400.0, 5400.0)])
        self.assertEqual(self.doc.timeline.duration_s, 5400.0 + BREAK_S + END_S)

    def test_match_clock_maps_to_the_timeline_by_period(self) -> None:
        tl = self.doc.timeline
        self.assertEqual(tl.to_timeline(600.0), 600.0)
        self.assertEqual(tl.to_timeline(2700.0, 1), 2700.0)
        self.assertEqual(tl.to_timeline(2700.0, 2), 2700.0 + BREAK_S)
        self.assertEqual(tl.to_timeline(2700.0), 2700.0)  # a boundary clock belongs to the earlier period
        self.assertEqual(tl.to_timeline(3000.0), 3000.0 + BREAK_S)

    def test_recaps_play_in_the_break_and_after_the_final_whistle(self) -> None:
        recaps = {o.kind: o for o in self.doc.overlays if o.status == "recap"}
        ht, ft = recaps["recap_half_time"], recaps["recap_full_time"]
        self.assertEqual((ht.show_at_s, ht.hide_at_s), (2700.0, 2700.0 + BREAK_S))
        self.assertEqual((ft.show_at_s, ft.hide_at_s), (5400.0 + BREAK_S, self.doc.timeline.duration_s))
        self.assertEqual((ht.clock_s, ft.clock_s), (2700.0, 5400.0))

    def test_second_half_moments_are_shifted_by_the_break(self) -> None:
        for o in self.doc.overlays:
            if o.status != "recap":
                expected = o.clock_s + (BREAK_S if o.period == 2 else 0.0)
                self.assertAlmostEqual(o.show_at_s, expected, places=3, msg=o.overlay_id)


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = bundle_dir()
        cls.bundle = load_bundle(cls.path)
        cls.doc = overlay_from_dir(cls.path)
        cls.faulted = overlay_from_dir(bundle_dir(faults=True))

    def test_one_overlay_per_narrated_job_in_show_order(self) -> None:
        self.assertEqual({o.job_id for o in self.doc.overlays}, {n.job_id for n in self.bundle.narratives})
        shows = [o.show_at_s for o in self.doc.overlays]
        self.assertEqual(shows, sorted(shows))

    def test_lower_third_windows_never_overlap_and_stay_inside_the_overlay(self) -> None:
        for doc in (self.doc, self.faulted):
            windows = lower(doc)
            for a, b in zip(windows, windows[1:]):
                self.assertLessEqual(a[1], b[0], f"{a} overlaps {b}")
            for o in doc.overlays:
                for p in o.placements:
                    if p.anchor == "lower_third":
                        self.assertGreaterEqual(p.show_at_s, o.show_at_s)
                        self.assertLessEqual(p.hide_at_s, o.hide_at_s)
                        self.assertLessEqual(p.show_at_s - o.show_at_s, MAX_DELAY_S + 1e-9)

    def test_context_findings_only_reach_the_side_panel(self) -> None:
        for o in self.doc.overlays:
            if o.status == "end" or o.kind == "counter_attack":
                self.assertEqual(o.anchor, "side_panel", o.overlay_id)
                self.assertNotIn("lower_third", [p.anchor for p in o.placements])
                self.assertEqual(o.hold_ms, 0)

    def test_every_overlay_reaches_the_screen_within_the_delay_limit(self) -> None:
        for o in self.doc.overlays:
            first = min(p.show_at_s for p in o.placements if p.anchor != "pitch_highlight")
            self.assertLessEqual(first - o.show_at_s, MAX_DELAY_S + 1e-9, o.overlay_id)

    def test_goals_win_the_lower_third_and_hold_the_replay(self) -> None:
        goals = [o for o in self.doc.overlays if o.kind == "goal"]
        self.assertTrue(goals)
        for g in goals:
            self.assertEqual(g.priority, 1.0)
            self.assertIn(("lower_third", g.show_at_s), [(p.anchor, p.show_at_s) for p in g.placements])
            self.assertGreater(g.hold_ms, 0)
            self.assertEqual(g.highlight.shape, "arrow")

    def test_every_persona_in_every_language(self) -> None:
        personas = [p.persona for p in self.doc.personas]
        self.assertEqual(personas, self.bundle.manifest.personas)
        self.assertEqual([p.kind for p in self.doc.personas], ["analyst", "casual", "favourite", "favourite"])
        for o in self.doc.overlays:
            self.assertEqual(set(o.texts), set(personas), o.overlay_id)
            for by_lang in o.texts.values():
                self.assertEqual(set(by_lang), {"en", "es"})

    def test_source_is_copied_from_the_manifest(self) -> None:
        m = self.bundle.manifest
        s = self.doc.source
        self.assertEqual((s.llm_mode, s.label, s.faults_injected, s.faults_fired),
                         (m.pipeline.llm_mode, m.label, False, 0))
        self.assertTrue(self.faulted.source.faults_injected)
        self.assertGreater(self.faulted.source.faults_fired, 0)

    def test_faults_and_badges_are_attached_to_their_overlays(self) -> None:
        fired = sum(f.count for o in self.faulted.overlays for f in o.audit.faults)
        self.assertEqual(fired, self.faulted.source.faults_fired)
        badges = {b for o in self.faulted.overlays for b in o.audit.badges}
        self.assertTrue({"template", "template_fallback", "semantic_check_skipped", "retried", "persona_template",
                         "fault_injected"} <= badges, badges)
        self.assertFalse(any("fault_injected" in o.audit.badges for o in self.doc.overlays))

    def test_rejected_drafts_keep_their_text_and_reasons(self) -> None:
        rejected = [r for o in self.faulted.overlays for r in o.audit.rejected]
        self.assertTrue(rejected)
        for r in rejected:
            self.assertTrue(r.text and r.reasons, r)

    def test_audit_steps_and_calls_match_the_bundle(self) -> None:
        for o in self.doc.overlays:
            steps = [a for a in self.bundle.audit if a.job_id == o.job_id]
            self.assertEqual([s.seq for s in o.audit.steps], sorted(a.seq for a in steps))
            calls = [u for u in self.bundle.usage if u.finding_id == o.job_id and u.status != "refused"]
            self.assertEqual(o.audit.calls.calls, len(calls))

    def test_teams_carry_generated_identities(self) -> None:
        home, away = self.doc.teams
        self.assertEqual((home.side, away.side), ("home", "away"))
        self.assertNotEqual(home.colours.primary, away.colours.primary)


class ResolveTests(unittest.TestCase):
    """The lower-third rules on hand-made overlays."""

    def run_rules(self, *items: tuple[str, float, float, float, str]) -> dict[str, _Draft]:
        drafts = [_Draft(key, show, hide, priority, anchor) for key, show, hide, priority, anchor in items]
        resolve_lower_third(drafts)
        return {d.key: d for d in drafts}

    def test_a_free_lower_third_is_used_as_is(self) -> None:
        d = self.run_rules(("a", 10, 25, 0.6, "lower_third"), ("b", 30, 40, 0.6, "lower_third"))
        self.assertEqual((d["a"].lower, d["b"].lower), ((10, 25), (30, 40)))

    def test_a_more_important_overlay_cuts_the_current_one_short(self) -> None:
        d = self.run_rules(("chance", 10, 25, 0.7, "lower_third"), ("goal", 15, 40, 1.0, "lower_third"))
        self.assertEqual((d["chance"].lower, d["goal"].lower), ((10, 15), (15, 40)))
        self.assertIn("cut short by goal", d["chance"].note)

    def test_an_equal_overlay_waits_up_to_the_limit(self) -> None:
        d = self.run_rules(("a", 10, 25, 0.7, "lower_third"), ("b", 20, 40, 0.7, "lower_third"))
        self.assertEqual(d["b"].lower, (25, 40))
        self.assertIn("delayed 5.0 s", d["b"].note)

    def test_beyond_the_limit_it_goes_to_the_side_panel(self) -> None:
        d = self.run_rules(("a", 10, 40, 0.7, "lower_third"), ("b", 20, 45, 0.6, "lower_third"))
        self.assertIsNone(d["b"].lower)
        self.assertIn("side panel only", d["b"].note)

    def test_a_waiting_overlay_yields_to_a_more_important_one_without_overlap(self) -> None:
        d = self.run_rules(("a", 0, 25, 0.9, "lower_third"), ("b", 20, 40, 0.6, "lower_third"),
                           ("c", 22, 45, 0.8, "lower_third"))
        self.assertIsNone(d["b"].lower)
        self.assertIn("pre-empted by c", d["b"].note)
        self.assertEqual(d["a"].lower, (0, 25))
        self.assertEqual(d["c"].lower, (25, 45))

    def test_side_panel_overlays_are_left_alone(self) -> None:
        d = self.run_rules(("end", 10, 20, 0.3, "side_panel"))
        self.assertIsNone(d["end"].lower)


class SchemaTests(unittest.TestCase):
    def test_overlay_schema_doc_is_current(self) -> None:
        on_disk = json.loads((ROOT / "docs" / "overlay.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, json.loads(json.dumps(overlay_json_schema())),
                         "run `uv run pitchside schema --overlay > docs/overlay.schema.json`")

    def test_built_documents_validate_against_the_published_schema(self) -> None:
        schema = json.loads((ROOT / "docs" / "overlay.schema.json").read_text(encoding="utf-8"))
        for path in (bundle_dir(), bundle_dir(faults=True)):
            jsonschema.validate(json.loads((path / OVERLAY_FILE).read_text(encoding="utf-8")), schema)

    def test_the_schema_rejects_a_broken_document(self) -> None:
        schema = json.loads((ROOT / "docs" / "overlay.schema.json").read_text(encoding="utf-8"))
        doc = json.loads((bundle_dir() / OVERLAY_FILE).read_text(encoding="utf-8"))
        del doc["overlays"][0]["show_at_s"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_write_bundle_writes_the_same_overlay_the_builder_makes(self) -> None:
        path = bundle_dir()
        self.assertEqual((path / OVERLAY_FILE).read_text(encoding="utf-8"), dump_overlay(overlay_from_dir(path)))


if __name__ == "__main__":
    unittest.main()
