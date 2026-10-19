"""Tests for the price table, the usage log and the `pitchside cost` summary."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from pitchside.cli import main
from pitchside.llm import ConfigError
from pitchside.llm.prices import PRICE_TABLE_VERSION, cost_usd, price_for
from pitchside.llm.usage import UsageLog, UsageRecord, format_summary, summarise

LUNA = price_for("gpt-6-luna")


def record(**overrides) -> UsageRecord:
    fields = dict(
        match_id="m1", finding_id="m1:goal:0", role="explainer", deployment="luna", model="gpt-6-luna",
        status="ok", input_tokens=1000, cached_tokens=0, output_tokens=200, cost_usd=0.0002,
        latency_ms=800.0, price_model="gpt-6-luna", price_version=PRICE_TABLE_VERSION,
    )
    fields.update(overrides)
    return UsageRecord(**fields)


class PriceTests(unittest.TestCase):
    def test_luna_prices_match_the_decision_log(self) -> None:
        self.assertEqual((LUNA.input_per_m, LUNA.cached_input_per_m, LUNA.output_per_m), (0.10, 0.01, 0.50))

    def test_cost_of_one_million_each(self) -> None:
        self.assertAlmostEqual(cost_usd(LUNA, 1_000_000, 0, 0), 0.10)
        self.assertAlmostEqual(cost_usd(LUNA, 0, 0, 1_000_000), 0.50)

    def test_cached_tokens_are_part_of_input_and_billed_lower(self) -> None:
        # 2000 input of which 1500 cached, 100 output
        expected = (500 * 0.10 + 1500 * 0.01 + 100 * 0.50) / 1e6
        self.assertAlmostEqual(cost_usd(LUNA, 2000, 1500, 100), expected)
        # cached can never exceed input
        self.assertAlmostEqual(cost_usd(LUNA, 100, 500, 0), 100 * 0.01 / 1e6)

    def test_unknown_model_fails_closed(self) -> None:
        with self.assertRaises(ConfigError):
            price_for("gpt-6-sol")


class UsageLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runs" / "usage.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip_creates_directories(self) -> None:
        log = UsageLog(self.path)
        log.append(record())
        log.append(record(status="error", error_kind="rate_limit", input_tokens=0, output_tokens=0, cost_usd=0))
        self.assertEqual([r.status for r in log.read()], ["ok", "error"])

    def test_unreadable_lines_are_skipped_and_counted(self) -> None:
        log = UsageLog(self.path)
        log.append(record())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"truncated": \n')
        log.append(record())
        self.assertEqual(len(list(log.read())), 2)
        self.assertEqual(log.bad_lines, 1)

    def test_missing_log_reads_as_empty(self) -> None:
        self.assertEqual(list(UsageLog(self.path).read()), [])


class SummaryTests(unittest.TestCase):
    def test_groups_and_totals(self) -> None:
        records = [
            record(role="explainer", latency_ms=100),
            record(role="explainer", latency_ms=300),
            record(role="verifier", match_id="m2", latency_ms=200),
            record(role="verifier", status="refused", input_tokens=0, output_tokens=0, cost_usd=0, latency_ms=0),
        ]
        rows, total = summarise(records, by="role")
        self.assertEqual([r.key for r in rows], ["explainer", "verifier"])
        self.assertEqual((total.attempts, total.ok, total.refused), (4, 3, 1))
        self.assertAlmostEqual(total.cost_usd, 0.0006)
        self.assertEqual(rows[0].percentile(50), 200)
        self.assertEqual(rows[1].latencies, [200], "refused calls never reached the service, no latency")
        by_match, _ = summarise(records, by="match")
        self.assertEqual([r.key for r in by_match], ["m1", "m2"])

    def test_format_has_a_total_line(self) -> None:
        rows, total = summarise([record()], by="role")
        text = format_summary(rows, total, "role")
        self.assertIn("explainer", text)
        self.assertIn("total", text)


class CostCommandTests(unittest.TestCase):
    def run_cli(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["cost", *args]), 0)
        return out.getvalue()

    def test_summarises_a_log_and_filters_by_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            log = UsageLog(path)
            log.append(record(match_id="m1", role="explainer"))
            log.append(record(match_id="m2", role="verifier"))
            text = self.run_cli("--log", str(path), "--match", "m2")
            self.assertIn("verifier", text)
            self.assertNotIn("explainer", text)
            self.assertIn("m1", self.run_cli("--log", str(path), "--by", "match"))

    def test_missing_log_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("no model usage", self.run_cli("--log", str(Path(tmp) / "none.jsonl")))


if __name__ == "__main__":
    unittest.main()
