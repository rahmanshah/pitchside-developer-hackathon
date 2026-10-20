"""The interpreter on MCP tools (PITCHSIDE_TOOLS=mcp): same ledger as local tools, bounded by the host, offline."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx

from pitchside.agents import fake_agents
from pitchside.agents.contracts import InterpreterReply
from pitchside.agents.fake_agents import RESPONDERS
from pitchside.agents.jobs import FindingPoint, RecapPoint, walk_match
from pitchside.agents.mcp_tools import INTERPRETER_TOOLS, McpTools
from pitchside.agents.pipeline import RunConfig, build_pipeline, run_match
from pitchside.agents.prompting import interpreter_prompt
from pitchside.agents.roles import ToolState, ask, build_agents
from pitchside.llm import Role, load_settings
from pitchside.llm.factory import build_clients
from pitchside.llm.fake import ToolCall
from pitchside.llm.guard import call_context
from pitchside.mcp_server import build_server, load_store
from pitchside.stats import tools

from _agents import match
from test_agents_pipeline import run
from test_stats_tools import TIMELINE

EVENTS = match(1, "comeback").events
JOBS = [p.job if isinstance(p, FindingPoint) else p.make([]) for p in walk_match(EVENTS)
        if isinstance(p, (FindingPoint, RecapPoint))]
LAST_GOAL = [j for j in JOBS if j.label == "goal"][-1]
_TMP = tempfile.TemporaryDirectory()


def settings(tools_mode: str = "mcp", **env):
    usage = Path(tempfile.mkdtemp(dir=_TMP.name)) / "usage.jsonl"
    return load_settings({"PITCHSIDE_USAGE_LOG": str(usage), "PITCHSIDE_TOOLS": tools_mode, **env})


def comparable(record):
    """A job record without what legitimately differs by tool mode: the interpreter's prompt digest."""
    data = record.model_dump(mode="json")
    for entry in data["audit"]:
        if entry["step"] == "interpret":
            entry["input_digest"] = None
    return data


class SameLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.local = run(1, "comeback", PITCHSIDE_TOOLS="local")
        cls.mcp = run(1, "comeback", PITCHSIDE_TOOLS="mcp")

    def test_every_job_records_the_same_ledger_drafts_checks_and_narrative_in_both_modes(self) -> None:
        self.assertEqual(len(self.local.records), len(self.mcp.records))
        for local, mcp in zip(self.local.records, self.mcp.records):
            with self.subTest(job=local.job_id):
                self.assertEqual(comparable(local), comparable(mcp))

    def test_the_comparison_covers_tool_numbers_not_just_finding_evidence(self) -> None:
        tool_items = [i for r in self.mcp.records for i in r.ledger.items if i.source == "context"]
        self.assertGreater(len(tool_items), 30)  # goals and recaps call match_totals: 8 numbers each
        self.assertEqual({i.metric for i in tool_items if i.ref.startswith("c")} >= {"possession_total", "xg_total"},
                         True)

    def test_the_manifest_says_how_the_interpreter_reached_its_tools(self) -> None:
        from pitchside.agents.bundle import build_manifest
        self.assertEqual((build_manifest(self.local, []).pipeline.interpreter_tools,
                          build_manifest(self.local, []).pipeline.mcp_server), ("local", None))
        m = build_manifest(self.mcp, [])
        self.assertEqual((m.pipeline.interpreter_tools, m.pipeline.mcp_server), ("mcp", "in-process"))

    def test_the_default_is_local(self) -> None:
        self.assertEqual(load_settings({}).tools, "local")
        self.assertIsNone(build_pipeline(load_settings({})).tools)


# --------------------------------------------------------------------------- #
# A scripted interpreter that calls every tool, including with hostile arguments
# --------------------------------------------------------------------------- #

EARLIER = TIMELINE.as_of(LAST_GOAL.clock_s, LAST_GOAL.as_of_event_id).findings[0]
FUTURE = TIMELINE.as_of(99999).findings[-1]
HOSTILE = {"as_of_s": 99999.0, "match_id": "some_other_match", "as_of_event_id": 10**9}


def scripted(calls):
    """Round 1: make these tool calls. After the results arrive: a valid brief citing every ref seen."""
    def respond(transcript: str):
        if "tool:" not in transcript:
            return list(calls)
        refs = sorted(set(__import__("re").findall(r'"ref":\s*"(c\d+)"', transcript)))
        return json.dumps({"angle": "scripted", "focus_refs": refs[:5] or ["e1"], "note": "scripted"})
    return respond


