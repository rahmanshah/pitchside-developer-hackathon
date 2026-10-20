"""Guard behaviour added for the agents: run-scoped budgets, step tags, truncation, injected faults (offline)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_framework import ChatMiddleware
from agent_framework.exceptions import ChatClientException

from pitchside.llm import Role, load_settings
from pitchside.llm.budget import BudgetGuard, budget_key
from pitchside.llm.errors import BudgetExhausted, MalformedOutput
from pitchside.llm.fake import FakeChatClient, Reply, openai_error
from pitchside.llm.guard import INJECTED, ModelGuard, call_context, current_call_context
from pitchside.llm.usage import UsageLog
from test_llm_guard import Harness, ask


class RunScopedBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_rerun_of_the_same_match_gets_a_fresh_match_budget(self) -> None:
        h = Harness(self.dir, match_budget_usd=0.0008)
        client = h.client(script=[Reply("ok", input_tokens=100, output_tokens=1000)] * 3)

        async def run(run_id: str):
            with call_context(run_id=run_id):
                return await ask(client, match="m1")

        asyncio.run(run("r1"))
        with self.assertRaises(BudgetExhausted):  # the first run spent its match budget
            asyncio.run(run("r1"))
        asyncio.run(run("r2"))  # a new run of the same match starts fresh
        self.assertEqual([r.run_id for r in h.records() if r.status == "ok"], ["r1", "r2"])

    def test_run_scoped_spending_is_reloaded_from_the_log(self) -> None:
        h = Harness(self.dir)
        asyncio.run(_tagged(h.client(script=[Reply("ok", input_tokens=100, output_tokens=100)]), "r9"))
        reloaded = BudgetGuard(UsageLog(self.dir / "usage.jsonl"), match_budget_usd=1, match_max_calls=10,
                               total_budget_usd=10)
        self.assertGreater(reloaded.spent(budget_key("r9", "m1")), 0)
        self.assertEqual(reloaded.spent("m1"), 0.0)

    def test_call_context_merges_with_the_enclosing_block(self) -> None:
        with call_context(match_id="m1", finding_id="f1", run_id="r1"):
            with call_context(step="explain", attempt=2):
                ctx = current_call_context()
        self.assertEqual((ctx.run_id, ctx.match_id, ctx.finding_id, ctx.step, ctx.attempt),
                         ("r1", "m1", "f1", "explain", 2))
        self.assertIsNone(current_call_context().match_id)

    def test_usage_rows_carry_the_step_and_its_attempt(self) -> None:
        h = Harness(self.dir)
        asyncio.run(_tagged(h.client(script=[Reply("ok")]), "r1", step="verify", attempt=3))
        [r] = h.records()
        self.assertEqual((r.run_id, r.step, r.step_attempt, r.attempt), ("r1", "verify", 3, 1))


class TruncationTests(unittest.TestCase):
    def test_a_reply_cut_off_at_the_cap_is_logged_then_raised_as_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            client = h.client(script=[Reply('{"claims": [', output_tokens=300, finish_reason="length")],
                              max_output_tokens=300)
            with self.assertRaises(MalformedOutput) as ctx:
                asyncio.run(ask(client))
            self.assertEqual(ctx.exception.kind, "truncated")
            self.assertIn("max_tokens=300", str(ctx.exception))
            [r] = h.records()
            self.assertEqual((r.status, r.finish_reason), ("ok", "length"))
            self.assertGreater(r.cost_usd, 0)  # the tokens were produced and billed


class InjectedFaultTests(unittest.TestCase):
    def test_an_injected_timeout_is_retried_but_never_charged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            guarded = h.client(script=[Reply("ok")])
            client = FakeChatClient("explainer", script=[Reply("ok")],
                                    middleware=[*_middleware_of(guarded), _RaiseOnce("timeout")])
            asyncio.run(ask(client))
            first, second = h.records()
            self.assertEqual((first.status, first.error_kind, first.injected, first.cost_usd),
                             ("error", "timeout", "timeout", 0.0))
            self.assertIn("injected", first.detail)
            self.assertEqual(second.status, "ok")


class SettingsTests(unittest.TestCase):
    def test_per_role_output_caps_reasoning_and_languages(self) -> None:
        s = load_settings({})
        self.assertEqual([s.max_output(r) for r in Role], [1500, 3000, 1500, 4000])
        self.assertEqual((s.reasoning_effort, s.language_codes), ("low", ("en", "es")))
        s = load_settings({"PITCHSIDE_MAX_OUTPUT_EXPLAINER": "5000", "PITCHSIDE_LANGUAGES": "en, es, fr",
                           "PITCHSIDE_REASONING_EFFORT": "OFF"})
        self.assertEqual((s.max_output(Role.EXPLAINER), s.language_codes, s.reasoning_effort),
                         (5000, ("en", "es", "fr"), "off"))

    def test_bad_language_list_names_the_variable(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            load_settings({"PITCHSIDE_LANGUAGES": "english"})
        self.assertIn("PITCHSIDE_LANGUAGES", str(ctx.exception))


async def _tagged(client, run_id: str, **tags):
    with call_context(run_id=run_id, **tags):
        return await ask(client)


def _middleware_of(client: FakeChatClient) -> list:
    return [m for m in client.chat_middleware if isinstance(m, ModelGuard)]


class _RaiseOnce(ChatMiddleware):
    """Chat middleware that fails the first call the way the fault injector does."""

    def __init__(self, kind) -> None:
        self.kind, self.done = kind, False

    async def process(self, context, call_next) -> None:
        if not self.done:
            self.done = True
            error = openai_error(self.kind)
            wrapped = ChatClientException(f"injected {self.kind}", inner_exception=error)
            setattr(wrapped, INJECTED, self.kind)
            raise wrapped from error
        await call_next()


if __name__ == "__main__":
    unittest.main()
