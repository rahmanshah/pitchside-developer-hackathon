"""Tests for the budget guard, concurrency limit, timeouts and retries (all offline, fake client)."""

from __future__ import annotations

import asyncio
import random
import tempfile
import unittest
from pathlib import Path

from agent_framework import Agent, Message

from pitchside.llm.budget import BudgetGuard
from pitchside.llm.errors import AuthFailed, BudgetExhausted, ServiceUnavailable
from pitchside.llm.fake import Delay, Fail, FakeChatClient, Reply
from pitchside.llm.guard import ConcurrencyLimiter, ModelGuard, backoff_delay, call_context
from pitchside.llm.prices import PRICE_TABLE_VERSION
from pitchside.llm.usage import UsageLog, UsageRecord

# Worst case for a short prompt with the default 1024-token output cap is about $0.0005 at Luna prices.
ROOMY = dict(match_budget_usd=1.0, match_max_calls=200, total_budget_usd=10.0)


class Harness:
    def __init__(self, tmp: Path, *, limit: int = 4, **caps) -> None:
        self.log = UsageLog(tmp / "usage.jsonl")
        self.budget = BudgetGuard(self.log, **{**ROOMY, **caps})
        self.limiter = ConcurrencyLimiter(limit)
        self.slept: list[float] = []

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def client(self, role: str = "explainer", script=(), **guard_kw) -> FakeChatClient:
        guard = ModelGuard(
            role=role, deployment=f"luna-{role}", log=self.log, budget=self.budget, limiter=self.limiter,
            rng=random.Random(7), sleep=self._sleep, **guard_kw,
        )
        return FakeChatClient(role, script=script, middleware=[guard])

    def records(self) -> list[UsageRecord]:
        return list(self.log.read())


async def ask(client: FakeChatClient, text: str = "Summarise the siege.", match: str = "m1", finding: str = "f1"):
    with call_context(match_id=match, finding_id=finding):
        return await client.get_response([Message(role="user", contents=[text])])


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class AccountingTests(GuardTestCase):
    def test_success_is_logged_with_ids_tokens_and_cost(self) -> None:
        h = Harness(self.dir)
        client = h.client(script=[Reply("ok", input_tokens=2000, cached_tokens=1500, output_tokens=100)])
        self.assertEqual(asyncio.run(ask(client)).text, "ok")
        [r] = h.records()
        self.assertEqual((r.status, r.role, r.deployment, r.model), ("ok", "explainer", "luna-explainer", "fake-luna"))
        self.assertEqual((r.match_id, r.finding_id, r.attempt), ("m1", "f1", 1))
        self.assertEqual((r.input_tokens, r.cached_tokens, r.output_tokens), (2000, 1500, 100))
        self.assertAlmostEqual(r.cost_usd, (500 * 0.10 + 1500 * 0.01 + 100 * 0.50) / 1e6)
        self.assertEqual(r.price_version, PRICE_TABLE_VERSION)
        self.assertAlmostEqual(h.budget.spent("m1"), r.cost_usd)

    def test_the_price_model_prices_the_call_and_is_logged(self) -> None:
        h = Harness(self.dir)
        client = h.client(script=[Reply("ok", input_tokens=2000, cached_tokens=1500, output_tokens=100)],
                          price_model="gpt-5-mini")
        asyncio.run(ask(client))
        [r] = h.records()
        self.assertEqual(r.price_model, "gpt-5-mini")
        self.assertAlmostEqual(r.cost_usd, (500 * 0.25 + 1500 * 0.03 + 100 * 2.00) / 1e6)

    def test_every_call_gets_an_output_cap(self) -> None:
        h = Harness(self.dir)
        client = h.client(max_output_tokens=300)
        asyncio.run(ask(client))
        self.assertEqual(client.calls[0].options.get("max_tokens"), 300)

    def test_missing_usage_is_charged_the_worst_case_not_zero(self) -> None:
        h = Harness(self.dir)
        asyncio.run(ask(h.client(script=[Reply("ok", input_tokens=0, output_tokens=0)])))
        [r] = h.records()
        self.assertGreater(r.cost_usd, 0)
        self.assertIn("worst-case", r.detail)

    def test_agent_runs_are_tagged_with_match_and_finding(self) -> None:
        h = Harness(self.dir)
        agent = Agent(client=h.client("verifier", script=[Reply("APPROVED")]), instructions="Verify.")

        async def run():
            with call_context(match_id="aur_irv_comeback_0007", finding_id="aur_irv_comeback_0007:goal:1"):
                return await agent.run("Claim: two goals.")

        self.assertEqual(asyncio.run(run()).text, "APPROVED")
        [r] = h.records()
        self.assertEqual((r.role, r.match_id, r.finding_id), ("verifier", "aur_irv_comeback_0007",
                                                              "aur_irv_comeback_0007:goal:1"))


