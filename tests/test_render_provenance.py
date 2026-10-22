"""The evidence chain: claims -> ledger numbers -> events, with triggering and in-window events kept apart."""

from __future__ import annotations

import unittest

from pitchside.agents.bundle import load_bundle
from pitchside.agents.contracts import Claim, ClaimNumber
from pitchside.jsonl import read_events
from pitchside.render.overlay import overlay_from_dir
from pitchside.render.provenance import MAX_EVENT_IDS, METRIC_EVENTS, number_spans

from _render import bundle_dir


class ChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = bundle_dir()
        cls.doc = overlay_from_dir(cls.path)
        cls.events = {e.event_id: e for e in read_events(cls.path / "events.jsonl")}
        cls.findings = {f.finding_id: f for f in load_bundle(cls.path).findings}

    def links(self):
        for o in self.doc.overlays:
            for claim in o.provenance.claims:
                for ev in claim.evidence:
                    yield o, claim, ev

    def test_every_shown_claim_resolves_to_known_evidence(self) -> None:
        for o in self.doc.overlays:
            self.assertTrue(o.provenance.claims, o.overlay_id)
            for claim in o.provenance.claims:
                self.assertTrue(claim.evidence, claim.claim_id)
        for _, claim, ev in self.links():
            self.assertTrue(ev.known, f"{claim.claim_id}: {ev.ref}")

    def test_triggering_events_are_the_findings_own(self) -> None:
        seen = 0
        for o, _, ev in self.links():
            for link in ev.events:
                if link.relation == "triggering":
                    seen += 1
                    self.assertEqual(ev.source, "finding")
                    self.assertEqual(link.event_ids, self.findings[o.finding_id].event_ids)
                    self.assertIsNone(link.filter)
        self.assertGreater(seen, 0)

    def test_in_window_events_match_their_filter(self) -> None:
        for _, _, ev in self.links():
            for link in ev.events:
                if link.relation == "triggering":
                    continue
                flt = link.filter
                self.assertLessEqual(len(link.event_ids), min(link.count, MAX_EVENT_IDS))
                for event_id in link.event_ids:
                    e = self.events[event_id]
                    self.assertIn(e.type, flt.types)
                    self.assertTrue(flt.from_s <= e.clock_s <= flt.to_s)
                    if flt.team_id:
                        self.assertEqual(e.team_id, flt.team_id)
                    if flt.player_id:
                        self.assertEqual(e.player_id, flt.player_id)

    def test_a_goal_links_its_shot_and_its_scorer(self) -> None:
        goal = next(o for o in self.doc.overlays if o.kind == "goal")
        evidence = [ev for c in goal.provenance.claims for ev in c.evidence]
        scorer = next(ev for ev in evidence if ev.kind == "player" and ev.involvement == "scorer")
        shots = [self.events[i] for i in scorer.events[0].event_ids]
        self.assertTrue(any(s.type == "shot" and s.outcome == "goal" for s in shots))
        xg = next(ev for ev in evidence if ev.metric == "shot_xg")
        trigger = next(link for link in xg.events if link.relation == "triggering")
        self.assertTrue(any(self.events[i].type == "shot" for i in trigger.event_ids))

    def test_semantic_status_is_reported_per_claim(self) -> None:
        faulted = overlay_from_dir(bundle_dir(faults=True))
        states = {c.semantic for o in faulted.overlays for c in o.provenance.claims}
        self.assertEqual(states, {"passed", "skipped", "not_run"})
        for o in faulted.overlays:
            for c in o.provenance.claims:
                if o.provenance.source == "template":
                    self.assertEqual(c.semantic, "not_run")


class MetricCoverageTests(unittest.TestCase):
    def test_every_ledger_metric_is_mapped_to_event_types(self) -> None:
        for storyline in ("balanced", "comeback", "siege", "end_to_end"):
            bundle = load_bundle(bundle_dir(3, storyline))
            metrics = {i.metric for n in bundle.narratives for i in n.ledger.items}
            self.assertFalse(metrics - set(METRIC_EVENTS), f"{storyline}: unmapped metrics")


class NumberSpanTests(unittest.TestCase):
    claims = [Claim(claim_id="c1", text="", refs=["e1", "c4"],
                    numbers=[ClaimNumber(value=0.64, ref="e1"), ClaimNumber(value=60.0, ref="c4"),
                             ClaimNumber(value=1.0, ref="e9")])]

    def spans(self, text: str) -> list[tuple[str, list[str]]]:
        return [(text[s.start:s.end], s.refs) for s in number_spans(text, self.claims)]

    def test_cited_figures_are_linked(self) -> None:
        self.assertEqual(self.spans("A 0.64 xG chance and 60% possession."), [("0.64", ["e1"]), ("60", ["c4"])])

    def test_scores_and_minutes_are_not_figures(self) -> None:
        self.assertEqual(self.spans("Now 1-1 after 1' and 1 – 0 before."), [])

    def test_a_decimal_comma_is_read(self) -> None:
        self.assertEqual(self.spans("Una ocasión de 0,64 xG."), [("0,64", ["e1"])])

    def test_uncited_numbers_are_left_alone(self) -> None:
        self.assertEqual(self.spans("17 shots in total"), [])

    def test_real_texts_only_link_refs_from_their_own_claims(self) -> None:
        doc = overlay_from_dir(bundle_dir())
        linked = 0
        for o in doc.overlays:
            cited = {e.ref for c in o.provenance.claims for e in c.evidence}
            for by_lang in o.texts.values():
                for variant in by_lang.values():
                    for span in variant.numbers:
                        linked += 1
                        self.assertLessEqual(set(span.refs), cited, o.overlay_id)
        self.assertGreater(linked, 0)


if __name__ == "__main__":
    unittest.main()
