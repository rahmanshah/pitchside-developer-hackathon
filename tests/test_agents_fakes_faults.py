"""Prompts, the offline fake agents and fault injection.

The fake agents must produce output the real checker accepts (they are correct by construction, so offline runs
measure plumbing), and every corruption must be caught with the reason it is meant to trigger.
"""

from __future__ import annotations

import json
import unittest

from pitchside.agents import fake_agents
from pitchside.agents.checker import CheckContext, check_claim, check_text
from pitchside.agents.contracts import ExplainerReply, InterpreterReply, PersonalizerReply, VerifierReply
from pitchside.agents.faults import FaultInjector, bad_claims_plan, corrupt, demo_plan, parse_faults
from pitchside.agents.jobs import FindingPoint, RecapPoint, walk_match
from pitchside.agents.languages import LANGUAGES, resolve_languages
from pitchside.agents.ledger import FLAG_METRICS, SHOT_OUTCOMES
from pitchside.agents.prompting import (
    evidence_lines, explainer_prompt, instructions, interpreter_prompt, moment_line, personalizer_prompt, personas_for,
    prompt_version, rule_brief, to_claims, verifier_prompt,
)
from pitchside.agents.roles import LOCAL_TOOL_NAMES
from pitchside.llm.fake import ToolCall
from pitchside.llm.guard import call_context

from _agents import match

LANGS = resolve_languages(("en", "es"))


def model_jobs(seed: int = 1, storyline: str = "comeback"):
    for point in walk_match(match(seed, storyline).events):
        if isinstance(point, FindingPoint) and point.route == "model":
            yield point.job
        elif isinstance(point, RecapPoint):
            yield point.make([])


def fake_draft(job):
    brief = InterpreterReply.model_validate_json(fake_agents.interpreter(interpreter_prompt(job)))
    reply = ExplainerReply.model_validate_json(fake_agents.explainer(explainer_prompt(job, brief, [], 1)))
    return brief, reply, to_claims(reply, 1)


class PromptTests(unittest.TestCase):
    def test_instructions_are_versioned_and_have_examples(self) -> None:
        for role in ("interpreter", "explainer", "verifier", "personalizer"):
            text = instructions(role)
            self.assertIn("synthetic", text)
            self.assertIn("Example", text)
            self.assertEqual(len(prompt_version(role)), 12)

    def test_explainer_prompt_lists_every_ledger_id_and_the_feedback(self) -> None:
        job = next(model_jobs())
        prompt = explainer_prompt(job, rule_brief(job), ["claim 1: number_mismatch: wrong"], 2)
        for item in job.ledger.items:
            self.assertIn(f"{item.ref} | {item.metric}", prompt)
        self.assertIn("FEEDBACK (attempt 2):\nclaim 1: number_mismatch: wrong", prompt)

    def test_evidence_lines_say_both_clubs_and_give_flags_as_yes_or_no(self) -> None:
        jobs = list(model_jobs())
        lines = [line for job in jobs for line in evidence_lines(job.ledger)]
        match_wide = [line for line in lines if " | both clubs | " in line]
        self.assertTrue(match_wide)
        self.assertFalse([line for line in lines if " | match | " in line])
        flags = [line for line in lines if line.split(" | ")[1] in FLAG_METRICS]
        self.assertTrue(flags)
        for line in flags:
            self.assertIn(line.split(" | ")[3], ("yes", "no"), line)
        outcomes = [line for line in lines if line.split(" | ")[1] == "shot_outcome"]
        self.assertTrue(outcomes)
        for line in outcomes:
            self.assertIn(line.split(" | ")[3], SHOT_OUTCOMES, line)

    def test_the_moment_line_gives_the_score_as_a_clause_and_the_shots_outcome(self) -> None:
        # "score Aurora Rovers 0-0 Ironvale Athletic" was pasted into persona texts (decision 24).
        lines = {job.job_id: moment_line(job) for seed, story in ((1, "comeback"), (3, "end_to_end"))
                 for job in model_jobs(seed, story)}
        for job_id, line in lines.items():
            self.assertRegex(line, r"\| score: it is \d+-\d+(?: to (?:Aurora Rovers|Ironvale Athletic))?$", job_id)
            self.assertNotRegex(line, r"(?:Rovers|Athletic) \d+-\d+ (?:Aurora|Ironvale)")
            shot = any(k in job_id for k in (":goal:", ":big_chance:"))
            self.assertEqual("| outcome: " in line, shot, job_id)
        goal = next(job for job in model_jobs() if job.label == "goal")  # seed 1: Ironvale open the scoring
        self.assertTrue(moment_line(goal).endswith("| outcome: goal | score: it is 0-1 to Ironvale Athletic"))


