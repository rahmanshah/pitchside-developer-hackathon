"""scripts/smoke_foundry.py offline: its call options, the cap report and the explainer step on the fake client."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

from pitchside.llm import Role, load_settings
from pitchside.llm.usage import UsageRecord

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("smoke_foundry", ROOT / "scripts" / "smoke_foundry.py")
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke  # dataclasses look the module up while the script runs
_spec.loader.exec_module(smoke)


def row(role: str, output: int, reasoning: int = 0, finish: str = "stop", status: str = "ok") -> UsageRecord:
    return UsageRecord(role=role, deployment="mini", status=status, output_tokens=output, reasoning_tokens=reasoning,
                       finish_reason=finish, price_model="gpt-5-mini", price_version="v")


class OptionsTests(unittest.TestCase):
    def test_tiny_calls_leave_room_to_reason_and_send_the_effort(self) -> None:
        settings = load_settings({})
        self.assertEqual(smoke.smoke_options(settings, "responses"),
                         {"max_tokens": 800, "reasoning": {"effort": "low"}})
        self.assertEqual(smoke.smoke_options(settings, "chat_completions"), {"max_tokens": 800})
        off = load_settings({"PITCHSIDE_REASONING_EFFORT": "off"})
        self.assertEqual(smoke.smoke_options(off, "responses"), {"max_tokens": 800})

    def test_usage_line_shows_reasoning_tokens(self) -> None:
        line = smoke.usage_line(row("explainer", 700, reasoning=448, finish="length"))
        self.assertIn("(reasoning   448)", line)
        self.assertIn("finish length", line)


class CapReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings({})  # caps 1500 / 3000 / 1500 / 4000

    def test_enough_headroom_is_ok_and_missing_roles_are_named(self) -> None:
        report = smoke.cap_report([row("explainer", 1400, 900), row("explainer", 600)], self.settings)
        self.assertEqual(report.too_tight, {})
        explainer = next(line for line in report.lines if line.startswith("explainer"))
        self.assertIn("largest output  1400 (reasoning   900) = 47% of the cap  ok", explainer)
        self.assertTrue(any(line.startswith("verifier") and "no calls" in line for line in report.lines))

    def test_less_than_twice_the_room_is_too_tight_with_the_variable_to_set(self) -> None:
        report = smoke.cap_report([row("verifier", 900, 700)], self.settings)
        self.assertEqual(report.too_tight, {"verifier": 2000})
        self.assertTrue(any("TOO TIGHT (less than 2x headroom): set PITCHSIDE_MAX_OUTPUT_VERIFIER=2000" in line
                            for line in report.lines))

    def test_a_reply_that_hit_the_cap_asks_for_at_least_double(self) -> None:
        report = smoke.cap_report([row("personalizer", 4000, 2500, finish="length")], self.settings)
        self.assertEqual(report.too_tight, {"personalizer": 8000})
        self.assertTrue(any("a reply hit the cap" in line for line in report.lines))

    def test_refused_and_usage_free_rows_are_ignored(self) -> None:
        report = smoke.cap_report([row("interpreter", 0, status="refused"), row("interpreter", 0)], self.settings)
        self.assertEqual(report.too_tight, {})

    def test_suggested_caps_round_up_to_500(self) -> None:
        # twice the largest output, rounded up: 20 -> 500, 1480 -> 1500, 1520 -> 2000, 2002 -> 2500
        self.assertEqual([smoke.suggested_cap(n) for n in (10, 740, 760, 1001)], [500, 1500, 2000, 2500])


class ExplainerStepTests(unittest.TestCase):
    def test_the_explainer_step_returns_valid_json_on_the_fake_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings({"PITCHSIDE_USAGE_LOG": str(Path(tmp) / "usage.jsonl")})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ok, rows = asyncio.run(smoke.explainer_check(settings, "smoke-test-explainer"))
            self.assertTrue(ok, out.getvalue())
            self.assertIn("OK    valid JSON", out.getvalue())
            [r] = rows
            self.assertEqual((r.role, r.step, r.status), ("explainer", "explain", "ok"))
            self.assertEqual(smoke.cap_report(rows, settings).too_tight, {})
            self.assertEqual(settings.max_output(Role.EXPLAINER), 3000)


if __name__ == "__main__":
    unittest.main()
