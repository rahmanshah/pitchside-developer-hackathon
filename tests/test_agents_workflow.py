"""The job workflow on the fake client: the happy path and every recovery path, with its audit trail."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from pitchside.agents.contracts import JobRecord
from pitchside.agents.fake_agents import RESPONDERS
from pitchside.agents.faults import FaultInjector, parse_faults
from pitchside.agents.languages import resolve_languages
from pitchside.agents.roles import build_agents
from pitchside.agents.workflow import RunFlags, run_job, workflow_mermaid
from pitchside.llm import load_settings
from pitchside.llm.factory import build_clients
from pitchside.llm.guard import call_context

from test_agents_fakes_faults import model_jobs

LANGS = resolve_languages(("en", "es"))
JOBS = list(model_jobs())
GOAL = next(j for j in JOBS if j.label == "goal")
RECAP = next(j for j in JOBS if j.kind == "recap")


class WorkflowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.usage = Path(self.tmp.name) / "usage.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_job(self, job=GOAL, faults: str = "", flags: Optional[RunFlags] = None, **env) -> JobRecord:
        settings = load_settings({"PITCHSIDE_USAGE_LOG": str(self.usage), **env})
        self.injector = FaultInjector(parse_faults(faults)) if faults else None
        middleware = (lambda role: [self.injector.middleware(role.value)]) if self.injector else ()
        self.models = build_clients(settings, fake_responders=RESPONDERS, backoff_base_s=0, backoff_cap_s=0,
                                    extra_middleware=middleware)
        if self.injector:
            self.injector.register(job)
        agents = build_agents(self.models)
        self.flags = flags or RunFlags()

        async def go():
            with call_context(run_id="r1", match_id=job.match_id, finding_id=job.job_id):
                return await run_job(job, agents, LANGS, self.flags)
        return asyncio.run(go())

    @staticmethod
    def steps(record: JobRecord) -> list[tuple[str, str]]:
        return [(a.step, a.outcome) for a in record.audit]


class HappyPathTests(WorkflowTestCase):
    def test_a_goal_goes_through_every_agent_and_is_shown_as_model_output(self) -> None:
        record = self.run_job()
        n = record.narrative
        self.assertEqual((n.source, n.semantic_check, record.route), ("model", "passed", "model"))
        self.assertTrue(n.claims)
        self.assertTrue(all(t.source == "model" for t in n.texts))
        self.assertEqual(len(n.texts), 4 * 2)  # analyst, casual, 2 favourite players; en + es
        self.assertEqual(self.steps(record), [("interpret", "brief"), ("explain", "draft"), ("check", "passed"),
                                              ("verify", "passed"), ("personalize", "texts"),
                                              ("publish", "model")])
        interpret = record.audit[0]
        self.assertEqual(interpret.data["tool_calls"][0]["tool"], "match_totals")
        self.assertTrue(interpret.prompt_version and interpret.input_digest)

    def test_usage_rows_link_to_steps_and_attempts(self) -> None:
        self.run_job()
        rows = list(self.models.log.read())
        self.assertEqual([(r.role, r.step, r.step_attempt) for r in rows],
                         [("interpreter", "interpret", 1), ("interpreter", "interpret", 1),
                          ("explainer", "explain", 1), ("verifier", "verify", 1),
                          ("personalizer", "personalize", 1)])
        self.assertTrue(all((r.run_id, r.finding_id) == ("r1", GOAL.job_id) for r in rows))

    def test_a_recap_goes_through_the_same_flow(self) -> None:
        record = self.run_job(RECAP)
        self.assertEqual((record.kind, record.narrative.source), ("recap", "model"))

    def test_the_workflow_draws_as_mermaid(self) -> None:
        settings = load_settings({"PITCHSIDE_USAGE_LOG": str(self.usage)})
        mermaid = workflow_mermaid(build_agents(build_clients(settings, fake_responders=RESPONDERS)), LANGS)
        for step in ("interpret", "explain", "check", "verify", "personalize", "fallback", "publish"):
            self.assertIn(step, mermaid)


class VerifierLoopTests(WorkflowTestCase):
    def test_a_wrong_number_is_rejected_and_fixed_on_retry_with_feedback(self) -> None:
        record = self.run_job(faults="explainer:corrupt_number")
        self.assertEqual(record.narrative.source, "model")
        self.assertEqual(len(record.drafts), 2)
        rejected = [c for c in record.checks if not c.ok]
        self.assertEqual({r.code for c in rejected for r in c.reasons}, {"number_mismatch"})
        self.assertEqual([s for s in self.steps(record) if s[0] in ("explain", "check")],
                         [("explain", "draft"), ("check", "rejected"), ("explain", "draft"), ("check", "passed")])
        retry = [a for a in record.audit if a.step == "explain"][1]
        self.assertTrue(any("number_mismatch" in f for f in retry.data["feedback"]))

    def test_after_the_last_attempt_only_the_passing_claims_are_kept(self) -> None:
        record = self.run_job(faults="explainer:corrupt_unknown_ref#*x9")
        self.assertEqual(record.narrative.source, "model")
        self.assertEqual(len(record.drafts), 3)
        self.assertEqual(len(record.narrative.claims), len(record.drafts[-1].claims) - 1)
        self.assertIn(("check", "partial"), self.steps(record))
        self.assertTrue(all("e99" not in c.refs for c in record.narrative.claims))

    def test_claims_rejected_on_every_attempt_fall_back_to_a_template(self) -> None:
        record = self.run_job(faults="explainer:corrupt_all#*x9")
        n = record.narrative
        self.assertEqual((n.source, n.semantic_check), ("template", "not_run"))
        self.assertIn("rejected every claim after 3 attempt", n.reason)
        self.assertEqual(len(record.drafts), 3)
        self.assertTrue(all(t.source == "template" and t.reason == n.reason for t in n.texts))

    def test_an_overclaim_is_caught_by_the_semantic_layer_and_fixed(self) -> None:
        record = self.run_job(faults="explainer:overclaim")
        self.assertEqual(record.narrative.source, "model")
        semantic = [c for c in record.checks if c.layer == "semantic" and not c.ok]
        self.assertTrue(semantic)
        self.assertIn(("verify", "rejected"), self.steps(record))

    def test_semantic_rejection_on_every_attempt_falls_back(self) -> None:
        record = self.run_job(faults="verifier:semantic_reject#*x9")
        self.assertEqual(record.narrative.source, "template")
        self.assertIn("semantic verifier rejected every claim", record.narrative.reason)


class FailureRecoveryTests(WorkflowTestCase):
    def test_a_rate_limit_is_retried_by_the_guard_and_nothing_changes(self) -> None:
        record = self.run_job(faults="explainer:rate_limit")
        self.assertEqual(record.narrative.source, "model")
        errors = [r for r in self.models.log.read() if r.status == "error"]
        self.assertEqual([(r.role, r.error_kind, r.injected, r.cost_usd) for r in errors],
                         [("explainer", "rate_limit", "rate_limit", 0.0)])

    def test_explainer_outage_falls_back_to_a_template_with_the_reason(self) -> None:
        record = self.run_job(faults="explainer:timeoutx4")
        n = record.narrative
        self.assertEqual(n.source, "template")
        self.assertTrue(n.reason.startswith("explainer timeout"), n.reason)
        self.assertIn(("explain", "failed"), self.steps(record))

    def test_verifier_outage_accepts_on_the_deterministic_check_flagged(self) -> None:
        record = self.run_job(faults="verifier:timeoutx4")
        n = record.narrative
        self.assertEqual(n.source, "model")
        self.assertTrue(n.semantic_check.startswith("skipped: verifier timeout"), n.semantic_check)

    def test_personalizer_outage_uses_template_texts(self) -> None:
        record = self.run_job(faults="personalizer:server_errorx4")
        n = record.narrative
        self.assertEqual(n.source, "model")  # the verified claims are still shown
        self.assertTrue(all(t.source == "template" and t.reason.startswith("personalizer server_error")
                            for t in n.texts))

    def test_one_bad_persona_text_is_replaced_and_the_rest_kept(self) -> None:
        record = self.run_job(faults="personalizer:corrupt_persona")
        sources = [t.source for t in record.narrative.texts]
        self.assertEqual(sources.count("template"), 1)
        bad = next(t for t in record.narrative.texts if t.source == "template")
        self.assertIn("unlisted_number", bad.reason)

    def test_interpreter_failure_uses_a_rule_based_brief(self) -> None:
        record = self.run_job(faults="interpreter:malformedx2")
        self.assertEqual(self.steps(record)[0], ("interpret", "failed"))
        self.assertEqual(record.audit[0].data["recovery"], "rule-based brief")
        self.assertEqual(record.narrative.source, "model")

    def test_budget_exhaustion_falls_back_and_flags_the_run(self) -> None:
        record = self.run_job(PITCHSIDE_MATCH_BUDGET_USD="0.0000001")
        self.assertEqual(record.narrative.source, "template")
        self.assertTrue(record.narrative.reason.startswith("explainer budget"), record.narrative.reason)
        self.assertIsNotNone(self.flags.budget_exhausted)
        self.assertTrue(all(r.status == "refused" for r in self.models.log.read()))


if __name__ == "__main__":
    unittest.main()
