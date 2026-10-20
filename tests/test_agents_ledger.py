"""Tests for the agent contracts and the evidence ledger."""

from __future__ import annotations

import unittest

from pitchside.agents.contracts import (
    Claim, ExplainerReply, InterpreterReply, JobRecord, Ledger, Narrative, PersonalizerReply, VerifierReply,
)
from pitchside.agents.languages import ENGLISH, LANGUAGES, SPANISH, resolve_languages
from pitchside.agents.ledger import (
    default_featured, finding_ledger, number_text, recap_ledger, say,
)
from pitchside.stats.engine import StatsEngine

from _agents import events_by_id, facts, first, match, tally_until


class NumberFormatTests(unittest.TestCase):
    def test_units_are_written_the_way_claims_must_quote_them(self) -> None:
        self.assertEqual(number_text(71.43, "pct"), "71%")
        self.assertEqual(number_text(0.395, "xg"), "0.40")
        self.assertEqual(number_text(0.254, "index"), "+0.25")
        self.assertEqual(number_text(-0.046, "index"), "-0.05")
        self.assertEqual(number_text(4.24, "per_min"), "4.2")
        self.assertEqual(number_text(360.0, "s"), "6 min")
        self.assertEqual(number_text(29.0, "s"), "29 s")
        self.assertEqual(number_text(20.7, "m"), "21 m")
        self.assertEqual(number_text(3.0, "count"), "3")

    def test_spanish_uses_a_decimal_comma_and_its_own_phrases(self) -> None:
        self.assertEqual(number_text(0.395, "xg", SPANISH), "0,40")
        self.assertEqual(say("field_tilt", 71.4, "pct", SPANISH), "71% de dominio territorial")
        self.assertEqual(say("opponent_pass_completion", 64.0, "pct", ENGLISH, "Ironvale Athletic"),
                         "Ironvale Athletic completing 64% of their passes")

    def test_languages_resolve_with_english_first_and_unknown_codes_fail(self) -> None:
        self.assertEqual([l.code for l in resolve_languages(("es", "en"))], ["en", "es"])
        with self.assertRaises(ValueError):
            resolve_languages(("en", "xx"))

    def test_every_language_has_every_phrase(self) -> None:
        for lang in LANGUAGES.values():
            self.assertFalse(set(ENGLISH.phrases) - set(lang.phrases), f"{lang.code} is missing phrases")


class LedgerTests(unittest.TestCase):
    def test_a_goal_ledger_has_every_evidence_number_the_scorer_and_their_numbers(self) -> None:
        goal = first("goal")
        ledger = finding_ledger(goal, facts(), events_by_id(), tally_until(1, "comeback", goal.detected_at_s))
        evidence = [i for i in ledger.items if i.source == "finding"]
        self.assertEqual([i.ref for i in evidence], [f"e{n}" for n in range(1, len(goal.evidence) + 1)])
        self.assertEqual([i.value for i in evidence], [e.value for e in goal.evidence])
        [scorer] = ledger.players
        self.assertEqual((scorer.ref, scorer.involvement), ("p1", "scorer"))
        player_goals = next(i for i in ledger.items if i.metric == "player_goals")
        self.assertEqual((player_goals.player_id, player_goals.value), (scorer.player_id, 1.0))

    def test_featured_players_are_each_clubs_striker(self) -> None:
        f = facts()
        featured = default_featured(f)
        self.assertEqual([f.player(p).position for p in featured], ["ST", "ST"])
        self.assertEqual({f.player(p).team_id for p in featured}, {f.home.team_id, f.away.team_id})

    def test_recap_ledger_has_totals_players_and_earlier_verified_claims(self) -> None:
        goal = first("goal")
        ledger = finding_ledger(goal, facts(), events_by_id(), tally_until(1, "comeback", goal.detected_at_s))
        record = _shown_record(goal, ledger)
        engine = StatsEngine()
        for event in match().events:
            if event.clock_s > 2700:
                break
            engine.ingest(event)
        f = facts()
        recap, earlier = recap_ledger(engine.snapshot(), f, tally_until(1, "comeback", 2700), default_featured(f),
                                      [goal.team_id and next(p.player_id for p in ledger.players)], [record])
        self.assertTrue(any(i.metric == "shots_total" for i in recap.items))
        self.assertEqual(len(earlier), 1)
        copied = recap.item(earlier[0].refs[0])
        self.assertEqual((copied.source, copied.origin, copied.value), ("claim", goal.finding_id, goal.evidence[0].value))
        self.assertIn("scorer", {p.involvement for p in recap.players})


class WireModelTests(unittest.TestCase):
    def test_reply_models_are_strict_json_shapes(self) -> None:
        for model in (InterpreterReply, ExplainerReply, VerifierReply, PersonalizerReply):
            schema = model.model_json_schema()
            for name, definition in [(model.__name__, schema), *schema.get("$defs", {}).items()]:
                self.assertFalse(definition.get("additionalProperties", True), name)
                self.assertEqual(set(definition["required"]), set(definition["properties"]), name)


def _shown_record(goal, ledger: Ledger) -> JobRecord:
    claim = Claim(claim_id="k1", text="x", team_id=goal.team_id, refs=["e1"])
    narrative = Narrative(source="model", headline="h", claims=[claim], semantic_check="passed", texts=[])
    return JobRecord(job_id=goal.finding_id, kind="finding", label="goal", finding_id=goal.finding_id,
                     team_id=goal.team_id, clock_s=goal.detected_at_s, route="model", ledger=ledger, drafts=[],
                     checks=[], narrative=narrative, audit=[])


if __name__ == "__main__":
    unittest.main()
