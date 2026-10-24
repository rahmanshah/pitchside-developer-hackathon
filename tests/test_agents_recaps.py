"""Recaps say what is true at the whistle (decision 25).

Live recaps quoted a spell's 88% field tilt as a half's figures, a scorer's "2 shots and 0.37 xG" from minute 35
at the break (he had 3 and 1.03), and "it is 1-0" at a 1-1 half-time. The regressions replay the three recorded demo
matches offline: same seeds, storylines and clubs, so the same events.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path

from pitchside.agents.checker import CheckContext, check_claim, check_text
from pitchside.agents.contracts import Claim
from pitchside.agents.fake_agents import RESPONDERS
from pitchside.agents.languages import SPANISH
from pitchside.agents.ledger import POINT_METRICS, goal_margin_say, recap_score_say
from pitchside.agents.pipeline import RunConfig, build_pipeline, run_match
from pitchside.generator import generate_match
from pitchside.llm import Role, load_settings
from pitchside.schema import Score

from _agents import facts
from test_agents_workflow import RECAP, WorkflowTestCase

DEMO = {"comeback_0007": (7, "comeback", "aur", "irv"), "siege_0003": (3, "siege", "har", "wst"),
        "end_to_end_0004": (4, "end_to_end", "wst", "aur")}
_TMP = tempfile.TemporaryDirectory()


@lru_cache(maxsize=None)
def demo_run(name: str):
    seed, story, home, away = DEMO[name]
    game = generate_match(seed=seed, storyline=story, home=home, away=away)
    settings = load_settings({"PITCHSIDE_USAGE_LOG": str(Path(tempfile.mkdtemp(dir=_TMP.name)) / "usage.jsonl"),
                              "PITCHSIDE_TOOLS": "local"})
    pipeline = build_pipeline(settings)
    return asyncio.run(run_match(game.events, pipeline, RunConfig.from_settings(settings, strict=True),
                                 run_id="recap-test", answer_key=game.answer_key))


def recaps(name: str):
    return [r for r in demo_run(name).records if r.kind == "recap"]


class RecapPhraseTests(unittest.TestCase):
    def test_the_score_at_the_whistle_says_how_things_stand(self) -> None:
        f = facts()  # Aurora Rovers (home) v Ironvale Athletic
        self.assertEqual(recap_score_say("half_time", Score(home=1, away=0), f),
                         ("aur", "it is 1-0 at half-time: Aurora Rovers lead"))
        self.assertEqual(recap_score_say("half_time", Score(home=1, away=1), f), (None, "it is 1-1 at half-time"))
        self.assertEqual(recap_score_say("full_time", Score(home=1, away=2), f),
                         ("irv", "it ends 1-2: Ironvale Athletic win"))
        self.assertEqual(recap_score_say("full_time", Score(home=2, away=2), f), (None, "it ends 2-2: a draw"))
        self.assertEqual(recap_score_say("full_time", Score(home=2, away=1), f, SPANISH)[1],
                         "termina 2-1: gana Aurora Rovers")

    def test_an_earlier_goal_is_told_in_the_past(self) -> None:
        self.assertEqual(goal_margin_say(1, "Harbourton City", Score(home=1, away=0), past=True),
                         "made it 1-0: Harbourton City opened the scoring")
        self.assertEqual(goal_margin_say(0, "Westmere Town", Score(home=1, away=1), past=True),
                         "made it 1-1: Westmere Town equalised")
        self.assertEqual(goal_margin_say(1, "Aurora Rovers", Score(home=2, away=1), SPANISH, past=True),
                         "puso el 2-1: Aurora Rovers se puso por delante")


class RecapLedgerTests(unittest.TestCase):
    def test_a_recap_repeats_only_numbers_fixed_at_their_own_moment(self) -> None:
        for name in DEMO:
            for record in recaps(name):
                for item in record.ledger.items:
                    with self.subTest(match=name, recap=record.label, ref=item.ref):
                        if item.source == "claim":
                            self.assertIn(item.metric, POINT_METRICS, item.say)
                        if item.metric.startswith("player_"):
                            self.assertEqual(item.window_end_s, record.clock_s, item.say)
                            self.assertNotIn("this one", item.say)
                        if item.metric == "goal_margin":
                            self.assertTrue(item.say.startswith("made it "), item.say)

    def test_the_recap_carries_its_own_score_at_the_whistle(self) -> None:
        expected = {("comeback_0007", "half_time"): "it is 0-1 at half-time: Ironvale Athletic lead",
                    ("comeback_0007", "full_time"): "it ends 2-1: Aurora Rovers win",
                    ("siege_0003", "half_time"): "it is 1-0 at half-time: Harbourton City lead",
                    ("siege_0003", "full_time"): "it ends 2-1: Harbourton City win",
                    ("end_to_end_0004", "half_time"): "it is 1-0 at half-time: Westmere Town lead",
                    ("end_to_end_0004", "full_time"): "it ends 2-2: a draw"}
        for (name, label), say in expected.items():
            record = next(r for r in recaps(name) if r.label == label)
            [item] = [i for i in record.ledger.items if i.metric == "recap_score"]
            self.assertEqual(item.say, say, (name, label))

    def test_siege_0003_half_time_has_no_spell_figures(self) -> None:
        # The live half-time recap gave the 19'-24' siege window's 88% field tilt and 72% possession as the half's.
        record = next(r for r in recaps("siege_0003") if r.label == "half_time")
        windowed = [i for i in record.ledger.items if i.metric in ("field_tilt", "possession", "momentum")]
        self.assertEqual(windowed, [])

    def test_end_to_end_0004_half_time_has_the_scorers_numbers_at_the_break(self) -> None:
        # The live recap said "Xarno Uusitalo has 2 shots and 0.37 xG" (his numbers at 35'); at the break: 3, 1.03.
        record = next(r for r in recaps("end_to_end_0004") if r.label == "half_time")
        mine = {i.metric: i.value for i in record.ledger.items if i.player_id == "wst_09" and i.metric.startswith("player_")}
        self.assertEqual((mine["player_shots"], round(mine["player_xg"], 2)), (3.0, 1.03))
        self.assertTrue(all(not (i.player_id == "wst_09" and i.source == "claim") for i in record.ledger.items))


class RecapScoreCheckTests(unittest.TestCase):
    """In a recap, "it is" gives the score at the whistle; any other score there is score_mismatch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.record = next(r for r in recaps("end_to_end_0004") if r.label == "half_time")  # 1-0 at the break
        full = next(r for r in recaps("end_to_end_0004") if r.label == "full_time")  # 2-2; goals 1-0, 1-1, 2-1, 2-2
        cls.ctx = CheckContext(ledger=full.ledger, facts=full_facts(), score=Score(home=2, away=2),
                               goals=goal_history(), window_start_s=0.0, window_end_s=full.clock_s, minute=91,
                               recap=True)
        cls.ref = next(i.ref for i in full.ledger.items if i.metric == "recap_score")

    def codes(self, text: str) -> list[str]:
        claim = Claim(claim_id="k1", text=text, team_id=None, refs=[self.ref], numbers=[])
        return [r.code for r in check_claim(claim, self.ctx).reasons]

    def test_an_earlier_score_given_as_current_is_rejected(self) -> None:
        self.assertEqual(self.codes("Aurora Rovers scored, and it is 1-1."), ["score_mismatch"])
        self.assertEqual(self.codes("Westmere Town are now 2-1 up."), ["score_mismatch"])
        reasons = check_claim(Claim(claim_id="k1", text="It is 2-1.", refs=[self.ref]), self.ctx).reasons
        self.assertIn("at the whistle it is 2-2", reasons[0].detail)

    def test_the_score_at_the_whistle_and_earlier_scores_in_the_past_pass(self) -> None:
        self.assertEqual(self.codes("It ends 2-2: a draw."), [])
        self.assertEqual(self.codes("It is 2-2 at full-time."), [])
        self.assertEqual(self.codes("Westmere Town's second goal made it 2-1 before Aurora Rovers equalised."), [])

    def test_persona_texts_follow_the_rule_in_their_language(self) -> None:
        self.assertEqual([r.code for r in check_text("It is 2-1 to Westmere Town.", self.ctx, [self.ref])],
                         ["score_mismatch"])
        self.assertEqual([r.code for r in check_text("Va 1-1 tras el gol.", self.ctx, [self.ref], SPANISH)],
                         ["score_mismatch"])
        self.assertEqual(check_text("Termina 2-2: empate.", self.ctx, [self.ref], SPANISH), [])

    def test_outside_a_recap_the_rule_does_not_apply(self) -> None:
        ctx = CheckContext(**{**self.ctx.__dict__, "recap": False})
        claim = Claim(claim_id="k1", text="It is 1-1.", refs=[self.ref])
        self.assertEqual(check_claim(claim, ctx).reasons, [])


