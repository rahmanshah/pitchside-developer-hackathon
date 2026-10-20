"""A whole match through the director, offline: routing, recaps, determinism, the demo faults, budget."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pitchside.agents.faults import demo_plan
from pitchside.agents.jobs import route
from pitchside.agents.pipeline import RunConfig, build_pipeline, run_match
from pitchside.llm import load_settings

from _agents import match


def run(seed: int = 1, storyline: str = "comeback", faults=None, run_id: str = "test-run", **env):
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings({"PITCHSIDE_USAGE_LOG": str(Path(tmp) / "usage.jsonl"), **env})
        pipeline = build_pipeline(settings, faults)
        result = asyncio.run(run_match(match(seed, storyline).events, pipeline,
                                       RunConfig.from_settings(settings, strict=True), run_id=run_id,
                                       answer_key=match(seed, storyline).answer_key))
        result.usage = list(pipeline.models.log.read())
        return result


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run()

    def test_every_finding_and_both_recaps_get_a_record_in_match_order(self) -> None:
        r = self.result
        finding_ids = [f.finding_id for f in r.findings]
        self.assertEqual([x.finding_id for x in r.records if x.kind == "finding"], finding_ids)
        self.assertEqual([x.label for x in r.records if x.kind == "recap"], ["half_time", "full_time"])
        clocks = [x.clock_s for x in r.records]
        self.assertEqual(clocks, sorted(clocks))

    def test_model_eligible_jobs_go_to_the_agents_and_the_rest_get_templates(self) -> None:
        r = self.result
        for finding in r.findings:
            record = r.record(finding.finding_id)
            if route(finding) == "model":
                self.assertEqual((record.route, record.narrative.source), ("model", "model"), finding.finding_id)
            else:
                self.assertEqual(record.route, "template")
                self.assertTrue(record.narrative.reason)
        self.assertTrue(all(x.narrative.source == "model" for x in r.records if x.kind == "recap"))

    def test_recaps_are_built_from_earlier_verified_claims(self) -> None:
        full_time = self.result.records[-1]
        claim_items = [i for i in full_time.ledger.items if i.source == "claim"]
        self.assertTrue(claim_items)
        self.assertTrue(all(self.result.record(i.origin).narrative.source == "model" for i in claim_items))

    def test_usage_is_tagged_with_the_run_and_every_job(self) -> None:
        jobs = {u.finding_id for u in self.result.usage}
        model_jobs = {x.job_id for x in self.result.records if x.route == "model"}
        self.assertEqual(jobs, model_jobs)
        self.assertEqual({u.run_id for u in self.result.usage}, {"test-run"})

    def test_offline_runs_are_deterministic(self) -> None:
        again = run()
        self.assertEqual([x.model_dump() for x in again.records], [x.model_dump() for x in self.result.records])


class DemoFaultTests(unittest.TestCase):
    def test_the_demo_plan_shows_every_recovery_path(self) -> None:
        r = run(seed=7, storyline="end_to_end", faults=demo_plan())
        fired = {(f["role"], f["kind"]) for f in r.faults_fired}
        self.assertTrue({("explainer", "rate_limit"), ("verifier", "timeout"), ("personalizer", "server_error"),
                         ("explainer", "corrupt_all")} <= fired, fired)
        reasons = [x.narrative.reason or "" for x in r.records if x.route == "model"]
        self.assertTrue(any("rejected every claim" in reason for reason in reasons))
        self.assertTrue(any(x.narrative.semantic_check.startswith("skipped") for x in r.records))
        self.assertTrue(any(t.source == "template" and (t.reason or "").startswith("personalizer")
                            for x in r.records for t in x.narrative.texts))


class BudgetTests(unittest.TestCase):
    def test_once_the_budget_runs_out_later_jobs_skip_the_models(self) -> None:
        r = run(PITCHSIDE_MATCH_BUDGET_USD="0.004")
        self.assertIsNotNone(r.budget_exhausted)
        reasons = [x.narrative.reason or "" for x in r.records if x.route == "template" and x.kind == "finding"]
        self.assertTrue(any(reason.startswith("budget exhausted earlier") for reason in reasons))
        self.assertLessEqual(sum(u.cost_usd for u in r.usage), 0.004 + 1e-9)


if __name__ == "__main__":
    unittest.main()
