"""The invented-detail guard (decision 26): details no evidence can hold are rejected in claims, headlines and
persona texts. The positive cases are live outputs from the recorded demo bundles and the held-out evaluations; the
negative cases are near misses that must pass. A false-positive scan over every committed bundle, template, ledger
label and offline seeds 1-40 found no hit before this shipped (see decision 26)."""

from __future__ import annotations

import json
import unittest

from pitchside.agents.checker import CheckContext, check_claim, check_text, invented_details
from pitchside.agents.contracts import Claim
from pitchside.agents.fake_agents import RESPONDERS
from pitchside.agents.languages import ENGLISH, LANGUAGES, SPANISH
from pitchside.llm import Role

from _agents import facts
from test_agents_workflow import GOAL, WorkflowTestCase

F = facts()  # Aurora Rovers v Ironvale Athletic

INVENTED = {
    "woodwork": ["Rafa rattles the bar for Ironvale", "Alden Brannholt rattled the woodwork for Aurora Rovers",
                 "Alden Brannholt rattles the post with a 0.53 xG chance", "it came back off the crossbar"],
    "body part": ["Rafa Yaldiran heads Ironvale Athletic in front", "Alden Brannholt volleyed a big 0.27 xG chance",
                  "Brannholt volleys a big chance", "a header from 6 m", "a left-footed finish",
                  "he headed home from 5 m"],
    "record": ["Alden Brannholt scored his first goal for Aurora Rovers",  # live: "... for Harbourton City"
               "Rafa Yaldiran scored his second goal for Ironvale Athletic", "his first goal for the club",
               "a debut to remember", "his best this season", "a career high"],
}
INVENTED_ES = ["su primer gol para el club desde 22 m", "estrella el balón en el larguero", "remata de cabeza",
               "un zurdazo desde 20 m", "su debut", "esta temporada"]
FINE = ["Aurora Rovers headed into the break 1-0 up", "this is Rafa Yaldiran's first goal in this match",
        "Rafa Yaldiran has 2 goals in this match, including this one", "the shot was saved",
        "Aurora Rovers gained possession after the ball went out of play", "Ironvale Athletic posted 3 shots",
        "a goal for Aurora Rovers", "Alden Brannholt's club are on top", "they raise the tempo",
        "the shot went off target", "Aurora Rovers have 3 shots in this spell"]
FINE_ES = ["una señal de control para el club de Alden Brannholt", "el portero detuvo el tiro",
           "una carrera por la banda", "es el primer gol de Rafa Yaldiran en este partido", "el tiro se fue fuera"]


class GuardWordTests(unittest.TestCase):
    def test_live_inventions_are_caught_in_english(self) -> None:
        for kind, texts in INVENTED.items():
            for text in texts:
                with self.subTest(text=text):
                    reasons = invented_details(text, F, ENGLISH)
                    self.assertTrue(reasons, text)
                    self.assertEqual({r.code for r in reasons}, {"invented_detail"})

    def test_live_inventions_are_caught_in_spanish(self) -> None:
        for text in INVENTED_ES:
            with self.subTest(text=text):
                self.assertTrue(invented_details(text, F, SPANISH), text)

    def test_near_misses_pass(self) -> None:
        for text in FINE:
            with self.subTest(text=text):
                self.assertEqual(invented_details(text, F, ENGLISH), [])
        for text in FINE_ES:
            with self.subTest(text=text):
                self.assertEqual(invented_details(text, F, SPANISH), [])

    def test_no_phrase_of_ours_trips_it(self) -> None:
        for lang in LANGUAGES.values():
            for key, phrase in lang.phrases.items():
                self.assertEqual(invented_details(phrase, F, lang), [], f"{lang.code} {key}: {phrase}")

    def test_claims_and_persona_texts_are_rejected_with_the_reason(self) -> None:
        ctx = CheckContext.for_job(GOAL)
        ref = next(i.ref for i in GOAL.ledger.items if i.metric == "shot_outcome")
        claim = Claim(claim_id="k1", text="Ironvale Athletic scored and the shot came off the post.", team_id="irv",
                      refs=[ref])
        reasons = check_claim(claim, ctx).reasons
        self.assertEqual([r.code for r in reasons], ["invented_detail"])
        self.assertIn("shot_outcome", reasons[0].detail)
        self.assertEqual([r.code for r in check_text("¡Ironvale Athletic marca de cabeza!", ctx, [ref], SPANISH)],
                         ["invented_detail"])


def explainer_with_headline(headline: str, always: bool) -> dict:
    """The offline explainer with an invented headline: on the first draft only, or on every draft."""
    def respond(transcript: str) -> str:
        reply = json.loads(RESPONDERS[Role.EXPLAINER](transcript))
        if always or "FEEDBACK" not in transcript:
            reply["headline"] = headline
        return json.dumps(reply)
    return {Role.EXPLAINER: respond}


def personalizer_with_woodwork() -> dict:
    def respond(transcript: str) -> str:
        reply = json.loads(RESPONDERS[Role.PERSONALIZER](transcript))
        for t in reply["texts"]:
            if t["persona"] == "casual" and t["language"] == "en":
                t["text"] = "Ironvale Athletic hit the post!"
        return json.dumps(reply)
    return {Role.PERSONALIZER: respond}


class GuardWorkflowTests(WorkflowTestCase):
    def test_an_invented_headline_is_sent_back_and_fixed(self) -> None:
        record = self.run_job(responders=explainer_with_headline("Yaldiran heads Ironvale in front", always=False))
        n = record.narrative
        self.assertEqual((n.source, len(record.drafts)), ("model", 2))
        check = next(a for a in record.audit if a.step == "check")
        self.assertEqual((check.outcome, check.detail),
                         ("rejected", "the headline adds a detail the evidence cannot hold; retry"))
        retry = [a for a in record.audit if a.step == "explain"][1]
        self.assertTrue(any(f.startswith("headline: invented_detail") for f in retry.data["feedback"]))
        self.assertEqual(invented_details(n.headline, GOAL.facts), [])

    def test_a_headline_invented_on_every_draft_is_replaced_by_the_templates(self) -> None:
        record = self.run_job(responders=explainer_with_headline("Yaldiran rattles the bar", always=True))
        n = record.narrative
        self.assertEqual((n.source, len(record.drafts)), ("model", 3))
        self.assertNotIn("bar", n.headline)
        last = [a for a in record.audit if a.step == "check"][-1]
        self.assertIn("headline replaced by the template's", last.detail)

    def test_an_invented_persona_text_is_replaced_by_the_template(self) -> None:
        record = self.run_job(responders=personalizer_with_woodwork())
        casual = next(t for t in record.narrative.texts if t.persona == "casual" and t.language == "en")
        self.assertEqual(casual.source, "template")
        self.assertIn("invented_detail", casual.reason)


if __name__ == "__main__":
    unittest.main()