def full_facts():
    from pitchside.agents.ledger import match_facts
    return match_facts(generate_match(seed=4, storyline="end_to_end", home="wst", away="aur").events[0])


def goal_history():
    from pitchside.agents.contracts import GoalAt
    from pitchside.schema import ShotEvent
    events = generate_match(seed=4, storyline="end_to_end", home="wst", away="aur").events
    return [GoalAt(clock_s=e.clock_s, team_id=e.team_id, player_id=e.player_id, score=e.score_after)
            for e in events if isinstance(e, ShotEvent) and e.outcome == "goal"]


def explainer_with_a_stale_score() -> dict:
    """The offline explainer, plus a recap claim giving an earlier score as current, on the first draft only."""
    def respond(transcript: str) -> str:
        reply = json.loads(RESPONDERS[Role.EXPLAINER](transcript))
        if "FEEDBACK" not in transcript and "MOMENT: recap" in transcript:
            ref = next(line.split(" | ")[0] for line in transcript.splitlines() if " | recap_score | " in line)
            reply["claims"].append({"text": "It is 0-0.", "team_id": None, "refs": [ref], "numbers": []})
        return json.dumps(reply)
    return {Role.EXPLAINER: respond}


class RecapRetryTests(WorkflowTestCase):
    def test_a_stale_score_in_a_recap_is_sent_back_and_fixed(self) -> None:
        # seed 1 comeback half-time comes after a goal, so "it is 0-0" (a score the match had) is stale there
        self.assertNotEqual(RECAP.score, Score(home=0, away=0))
        record = self.run_job(RECAP, responders=explainer_with_a_stale_score())
        self.assertEqual((record.narrative.source, len(record.drafts)), ("model", 2))
        check = next(a for a in record.audit if a.step == "check")
        self.assertEqual(check.outcome, "rejected")
        retry = [a for a in record.audit if a.step == "explain"][1]
        self.assertTrue(any("at the whistle" in f for f in retry.data["feedback"]), retry.data["feedback"])
        self.assertFalse(any("0-0" in c.text for c in record.narrative.claims))


if __name__ == "__main__":
    unittest.main()