@asynccontextmanager
async def interpreter_env(responder, tools_mode: str = "mcp"):
    s = settings(tools_mode)
    models = build_clients(s, fake_responders={**RESPONDERS, Role.INTERPRETER: responder})
    mcp_tools: Optional[McpTools] = McpTools() if tools_mode == "mcp" else None
    agents = build_agents(models, mcp_tools)
    config = getattr(models[Role.INTERPRETER], "function_invocation_configuration")
    config.update(max_function_calls=50, max_iterations=10)  # this test calls many tools at once
    if mcp_tools:
        mcp_tools.register_match(EVENTS)
        await mcp_tools.open()
    try:
        yield agents
    finally:
        if mcp_tools:
            await mcp_tools.aclose()
        await models.aclose()


async def interpret(job, calls):
    ledger = job.ledger.model_copy(deep=True)
    state = ToolState(job=job, ledger=ledger)
    async with interpreter_env(scripted(calls)) as agents:
        with call_context(run_id="r", match_id=job.match_id, finding_id=job.job_id, step="interpret", attempt=1):
            answer = await ask(agents, Role.INTERPRETER, interpreter_prompt(job, agents.tool_names), InterpreterReply,
                               tool_state=state)
    return ledger, state, answer


def direct(name, job, *args):
    view = TIMELINE.as_of(job.clock_s, job.as_of_event_id)
    return getattr(tools, name)(view, *args)


class HostBoundTests(unittest.TestCase):
    def test_the_model_cannot_widen_the_bound_whatever_it_passes(self) -> None:
        calls = [ToolCall(n, dict(HOSTILE)) for n in ("match_totals", "recent_window", "momentum", "control_vs_chaos")]
        calls += [ToolCall(n, {**HOSTILE, "scope": "since_kickoff"})
                  for n in ("possession", "field_tilt", "pressure", "passing", "shots_and_xg")]
        ledger, state, _ = asyncio.run(interpret(LAST_GOAL, calls))
        self.assertEqual(len(state.calls), len(calls), "every tool ran")
        added = [i for i in ledger.items if i.source == "context"]
        self.assertTrue(added)
        self.assertTrue(all(i.window_end_s <= LAST_GOAL.clock_s for i in added))
        expected = []
        for name in ("match_totals", "recent_window", "momentum", "control_vs_chaos"):
            expected += direct(name, LAST_GOAL).items
        for name in ("possession", "field_tilt", "pressure", "passing", "shots_and_xg"):
            expected += direct(name, LAST_GOAL, "since_kickoff").items
        self.assertEqual([(i.metric, i.team_id, i.value, i.unit, i.window_start_s, i.window_end_s) for i in added],
                         [(e.metric, e.team_id, e.value, e.unit, e.window_start_s, e.window_end_s) for e in expected])

    def test_a_model_that_omits_the_required_bound_still_gets_a_valid_call(self) -> None:
        ledger, state, _ = asyncio.run(interpret(LAST_GOAL, [ToolCall("match_totals", {})]))
        self.assertEqual(len(state.calls[0]["refs"]), 8)

    def test_key_moments_and_evidence_lookup_return_only_what_was_known_then(self) -> None:
        calls = [ToolCall("key_moments", {**HOSTILE, "limit": 30}),
                 ToolCall("evidence_lookup", {**HOSTILE, "finding_id": EARLIER.finding_id})]
        ledger, state, answer = asyncio.run(interpret(LAST_GOAL, calls))
        evidence = [i for i in ledger.items if i.source == "context"]
        self.assertEqual([(i.metric, i.value, i.baseline) for i in evidence],
                         [(e.metric, e.value, e.baseline) for e in EARLIER.evidence])
        self.assertTrue(all(i.team_id == e.team_id for i, e in zip(evidence, EARLIER.evidence)))
        self.assertEqual(state.calls[0]["refs"], [], "key_moments has no numbers to give ids to")
        self.assertEqual(len(state.calls[1]["refs"]), len(EARLIER.evidence))

    def test_key_moments_reach_the_model_untouched_and_hold_nothing_from_the_future(self) -> None:
        seen = {}

        def responder(transcript: str):
            if "tool:" not in transcript:
                return [ToolCall("key_moments", {**HOSTILE, "limit": 30})]
            seen["text"] = transcript
            return json.dumps({"angle": "x", "focus_refs": ["e1"], "note": ""})

        async def go():
            async with interpreter_env(responder) as agents:
                with call_context(run_id="r", match_id=LAST_GOAL.match_id, finding_id=LAST_GOAL.job_id):
                    await ask(agents, Role.INTERPRETER, interpreter_prompt(LAST_GOAL, agents.tool_names),
                              InterpreterReply, tool_state=ToolState(LAST_GOAL, LAST_GOAL.ledger.model_copy(deep=True)))
        asyncio.run(go())
        data = json.loads(seen["text"].split("tool: ")[-1].splitlines()[0])
        ids = [m["finding_id"] for m in data["moments"]]
        self.assertEqual(ids, [f.finding_id for f in TIMELINE.as_of(LAST_GOAL.clock_s, LAST_GOAL.as_of_event_id).findings])
        self.assertNotIn(FUTURE.finding_id, ids)
        self.assertTrue(all(m["detected_at_s"] <= LAST_GOAL.clock_s for m in data["moments"]))

    def test_evidence_for_a_future_finding_is_an_error_the_run_survives(self) -> None:
        seen = {}

        def responder(transcript: str):
            if "tool:" not in transcript:
                return [ToolCall("evidence_lookup", {"as_of_s": 0, "finding_id": FUTURE.finding_id})]
            seen["text"] = transcript
            return json.dumps({"angle": "x", "focus_refs": ["e1"], "note": ""})

        async def go():
            async with interpreter_env(responder) as agents:
                ledger = LAST_GOAL.ledger.model_copy(deep=True)
                with call_context(run_id="r", match_id=LAST_GOAL.match_id, finding_id=LAST_GOAL.job_id):
                    answer = await ask(agents, Role.INTERPRETER, interpreter_prompt(LAST_GOAL, agents.tool_names),
                                       InterpreterReply, tool_state=ToolState(LAST_GOAL, ledger))
                return ledger, answer
        ledger, answer = asyncio.run(go())
        self.assertEqual(answer.value.angle, "x")
        self.assertEqual([i for i in ledger.items if i.source == "context"], [])
        # Agent Framework tells the model only that the call failed, never why (no detail to leak).
        self.assertEqual(seen["text"].split("tool: ")[-1].strip(), "Error: Function failed.")
        self.assertNotIn(FUTURE.finding_id, seen["text"])