class BudgetTests(GuardTestCase):
    def test_match_budget_refuses_without_calling_the_model(self) -> None:
        h = Harness(self.dir, match_budget_usd=0.0001)  # below one worst-case call
        client = h.client()
        with self.assertRaises(BudgetExhausted) as ctx:
            asyncio.run(ask(client))
        self.assertEqual(ctx.exception.scope, "match")
        self.assertEqual(client.calls, [])
        [r] = h.records()
        self.assertEqual((r.status, r.error_kind), ("refused", "budget"))
        self.assertIn("PITCHSIDE_MATCH_BUDGET_USD", r.detail)

    def test_match_budget_stops_after_spending_and_is_per_match(self) -> None:
        h = Harness(self.dir, match_budget_usd=0.0012)
        client = h.client(script=[Reply("a", input_tokens=10, output_tokens=1500)] * 3)  # $0.00075 each
        asyncio.run(ask(client, match="m1"))
        with self.assertRaises(BudgetExhausted):
            asyncio.run(ask(client, match="m1"))
        asyncio.run(ask(client, match="m2"))  # another match has its own budget
        self.assertEqual([r.status for r in h.records()], ["ok", "refused", "ok"])

    def test_match_call_cap(self) -> None:
        h = Harness(self.dir, match_max_calls=2)
        client = h.client()
        asyncio.run(ask(client))
        asyncio.run(ask(client))
        with self.assertRaises(BudgetExhausted) as ctx:
            asyncio.run(ask(client))
        self.assertEqual(ctx.exception.scope, "match_calls")
        self.assertEqual(len(client.calls), 2)

    def test_total_budget_spans_matches(self) -> None:
        h = Harness(self.dir, total_budget_usd=0.0012)  # $0.00075 spent + ~$0.0005 worst case > cap
        client = h.client(script=[Reply("a", input_tokens=10, output_tokens=1500)] * 3)  # $0.00075 each
        asyncio.run(ask(client, match="m1"))
        with self.assertRaises(BudgetExhausted) as ctx:
            asyncio.run(ask(client, match="m2"))
        self.assertEqual(ctx.exception.scope, "total")
        self.assertIn("PITCHSIDE_TOTAL_BUDGET_USD", str(ctx.exception))

    def test_spending_is_reloaded_from_the_log(self) -> None:
        log = UsageLog(self.dir / "usage.jsonl")
        for match, cost in (("m1", 0.06), ("m2", 3.0), ("m3", 6.95)):
            log.append(UsageRecord(match_id=match, role="explainer", deployment="luna", status="ok",
                                   cost_usd=cost, price_model="gpt-6-luna", price_version=PRICE_TABLE_VERSION))
        log.append(UsageRecord(match_id="m1", role="explainer", deployment="luna", status="refused",
                               cost_usd=0, price_model="gpt-6-luna", price_version=PRICE_TABLE_VERSION))
        budget = BudgetGuard(log, **ROOMY)
        self.assertAlmostEqual(budget.spent("m1"), 0.06)
        self.assertEqual(budget.calls("m1"), 1, "refused attempts were never sent")
        self.assertAlmostEqual(budget.total_spent(), 10.01)
        # A fresh process with the same log must refuse: the total cap ($10) is already spent.
        h = Harness(self.dir)
        with self.assertRaises(BudgetExhausted) as ctx:
            asyncio.run(ask(h.client(), match="m9"))
        self.assertEqual(ctx.exception.scope, "total")

    def test_concurrent_calls_cannot_overshoot(self) -> None:
        h = Harness(self.dir, match_budget_usd=0.0021)  # room for 4 worst-case reservations, not 6
        client = h.client(script=[Delay(0.02)] * 6)

        async def run():
            return await asyncio.gather(*(ask(client) for _ in range(6)), return_exceptions=True)

        results = asyncio.run(run())
        refused = [r for r in results if isinstance(r, BudgetExhausted)]
        self.assertEqual(len(refused), 2)
        self.assertLessEqual(h.budget.spent("m1"), 0.0021)