class FakeAgentTests(unittest.TestCase):
    def test_fake_agents_pass_the_checker_on_every_model_job(self) -> None:
        jobs = 0
        for seed, story in ((1, "comeback"), (2, "siege"), (3, "end_to_end"), (4, "balanced")):
            for job in model_jobs(seed, story):
                jobs += 1
                ctx = CheckContext.for_job(job)
                _, reply, claims = fake_draft(job)
                with self.subTest(job=job.job_id):
                    self.assertTrue(claims)
                    for claim in claims:
                        check = check_claim(claim, ctx)
                        self.assertTrue(check.ok, f"{claim.text!r}: {check.reasons}")
                    verdicts = VerifierReply.model_validate_json(fake_agents.verifier(verifier_prompt(job, claims)))
                    self.assertTrue(all(v.supported for v in verdicts.verdicts))
                    self.assertEqual(len(verdicts.verdicts), len(claims))
                    texts = PersonalizerReply.model_validate_json(fake_agents.personalizer(
                        personalizer_prompt(job, claims, personas_for(job), LANGS)))
                    self.assertEqual({(t.persona, t.language) for t in texts.texts},
                                     {(p, l.code) for p in personas_for(job) for l in LANGS})
                    refs = [r for c in claims for r in c.refs]
                    for t in texts.texts:
                        self.assertEqual(check_text(t.text, ctx, refs, LANGUAGES[t.language]), [], t.text)
        self.assertGreater(jobs, 30)

    def test_fake_interpreter_asks_for_match_totals_on_goals_once(self) -> None:
        goal = next(j for j in model_jobs() if j.label == "goal")
        prompt = interpreter_prompt(goal, tools=LOCAL_TOOL_NAMES)
        self.assertEqual(fake_agents.interpreter(prompt), [ToolCall("match_totals")])
        answer = fake_agents.interpreter(prompt + '\ntool: {"items": [{"ref": "c4"}]}')
        self.assertIn("c4", json.loads(answer)["focus_refs"])


class CorruptionTests(unittest.TestCase):
    EXPECTED = {
        "corrupt_number": "number_mismatch",
        "corrupt_team": "team_mismatch",
        "corrupt_no_evidence": "no_evidence",
        "corrupt_unknown_ref": "unknown_evidence",
        "corrupt_unlisted_number": "unlisted_number",
        "corrupt_window": "window_mismatch",
        "corrupt_score": "score_mismatch",
    }

    def test_each_corruption_is_caught_with_its_reason(self) -> None:
        jobs = [j for j in model_jobs() if j.kind == "finding"][:6]
        for kind, code in self.EXPECTED.items():
            for job in jobs:
                _, reply, _ = fake_draft(job)
                bad = ExplainerReply.model_validate(corrupt(kind, reply.model_dump(), job))
                codes = {r.code for c in to_claims(bad, 1) for r in check_claim(c, CheckContext.for_job(job)).reasons}
                with self.subTest(kind=kind, job=job.job_id):
                    self.assertIn(code, codes)

    def test_overclaim_passes_the_checker_but_not_the_semantic_layer(self) -> None:
        job = next(j for j in model_jobs() if j.kind == "finding")
        _, reply, _ = fake_draft(job)
        claims = to_claims(ExplainerReply.model_validate(corrupt("overclaim", reply.model_dump(), job)), 1)
        self.assertTrue(all(check_claim(c, CheckContext.for_job(job)).ok for c in claims))
        verdicts = VerifierReply.model_validate_json(fake_agents.verifier(verifier_prompt(job, claims)))
        self.assertFalse(all(v.supported for v in verdicts.verdicts))


class FaultPlanTests(unittest.TestCase):
    def test_spec_grammar(self) -> None:
        [rule] = parse_faults("explainer:corrupt_number@goal.2#*x3").rules
        self.assertEqual((rule.role, rule.kind, rule.target, rule.attempt, rule.times),
                         ("explainer", "corrupt_number", "goal.2", None, 3))
        [rule] = parse_faults("verifier:timeout").rules
        self.assertEqual((rule.target, rule.attempt, rule.times), ("*", 1, 1))
        with self.assertRaises(ValueError):
            parse_faults("narrator:timeout")
        self.assertEqual(parse_faults("demo").name, "demo")
        self.assertGreater(len(demo_plan().describe()), 5)

    def test_rules_target_jobs_by_kind_and_order_and_fire_a_bounded_number_of_times(self) -> None:
        injector = FaultInjector(parse_faults("explainer:timeout@goal.2x2"))
        goals = [j for j in model_jobs() if j.label == "goal"]
        for job in goals:
            injector.register(job)
        with call_context(finding_id=goals[0].job_id, attempt=1):
            self.assertIsNone(injector.pick("explainer"))
        with call_context(finding_id=goals[1].job_id, attempt=1):
            self.assertIsNotNone(injector.pick("explainer"))
            self.assertIsNone(injector.pick("verifier"))
            self.assertIsNotNone(injector.pick("explainer"))
            self.assertIsNone(injector.pick("explainer"))
        self.assertEqual([f.job_id for f in injector.fired], [goals[1].job_id] * 2)

    def test_bad_claims_plan_hits_about_the_requested_share(self) -> None:
        injector = FaultInjector(bad_claims_plan(0.3))
        jobs = [j for s in (1, 2, 3) for j in model_jobs(s, "end_to_end")]
        hits = 0
        for job in jobs:
            injector.register(job)
            with call_context(finding_id=job.job_id, attempt=1):
                hits += injector.pick("explainer") is not None
        self.assertTrue(0.1 < hits / len(jobs) < 0.5, hits / len(jobs))


if __name__ == "__main__":
    unittest.main()