class FakeInterpreterOnMcpTests(unittest.TestCase):
    def test_the_prompt_lists_the_tools_the_interpreter_really_has(self) -> None:
        self.assertEqual(INTERPRETER_TOOLS, ("match_totals", "recent_window", "key_moments", "evidence_lookup",
                                             "momentum", "control_vs_chaos", "possession", "field_tilt", "pressure",
                                             "passing", "shots_and_xg"))
        prompt = interpreter_prompt(LAST_GOAL, INTERPRETER_TOOLS)
        self.assertIn("TOOLS: match_totals, recent_window, key_moments", prompt)
        self.assertIn("do not pass times or match ids", prompt)
        self.assertEqual(fake_agents.interpreter(prompt)[0].name, "match_totals")

    def test_the_tool_schemas_the_model_sees_stay_small(self) -> None:
        async def go():
            tool = McpTools()
            tool.register_match(EVENTS)
            await tool.open()
            try:
                return [f.to_json_schema_spec() for f in tool.tool.functions]
            finally:
                await tool.aclose()
        specs = asyncio.run(go())
        self.assertEqual({s["function"]["name"] for s in specs}, set(INTERPRETER_TOOLS))
        tokens = len(json.dumps(specs)) / 4  # rough: what 11 tool schemas add to every interpreter call
        self.assertLess(tokens, 3000)  # measured about 2,800: about $0.0003 per call at Luna input prices


class RemoteServerTests(unittest.TestCase):
    """PITCHSIDE_MCP_URL: the pipeline talks to a deployed server that already holds the match."""

    HOST = "pitchside-mcp.example.azurecontainerapps.io"

    def test_a_remote_server_gives_the_same_ledgers_as_the_in_process_one(self) -> None:
        async def go():
            server = build_server(load_store(generate=["1:comeback"]), stateless_http=True, json_response=True,
                                  allowed_hosts=[self.HOST])
            app = server.streamable_http_app()
            s = settings("mcp", PITCHSIDE_MCP_URL=f"http://{self.HOST}/mcp")
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{self.HOST}")
            pipeline = build_pipeline(s, mcp_http_client=client)
            self.assertIsNone(pipeline.tools.store, "the pipeline does not load the match: the server has it")
            async with server.session_manager.run():
                result = await run_match(EVENTS, pipeline, RunConfig.from_settings(s, strict=True), run_id="remote",
                                         answer_key=match(1, "comeback").answer_key)
            await client.aclose()
            await pipeline.aclose()
            return result
        remote = asyncio.run(go())
        local = run(1, "comeback", PITCHSIDE_TOOLS="local")
        self.assertEqual(remote.tools_location, f"http://{self.HOST}/mcp")
        for a, b in zip(local.records, remote.records):
            with self.subTest(job=a.job_id):
                self.assertEqual(comparable(a), comparable(b))


if __name__ == "__main__":
    unittest.main()
