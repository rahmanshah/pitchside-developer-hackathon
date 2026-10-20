"""The MCP server over the real protocol: tool schemas, read-only, no future leakage, stdio and Streamable HTTP."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from contextlib import asynccontextmanager

import httpx
from agent_framework import MCPStdioTool, MCPStreamableHTTPTool
from mcp.shared.memory import create_connected_server_and_client_session

from pitchside.mcp_server import build_server, load_store, parse_generate
from pitchside.stats import tools
from pitchside.stats.timeline import MatchStore

from _agents import match
from test_stats_tools import EVENTS, MOMENTS, TIMELINE, poisoned_timeline, timestamps

MATCH_ID = EVENTS[0].match_id
STAT_TOOLS = {"possession", "field_tilt", "pressure", "passing", "shots_and_xg", "match_totals", "recent_window",
              "momentum", "control_vs_chaos", "key_moments", "evidence_lookup"}
ALL_TOOLS = STAT_TOOLS | {"list_matches"}


def store_of(*timelines) -> MatchStore:
    store = MatchStore()
    store._timelines = {t.match_id: t for t in timelines}  # reuse already-replayed timelines (they are read-only)
    return store


@asynccontextmanager
async def connected(store: MatchStore, **options):
    async with create_connected_server_and_client_session(build_server(store, **options)._mcp_server) as session:
        yield session


async def call(session, name: str, **arguments):
    return await session.call_tool(name, arguments)


async def every_call(session, as_of_s: float) -> dict:
    """Every tool over the protocol at one moment; evidence lookups for every finding key_moments lists."""
    out = {}
    base = {"as_of_s": as_of_s}
    for name in ("possession", "field_tilt", "pressure", "passing", "shots_and_xg"):
        for scope in ("rolling", "since_kickoff"):
            out[f"{name}/{scope}"] = (await call(session, name, scope=scope, **base)).structuredContent
    for name in ("match_totals", "recent_window", "momentum", "control_vs_chaos"):
        out[name] = (await call(session, name, **base)).structuredContent
    moments = await call(session, "key_moments", limit=30, **base)
    out["key_moments"] = moments.structuredContent
    for moment in moments.structuredContent["moments"]:
        out[f"evidence/{moment['finding_id']}"] = (
            await call(session, "evidence_lookup", finding_id=moment["finding_id"], **base)).structuredContent
    return out


class ToolSchemaTests(unittest.TestCase):
    def listed(self):
        async def go():
            async with connected(store_of(TIMELINE)) as session:
                return (await session.list_tools()).tools
        return asyncio.run(go())

    def test_the_expected_tools_are_exposed_and_nothing_else(self) -> None:
        self.assertEqual({t.name for t in self.listed()}, ALL_TOOLS)

    def test_every_tool_is_read_only_described_and_typed(self) -> None:
        for tool in self.listed():
            with self.subTest(tool=tool.name):
                self.assertTrue(len(tool.description or "") > 40, "a clear description")
                a = tool.annotations
                self.assertTrue(a.readOnlyHint and a.idempotentHint and not a.destructiveHint and not a.openWorldHint)
                self.assertIsNotNone(tool.outputSchema, "typed result")
                self.assertEqual(tool.inputSchema.get("additionalProperties", False), False)

    def test_every_stat_tool_requires_as_of_s_and_it_cannot_be_negative(self) -> None:
        for tool in self.listed():
            if tool.name == "list_matches":
                continue
            with self.subTest(tool=tool.name):
                self.assertIn("as_of_s", tool.inputSchema["required"])
                prop = tool.inputSchema["properties"]["as_of_s"]
                self.assertEqual(prop["type"], "number")
                self.assertEqual(prop["minimum"], 0)
                self.assertIn("match seconds", prop["description"])
                self.assertIn("match_id", tool.inputSchema["properties"])
                self.assertNotIn("match_id", tool.inputSchema["required"])

    def test_enums_and_limits_are_in_the_schema(self) -> None:
        by_name = {t.name: t for t in self.listed()}
        scope = by_name["possession"].inputSchema["properties"]["scope"]
        self.assertEqual(scope["enum"], ["rolling", "since_kickoff"])
        kinds = json.dumps(by_name["key_moments"].inputSchema)
        for kind in ("goal", "big_chance", "siege", "chaos_spell"):
            self.assertIn(kind, kinds)
        self.assertEqual(by_name["key_moments"].inputSchema["properties"]["limit"]["maximum"], tools.MAX_MOMENTS)


class ProtocolResultTests(unittest.TestCase):
    def test_results_over_the_protocol_equal_the_direct_function_results(self) -> None:
        async def go():
            async with connected(store_of(TIMELINE)) as session:
                return await every_call(session, 4400.0)
        over_protocol = asyncio.run(go())
        direct = {k: json.loads(v) for k, v in __import__("test_stats_tools").every_tool(TIMELINE.as_of(4400.0)).items()}
        self.assertEqual(over_protocol, direct)

    def test_match_id_is_optional_for_one_match_and_required_for_several(self) -> None:
        other = match(2, "siege").events
        async def go():
            async with connected(store_of(TIMELINE)) as one:
                ok = await call(one, "match_totals", as_of_s=900.0)
            async with connected(store_of(TIMELINE, __import__("test_stats_tools").MatchTimeline(other))) as two:
                missing = await call(two, "match_totals", as_of_s=900.0)
                named = await call(two, "match_totals", as_of_s=900.0, match_id=other[0].match_id)
                listed = await call(two, "list_matches")
            return ok, missing, named, listed
        ok, missing, named, listed = asyncio.run(go())
        self.assertFalse(ok.isError)
        self.assertTrue(missing.isError)
        self.assertIn("match_id is required", missing.content[0].text)
        self.assertEqual(named.structuredContent["match_id"], other[0].match_id)
        self.assertEqual(len(listed.structuredContent["matches"]), 2)

    def test_bad_requests_are_tool_errors_not_crashes(self) -> None:
        async def go():
            async with connected(store_of(TIMELINE)) as session:
                return {
                    "negative": await call(session, "possession", as_of_s=-5.0),
                    "missing": await session.call_tool("possession", {}),
                    "unknown match": await call(session, "possession", as_of_s=5.0, match_id="nope"),
                    "bad scope": await call(session, "possession", as_of_s=5.0, scope="forever"),
                    "bad limit": await call(session, "key_moments", as_of_s=5.0, limit=0),
                    "later session still works": await call(session, "possession", as_of_s=5.0),
                }
        results = asyncio.run(go())
        for name in ("negative", "missing", "unknown match", "bad scope", "bad limit"):
            self.assertTrue(results[name].isError, name)
        self.assertFalse(results["later session still works"].isError)


class NoFutureOverTheProtocolTests(unittest.TestCase):
    def test_a_client_cannot_learn_anything_from_after_as_of_s(self) -> None:
        """Same server code, one match whose future was replaced by another match: identical answers up to T."""
        async def go(t, as_of):
            async with connected(store_of(TIMELINE)) as real, connected(store_of(poisoned_timeline(t))) as poisoned:
                return await every_call(real, as_of), await every_call(poisoned, as_of)
        for t in (1400.0, 4318.9):
            for as_of in (t / 2, t):
                with self.subTest(t=t, as_of=as_of):
                    real, poisoned = asyncio.run(go(t, as_of))
                    self.assertEqual(real, poisoned)

    def test_no_timestamp_in_any_response_is_after_as_of_s(self) -> None:
        async def go():
            seen = {}
            async with connected(store_of(TIMELINE)) as session:
                for as_of in MOMENTS:
                    seen[as_of] = await every_call(session, as_of)
            return seen
        for as_of, results in asyncio.run(go()).items():
            for name, data in results.items():
                for key, value in timestamps(data):
                    if key != "as_of_s":
                        with self.subTest(as_of=as_of, tool=name, key=key):
                            self.assertLessEqual(value, as_of)

    def test_asking_about_a_future_finding_gets_the_same_error_as_an_unknown_one(self) -> None:
        late = TIMELINE.as_of(99999).findings[-1]
        async def go():
            async with connected(store_of(TIMELINE)) as session:
                future = await call(session, "evidence_lookup", as_of_s=late.detected_at_s - 60,
                                    finding_id=late.finding_id)
                unknown = await call(session, "evidence_lookup", as_of_s=late.detected_at_s - 60,
                                     finding_id="made_up:goal:9")
                return future, unknown
        future, unknown = asyncio.run(go())
        self.assertTrue(future.isError and unknown.isError)
        strip = lambda r, i: r.content[0].text.replace(i, "ID")  # noqa: E731
        self.assertEqual(strip(future, late.finding_id), strip(unknown, "made_up:goal:9"))

    def test_the_event_id_bound_can_only_narrow_the_answer(self) -> None:
        async def go():
            async with connected(store_of(TIMELINE)) as session:
                loose = await call(session, "match_totals", as_of_s=1391.2)
                exact = await call(session, "match_totals", as_of_s=1391.2, as_of_event_id=0)
                return loose.structuredContent, exact.structuredContent
        loose, exact = asyncio.run(go())
        self.assertEqual(exact["data_through_s"], 0.0)
        self.assertLess(exact["data_through_s"], loose["data_through_s"])


class LoadingTests(unittest.TestCase):
    def test_the_default_store_holds_the_demo_match_and_specs_are_validated(self) -> None:
        self.assertEqual(load_store().ids(), ["aur_irv_comeback_0007"])
        self.assertEqual(parse_generate("3"), (3, "comeback"))
        self.assertEqual(parse_generate("4:siege"), (4, "siege"))
        for bad in ("x", "3:nonsense"):
            with self.assertRaises(ValueError):
                parse_generate(bad)


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #


class StdioTransportTests(unittest.TestCase):
    def test_agent_framework_can_use_the_server_over_stdio(self) -> None:
        """A real subprocess (`pitchside mcp`), driven by Agent Framework's own MCP client."""
        async def go():
            tool = MCPStdioTool(name="stats", command=sys.executable, load_prompts=False,
                                args=["-m", "pitchside.cli", "mcp", "--generate", "1:comeback"])
            async with tool:
                names = {f.name for f in tool.functions}
                result = await next(f for f in tool.functions if f.name == "match_totals").invoke(
                    arguments={"as_of_s": 2700.0})
                return names, json.loads("".join(c.text for c in result))
        names, data = asyncio.run(go())
        self.assertEqual(names, ALL_TOOLS)
        self.assertEqual(data["match_id"], MATCH_ID)
        self.assertEqual(data, json.loads(tools.match_totals(TIMELINE.as_of(2700.0)).model_dump_json()))