class ConcurrencyTests(GuardTestCase):
    def test_limit_is_shared_by_all_roles(self) -> None:
        h = Harness(self.dir, limit=2)
        clients = [h.client(role, script=[Delay(0.03)] * 3) for role in ("explainer", "verifier")]

        async def run():
            await asyncio.gather(*(ask(c) for c in clients for _ in range(3)))

        asyncio.run(run())
        self.assertEqual(h.limiter.peak, 2)
        self.assertEqual(h.limiter.in_flight, 0)
        self.assertEqual(len(h.records()), 6)

    def test_limiter_survives_separate_event_loops(self) -> None:
        h = Harness(self.dir, limit=1)
        client = h.client()
        asyncio.run(ask(client))
        asyncio.run(ask(client))
        self.assertEqual(h.limiter.peak, 1)

    def test_backoff_happens_outside_the_slot(self) -> None:
        h = Harness(self.dir, limit=1)
        seen_in_flight: list[int] = []

        async def sleep(seconds: float) -> None:
            seen_in_flight.append(h.limiter.in_flight)

        h._sleep = sleep  # type: ignore[method-assign]
        asyncio.run(ask(h.client(script=[Fail("rate_limit"), Reply("ok")])))
        self.assertEqual(seen_in_flight, [0])


class RetryTests(GuardTestCase):
    def test_rate_limit_then_success_honours_retry_after(self) -> None:
        h = Harness(self.dir)
        result = asyncio.run(ask(h.client(script=[Fail("rate_limit", retry_after_s=3), Reply("ok")])))
        self.assertEqual(result.text, "ok")
        self.assertEqual([(r.attempt, r.status, r.error_kind) for r in h.records()],
                         [(1, "error", "rate_limit"), (2, "ok", None)])
        self.assertGreaterEqual(h.slept[0], 3)

    def test_gives_up_after_max_retries(self) -> None:
        h = Harness(self.dir)
        client = h.client(script=[Fail("server_error")] * 5, max_retries=3)
        with self.assertRaises(ServiceUnavailable) as ctx:
            asyncio.run(ask(client))
        self.assertIn("after 4 attempts", str(ctx.exception))
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(len(h.slept), 3)
        for retry, delay in enumerate(h.slept, start=1):
            self.assertLessEqual(delay, min(20.0, 2 ** (retry - 1)))

    def test_auth_failure_is_not_retried_and_explains_the_fix(self) -> None:
        h = Harness(self.dir)
        client = h.client(script=[Fail("auth"), Reply("never")])
        with self.assertRaises(AuthFailed) as ctx:
            asyncio.run(ask(client))
        self.assertEqual(len(client.calls), 1)
        self.assertIn("az login", str(ctx.exception))
        self.assertIn("Cognitive Services OpenAI User", str(ctx.exception))

    def test_agent_callers_see_the_same_errors(self) -> None:
        h = Harness(self.dir)
        agent = Agent(client=h.client(script=[Fail("auth")]), instructions="Explain.")
        with self.assertRaises(AuthFailed):
            asyncio.run(agent.run("Why did the siege matter?"))
        h2 = Harness(self.dir / "other", match_budget_usd=0.0001)
        agent2 = Agent(client=h2.client(), instructions="Explain.")
        with self.assertRaises(BudgetExhausted):
            asyncio.run(agent2.run("Why did the siege matter?"))

    def test_timeout_is_retried_and_charged_the_worst_case(self) -> None:
        h = Harness(self.dir)
        client = h.client(script=[Delay(1.0), Reply("ok")], timeout_s=0.05)
        self.assertEqual(asyncio.run(ask(client)).text, "ok")
        first, second = h.records()
        self.assertEqual((first.status, first.error_kind), ("error", "timeout"))
        self.assertGreater(first.cost_usd, 0)
        self.assertIn("may be billed", first.detail)
        self.assertEqual(second.status, "ok")

    def test_backoff_is_full_jitter_and_capped(self) -> None:
        rng = random.Random(1)
        delays = [backoff_delay(n, rng, base_s=1, cap_s=20) for n in range(1, 10)]
        for n, d in enumerate(delays, start=1):
            self.assertLessEqual(d, min(20, 2 ** (n - 1)))
        self.assertEqual(backoff_delay(1, random.Random(1), base_s=1, cap_s=20, retry_after_s=60), 20)


if __name__ == "__main__":
    unittest.main()
