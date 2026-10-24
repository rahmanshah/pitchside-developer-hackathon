"""Tests for the agent contracts and the evidence ledger."""

from __future__ import annotations

import unittest

from pitchside.agents.contracts import (
    Claim, ExplainerReply, InterpreterReply, JobRecord, Ledger, Narrative, PersonalizerReply, VerifierReply,
)
from pitchside.agents.languages import ENGLISH, LANGUAGES, SPANISH, resolve_languages
from pitchside.agents.ledger import (
    FLAG_METRICS, SPELL_WINDOW_METRICS, default_featured, finding_ledger, goal_effect, goal_margin_say, number_text,
    recap_ledger, say,
)
from pitchside.schema import Score
from pitchside.stats.engine import StatsEngine

from _agents import events_by_id, facts, findings, first, match, tally_until


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
                         "Ironvale Athletic are completing 64% of their passes")

    def test_flags_are_words_with_no_digit_in_every_language(self) -> None:
        # Live output quoted "ball won in open play (1 = yes): 1" as "(1)"; a flag has nothing to quote now.
        club = "Aurora Rovers"
        self.assertEqual(say("possession_won_in_open_play", 1.0, "count", team_name=club),
                         "Aurora Rovers won the ball in open play")
        self.assertEqual(say("possession_won_in_open_play", 0.0, "count", team_name=club),
                         "Aurora Rovers gained possession from a restart or a save")  # cause unknown
        self.assertEqual(say("ended_with_shot", 1.0, "count"), "the move ended with a shot")
        self.assertEqual(say("ended_with_shot", 0.0, "count"), "the move did not end with a shot")
        self.assertEqual(say("possession_won_in_open_play", 1.0, "count", SPANISH, club),
                         "Aurora Rovers recuperó el balón en juego")
        self.assertEqual(say("ended_with_shot", 0.0, "count", SPANISH), "la jugada no terminó en tiro")
        for lang in LANGUAGES.values():
            for metric in FLAG_METRICS:
                for value in (0.0, 1.0):
                    for cause in (None, "kickoff", "goal_kick", "out_of_play", "shot_saved"):
                        self.assertNotRegex(say(metric, value, "count", lang, club, cause=cause), r"\d",
                                            f"{lang.code} {metric}={value} {cause}")

    def test_a_restart_is_named_by_its_real_cause_and_timed_from_gaining_possession(self) -> None:
        club = "Ironvale Athletic"
        causes = {"kickoff": "Ironvale Athletic gained possession from a kickoff",
                  "goal_kick": "Ironvale Athletic gained possession from a goal kick",
                  "out_of_play": "Ironvale Athletic gained possession after the ball went out of play",
                  "shot_saved": "Ironvale Athletic gained possession from a save by their goalkeeper"}
        for cause, text in causes.items():
            self.assertEqual(say("possession_won_in_open_play", 0.0, "count", team_name=club, cause=cause), text)
            self.assertEqual(say("seconds_since_turnover", 21.2, "s", cause=cause), "21 s after gaining possession")
        for cause in ("tackle", "interception", "shot_blocked", None):
            self.assertEqual(say("seconds_since_turnover", 21.2, "s", cause=cause), "21 s after winning the ball")

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

    def test_goal_evidence_carries_the_margin_from_that_goals_own_score(self) -> None:
        # Bounded by the goal's own event: the margin is the score after this goal, never a later one.
        for seed, story in ((1, "comeback"), (2, "siege"), (3, "end_to_end"), (4, "balanced")):
            key = {g["event_id"]: g for g in match(seed, story).answer_key["goals"]}
            home = facts(seed, story).home.team_id
            goals = [f for f in findings(seed, story) if f.kind.value == "goal"]
            self.assertEqual(len(goals), len(key))
            for goal in goals:
                g = key[goal.event_ids[0]]
                after = g["score_after"]
                mine, theirs = (after["home"], after["away"]) if g["team_id"] == home else (after["away"], after["home"])
                margin = {e.metric: e for e in goal.evidence}["goal_margin"]
                self.assertEqual((margin.team_id, margin.value, margin.baseline), (g["team_id"], mine - theirs,
                                                                                  mine - theirs - 1), goal.finding_id)
                self.assertEqual(margin.window_start_s, margin.window_end_s)

    def test_a_goal_ledger_says_what_the_goal_did_and_counts_it_for_the_scorer(self) -> None:
        goal = first("goal")  # seed 1 comeback: Ironvale Athletic open the scoring, 0-1
        ledger = finding_ledger(goal, facts(), events_by_id(), tally_until(1, "comeback", goal.detected_at_s))
        margin = next(i for i in ledger.items if i.metric == "goal_margin")
        self.assertEqual(margin.say, "it is 0-1: Ironvale Athletic open the scoring")  # no club named after the score
        scorer = ledger.players[0]
        goals = next(i for i in ledger.items if i.metric == "player_goals" and i.player_id == scorer.player_id)
        self.assertEqual(goals.say, f"this is {scorer.name}'s first goal of the match")

    def test_goal_effects_follow_the_margin(self) -> None:
        self.assertEqual([goal_effect(m, n) for m, n in ((1, 1), (0, 2), (1, 3), (2, 2), (-1, 3))],
                         ["opener", "equaliser", "ahead", "extend", "back"])
        self.assertEqual(goal_margin_say(0, "Ironvale Athletic", Score(home=1, away=1), SPANISH),
                         "va 1-1: Ironvale Athletic empata")

    def test_match_wide_chaos_numbers_name_their_window(self) -> None:
        # "79% pass completion" alone was written as "match pass completion": it is the spell's window, not the match.
        chaos = first("chaos_spell", status="start")
        ledger = finding_ledger(chaos, facts(), events_by_id())
        windowed = [i for i in ledger.items if i.metric in SPELL_WINDOW_METRICS]
        self.assertEqual({i.metric for i in windowed}, set(SPELL_WINDOW_METRICS))
        for item in windowed:
            self.assertIsNone(item.team_id)
            self.assertTrue(item.say.endswith(" in this spell"), item.say)
        self.assertEqual(ledger.item("e1").say, f"chaos index {number_text(chaos.evidence[0].value, 'index')} in this spell")

    def test_a_spells_counts_name_the_window_and_its_rates_do_not(self) -> None:
        # comeback_0209: "Aurora Rovers have 3 shots and 0.85 xG" was a 5-minute window read as match totals.
        for kind, windowed in (("momentum_shift", {"shots", "xg"}), ("siege", {"shots", "xg", "opponent_final_third_passes"})):
            ledger = finding_ledger(first(kind, status="start"), facts(), events_by_id())
            self.assertEqual({i.metric for i in ledger.items if i.say.endswith(" in this spell")}, windowed, kind)
            for item in ledger.items:
                self.assertNotIn("minute", item.say.replace("per minute", ""), item.say)  # no "5 minutes" anywhere

    def test_a_surge_ledger_names_the_pressed_club_and_no_window(self) -> None:
        surge = first("pressure_surge", status="start")
        f = facts()
        ledger = finding_ledger(surge, f, events_by_id())
        item = next(i for i in ledger.items if i.metric == "opponent_pass_completion")
        self.assertEqual(item.team_id, f.opponent(surge.team_id).team_id)
        self.assertTrue(item.say.startswith(f"{f.opponent(surge.team_id).name} are completing "), item.say)
        # Only the counts read like match totals, so only they name the window; a rate or a share does not.
        windowed = {i.metric for i in ledger.items if i.say.endswith(" in this spell")}
        self.assertEqual(windowed, {"tackles_won"})

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