class StreamableHttpTransportTests(unittest.TestCase):
    """The Container Apps shape, in-process: the real ASGI app, the real HTTP transport, no sockets."""

    HOST = "pitchside-mcp.example.azurecontainerapps.io"

    def run_http(self, coro_factory, **options):
        async def go():
            server = build_server(store_of(TIMELINE), stateless_http=True, json_response=True,
                                  allowed_hosts=[self.HOST], **options)
            app = server.streamable_http_app()
            async with server.session_manager.run():
                return await coro_factory(app)
        return asyncio.run(go())

    def test_agent_framework_can_use_the_server_over_streamable_http(self) -> None:
        async def use(app):
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{self.HOST}")
            tool = MCPStreamableHTTPTool(name="stats", url=f"http://{self.HOST}/mcp", http_client=client,
                                         load_prompts=False)
            try:
                async with tool:
                    fn = next(f for f in tool.functions if f.name == "momentum")
                    result = await fn.invoke(arguments={"as_of_s": 4400.0})
                    return {f.name for f in tool.functions}, json.loads("".join(c.text for c in result))
            finally:
                await client.aclose()
        names, data = self.run_http(use)
        self.assertEqual(names, ALL_TOOLS)
        self.assertEqual(data, json.loads(tools.momentum(TIMELINE.as_of(4400.0)).model_dump_json()))

    def test_requests_for_other_hostnames_are_refused(self) -> None:
        async def probe(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                wrong = await client.post("http://evil.example/mcp", json=body, headers=headers)
                right = await client.post(f"http://{self.HOST}/mcp", json=body, headers=headers)
                return wrong.status_code, right.status_code
        wrong, right = self.run_http(probe)
        self.assertEqual((wrong, right), (421, 200))


if __name__ == "__main__":
    unittest.main()
