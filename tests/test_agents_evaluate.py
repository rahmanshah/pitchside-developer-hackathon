"""The agent evaluation (offline): scores, the plumbing label, injected bad drafts, and the CLI."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from pitchside.agents.evaluate import OFFLINE_BANNER, evaluate_run, format_report, summarise, to_json
from pitchside.agents.faults import bad_claims_plan, demo_plan
from pitchside.cli import main

from test_agents_pipeline import run


class EvaluationTests(unittest.TestCase):
    def test_a_clean_offline_run_passes_every_check_and_says_it_is_a_plumbing_check(self) -> None:
        evals = [evaluate_run(run(1, "comeback")), evaluate_run(run(1, "balanced"))]
        summary = summarise(evals, 0.10, faults_planned=False, injected_planned=False)
        self.assertTrue(summary.passed, summary.checks)
        v = summary.values
        self.assertEqual((v["goals_naming_scoring_club"], v["recaps"], v["fallback_rate"]), (1.0, 4, 0.0))
        report = format_report(evals, summary, "1")
        self.assertTrue(report.splitlines()[1].startswith("OFFLINE EVALUATION (fake model client): PLUMBING CHECK"))
        self.assertIn("RESULT: PASS  (offline plumbing check, not model quality)", report)
        self.assertEqual(to_json(evals, summary)["mode"], "fake")

    def test_injected_bad_drafts_are_counted_and_all_caught(self) -> None:
        ev = evaluate_run(run(1, "end_to_end", faults=bad_claims_plan(0.5)))
        self.assertGreater(ev.injected, 0)
        self.assertEqual(ev.injected_caught, ev.injected)
        self.assertGreater(ev.first_draft_failed_deterministic, 0)
        self.assertEqual(ev.shown_recheck_failures, 0)

    def test_demo_faults_show_up_as_recovery_numbers(self) -> None:
        ev = evaluate_run(run(7, "end_to_end", faults=demo_plan()))
        self.assertEqual(ev.model_fallbacks.get("checker rejected"), 1)
        self.assertGreaterEqual(ev.semantic_skipped, 1)
        self.assertGreater(ev.persona_template, 0)


class CliTests(unittest.TestCase):
    def test_eval_agents_runs_offline_and_writes_its_report(self) -> None:
        # Offline whatever the developer's environment holds: fake client, local tools, no MCP server URL.
        offline = {"PITCHSIDE_LLM": "fake", "PITCHSIDE_TOOLS": "local"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, offline):
            os.environ.pop("PITCHSIDE_MCP_URL", None)  # restored by patch.dict on exit
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["eval-agents", "--seeds", "1", "--storylines", "comeback", "--out", tmp])
            self.assertEqual(code, 0, out.getvalue())
            [eval_dir] = Path(tmp).iterdir()
            self.assertTrue((eval_dir / "report.txt").read_text(encoding="utf-8").count(OFFLINE_BANNER.splitlines()[0]) == 2)
            [bundle] = (eval_dir / "bundles").iterdir()
            self.assertTrue((bundle / "evaluation.json").exists())

    def test_live_calls_need_live_flag_as_well_as_foundry_mode(self) -> None:
        env = {"PITCHSIDE_LLM": "foundry", "FOUNDRY_ENDPOINT": "https://a.services.ai.azure.com/api/projects/p",
               "MODEL_INTERPRETER": "l", "MODEL_EXPLAINER": "l", "MODEL_VERIFIER": "l", "MODEL_PERSONALIZER": "l"}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["run", "--seed", "1", "--out", "unused"]), 2)
        self.assertIn("add --live to confirm", err.getvalue())


if __name__ == "__main__":
    unittest.main()
