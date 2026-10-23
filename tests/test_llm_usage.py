"""Tests for the price table, the usage log and the `pitchside cost` summary."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from pitchside.cli import main
from pitchside.llm import ConfigError
from pitchside.llm.prices import PRICE_TABLE, PRICE_TABLE_VERSION, cost_usd, price_for
from pitchside.llm.usage import UsageLog, UsageRecord, format_summary, summarise, total_cost

LUNA = price_for("gpt-6-luna")
MINI = price_for("gpt-5-mini")


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

    def test_gpt_5_mini_prices_match_the_decision_log(self) -> None:
        self.assertEqual((MINI.input_per_m, MINI.cached_input_per_m, MINI.output_per_m), (0.25, 0.03, 2.00))
        self.assertIn("2026-10-23", MINI.source)

    def test_gpt_5_mini_call_counts_reasoning_as_output(self) -> None:
        # 2000 input of which 1500 cached, 100 visible output + 600 reasoning = 700 output tokens
        expected = (500 * 0.25 + 1500 * 0.03 + 700 * 2.00) / 1e6
        self.assertAlmostEqual(cost_usd(MINI, 2000, 1500, 700), expected)

    def test_both_models_stay_in_the_table(self) -> None:
        self.assertEqual(set(PRICE_TABLE), {"gpt-6-luna", "gpt-5-mini"})

    def test_unknown_model_fails_closed(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            price_for("gpt-6-sol")
        self.assertIn("known: gpt-5-mini, gpt-6-luna", str(caught.exception))


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

    def test_a_log_with_both_models_is_grouped_by_model(self) -> None:
        luna_cost = cost_usd(LUNA, 1000, 0, 200)
        mini_cost = cost_usd(MINI, 1000, 0, 200)
        records = [
            record(model="gpt-6-luna-2026-09-01", cost_usd=luna_cost),
            record(model="gpt-5-mini-2025-08-07", deployment="mini", price_model="gpt-5-mini", cost_usd=mini_cost),
            record(model="gpt-5-mini-2025-08-07", deployment="mini", price_model="gpt-5-mini", cost_usd=mini_cost),
        ]
        rows, total = summarise(records, by="model")
        self.assertEqual([(r.key, r.attempts) for r in rows],
                         [("gpt-5-mini-2025-08-07", 2), ("gpt-6-luna-2026-09-01", 1)])
        self.assertAlmostEqual(rows[0].cost_usd, 2 * (1000 * 0.25 + 200 * 2.00) / 1e6)
        self.assertAlmostEqual(total.cost_usd, luna_cost + 2 * mini_cost)
        self.assertEqual(total_cost(records), 0.0015)  # 0.0002 + 2 * 0.00065

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

    def test_by_model_prints_each_model_with_its_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            log = UsageLog(path)
            log.append(record(model="gpt-6-luna", cost_usd=cost_usd(LUNA, 1000, 0, 200)))
            log.append(record(model="gpt-5-mini", price_model="gpt-5-mini", cost_usd=cost_usd(MINI, 1000, 0, 200)))
            lines = {line.split()[0]: line.split() for line in self.run_cli("--log", str(path), "--by", "model")
                     .splitlines() if line.startswith(("gpt-", "total"))}
            self.assertEqual(lines["gpt-6-luna"][8], "0.000200")
            self.assertEqual(lines["gpt-5-mini"][8], "0.000650")
            self.assertEqual(lines["total"][8], "0.000850")

    def test_missing_log_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("no model usage", self.run_cli("--log", str(Path(tmp) / "none.jsonl")))


class TotalCostTests(unittest.TestCase):
    def test_rounding_does_not_depend_on_the_python_version(self) -> None:
        # The five calls of one goal in the siege sample. A left-to-right float sum is exactly 0.0004505 on
        # Python 3.11 (rounds to 0.00045); 3.12+ sum() and math.fsum give 0.00045050000000000005 (0.000451).
        # Bundles, overlays and evaluations must agree on every supported version (CI runs 3.11 and 3.13).
        costs = [3.86e-05, 8.080000000000001e-05, 0.0001127, 3.990000000000001e-05, 0.0001785]
        records = [UsageRecord(role="explainer", deployment="fake", status="ok", price_model="m", price_version="v",
                               cost_usd=c) for c in costs]
        naive = 0.0
        for c in costs:
            naive += c
        self.assertEqual(round(naive, 6), 0.00045)  # the 3.11 behaviour that broke CI
        self.assertEqual(total_cost(records), 0.000451)
        self.assertEqual(total_cost([]), 0.0)


if __name__ == "__main__":
    unittest.main()
