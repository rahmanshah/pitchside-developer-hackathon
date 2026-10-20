"""The agents on the fake client: tools add ledger ids, output failures are recovered, options are passed."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pitchside.agents.contracts import ExplainerReply, InterpreterReply
from pitchside.agents.fake_agents import RESPONDERS
from pitchside.agents.faults import FaultInjector, parse_faults
from pitchside.agents.prompting import explainer_prompt, interpreter_prompt, rule_brief
from pitchside.agents.roles import LOCAL_TOOL_NAMES, ToolState, ask, build_agents
from pitchside.llm import Role, load_settings
from pitchside.llm.errors import MalformedOutput
from pitchside.llm.factory import build_clients
from pitchside.llm.guard import call_context

from test_agents_fakes_faults import model_jobs


class RolesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = load_settings({"PITCHSIDE_USAGE_LOG": str(Path(self.tmp.name) / "usage.jsonl")})
        self.goal = next(j for j in model_jobs() if j.label == "goal")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def agents(self, faults: str = ""):
        injector = FaultInjector(parse_faults(faults)) if faults else None
        models = build_clients(self.settings, fake_responders=RESPONDERS, backoff_base_s=0, backoff_cap_s=0,
                               extra_middleware=(lambda role: [injector.middleware(role.value)]) if injector else ())
        if injector:
            injector.register(self.goal)
        return build_agents(models), models, injector

    def run_in_job(self, coro_fn, attempt: int = 1):
        async def go():
            with call_context(run_id="r1", match_id=self.goal.match_id, finding_id=self.goal.job_id,
                              step="test", attempt=attempt):
                return await coro_fn()
        return asyncio.run(go())


class InterpreterToolTests(RolesTestCase):
    def test_tool_numbers_get_ledger_ids_and_the_model_can_cite_them(self) -> None:
        agents, models, _ = self.agents()
        ledger = self.goal.ledger.model_copy(deep=True)
        state = ToolState(job=self.goal, ledger=ledger)
        answer = self.run_in_job(lambda: ask(agents, Role.INTERPRETER, interpreter_prompt(self.goal, tools=LOCAL_TOOL_NAMES),
                                             InterpreterReply, tool_state=state))
        added = [i for i in ledger.items if i.source == "context"]
        self.assertEqual(len(added), 8)  # 4 totals x 2 clubs
        self.assertTrue(all(i.ref.startswith("c") for i in added))
        self.assertIn(added[0].ref, answer.value.focus_refs)
        self.assertEqual(state.calls, [{"tool": "match_totals", "refs": [i.ref for i in added]}])
        rows = list(models.log.read())
        self.assertEqual([r.role for r in rows], ["interpreter", "interpreter"])  # tool call, then the answer
        self.assertEqual({r.step for r in rows}, {"test"})


class OutputRecoveryTests(RolesTestCase):
    def test_malformed_reply_is_re_asked_once(self) -> None:
        agents, _, injector = self.agents("explainer:malformed")
        prompt = explainer_prompt(self.goal, rule_brief(self.goal), [], 1)
        answer = self.run_in_job(lambda: ask(agents, Role.EXPLAINER, prompt, ExplainerReply))
        self.assertTrue(answer.value.claims)
        self.assertEqual(answer.notes, ["reply was not the JSON asked for; re-asked once"])
        self.assertEqual([f.kind for f in injector.fired], ["malformed"])

    def test_malformed_twice_raises(self) -> None:
        agents, _, _ = self.agents("explainer:malformedx2")
        prompt = explainer_prompt(self.goal, rule_brief(self.goal), [], 1)
        with self.assertRaises(MalformedOutput):
            self.run_in_job(lambda: ask(agents, Role.EXPLAINER, prompt, ExplainerReply))

    def test_truncated_reply_is_retried_at_twice_the_cap(self) -> None:
        agents, models, _ = self.agents("explainer:truncated")
        prompt = explainer_prompt(self.goal, rule_brief(self.goal), [], 1)
        answer = self.run_in_job(lambda: ask(agents, Role.EXPLAINER, prompt, ExplainerReply))
        self.assertEqual(answer.max_tokens, 6000)
        self.assertIn("max_tokens=6000", answer.notes[0])
        first, second = list(models.log.read())
        self.assertEqual((first.finish_reason, first.injected, first.cost_usd), ("length", "truncated", 0.0))
        self.assertEqual(second.status, "ok")

    def test_every_call_carries_the_roles_cap_reasoning_effort_and_schema(self) -> None:
        agents, models, _ = self.agents()
        prompt = explainer_prompt(self.goal, rule_brief(self.goal), [], 1)
        self.run_in_job(lambda: ask(agents, Role.EXPLAINER, prompt, ExplainerReply))
        options = models[Role.EXPLAINER].calls[0].options
        self.assertEqual(options["max_tokens"], 3000)
        self.assertEqual(options["reasoning"], {"effort": "low"})
        self.assertIs(options["response_format"], ExplainerReply)


if __name__ == "__main__":
    unittest.main()
