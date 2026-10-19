"""Live-client wiring, tested offline: construction with a dummy credential, and the real Agent Framework
OpenAI clients talking to an in-memory HTTP transport (no sockets)."""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import unittest
from pathlib import Path

import httpx2
from agent_framework import Message
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from openai import AsyncOpenAI

from pitchside.llm import Role, load_settings
from pitchside.llm.budget import BudgetGuard
from pitchside.llm.errors import AuthFailed
from pitchside.llm.factory import build_clients
from pitchside.llm.fake import FakeChatClient
from pitchside.llm.foundry import chat_client, connect
from pitchside.llm.guard import ConcurrencyLimiter, ModelGuard, call_context
from pitchside.llm.usage import UsageLog

ENDPOINT = "https://acct.services.ai.azure.com/api/projects/pitchside"


def live_env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {
        "PITCHSIDE_LLM": "foundry", "FOUNDRY_ENDPOINT": ENDPOINT, "MODEL_INTERPRETER": "luna-i",
        "MODEL_EXPLAINER": "luna-e", "MODEL_VERIFIER": "luna-v", "MODEL_PERSONALIZER": "luna-p",
        "PITCHSIDE_USAGE_LOG": str(tmp / "usage.jsonl"),
    }
    env.update(extra)
    return env


class DummyCredential:
    """Never asked for a token: nothing in these tests reaches the service."""

    async def get_token(self, *scopes, **kwargs):
        raise AssertionError("offline test asked for a token")

    async def close(self) -> None:
        pass


RESPONSES_OK = {
    "id": "resp_1", "object": "response", "created_at": 1760000000, "status": "completed",
    "model": "gpt-6-luna-2026-09-01", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    "output": [{
        "type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": "The siege lasted nine minutes.", "annotations": []}],
    }],
    "usage": {
        "input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000},
        "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 8}, "total_tokens": 1240,
    },
}
CHAT_OK = {
    "id": "chatcmpl_1", "object": "chat.completion", "created": 1760000000, "model": "gpt-6-luna-2026-09-01",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "The siege lasted nine minutes."}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240,
              "prompt_tokens_details": {"cached_tokens": 1000}},
}


class Server:
    """In-memory OpenAI-compatible endpoint: replies in order, records requests."""

    def __init__(self, *replies: httpx2.Response) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((request.url.path, json.loads(request.content or b"{}")))
        return self.replies.pop(0)

    def client(self) -> AsyncOpenAI:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        return AsyncOpenAI(base_url=f"{ENDPOINT}/openai/v1/", api_key="unused-offline", http_client=http,
                           max_retries=0)


class ConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_connection_targets_the_project_v1_route_without_sdk_retries(self) -> None:
        settings = load_settings(live_env(self.dir, LLM_TIMEOUT_S="12.5"))
        conn = connect(settings, credential=DummyCredential())
        self.assertEqual(str(conn.openai.base_url), f"{ENDPOINT}/openai/v1/")
        self.assertEqual(conn.openai.max_retries, 0, "the guard owns retries")
        self.assertEqual(conn.openai.timeout, 12.5)
        asyncio.run(conn.aclose())

    def test_build_clients_live_uses_one_connection_and_per_role_deployments(self) -> None:
        models = build_clients(load_settings(live_env(self.dir)), credential=DummyCredential())
        self.assertEqual(set(models.clients), set(Role))
        for role, deployment in ((Role.EXPLAINER, "luna-e"), (Role.VERIFIER, "luna-v")):
            client = models[role]
            self.assertIsInstance(client, OpenAIChatClient)
            self.assertEqual(client.model, deployment)
            self.assertIs(client.client, models.connection.openai)
            self.assertEqual(models.guards[role].deployment, deployment)
        asyncio.run(models.aclose())

    def test_chat_completions_fallback_is_selectable(self) -> None:
        settings = load_settings(live_env(self.dir, FOUNDRY_API="chat_completions"))
        models = build_clients(settings, credential=DummyCredential())
        self.assertIsInstance(models[Role.EXPLAINER], OpenAIChatCompletionClient)
        asyncio.run(models.aclose())

    def test_build_clients_fake_by_default(self) -> None:
        models = build_clients(load_settings({"PITCHSIDE_USAGE_LOG": str(self.dir / "u.jsonl")}))
        self.assertIsNone(models.connection)
        self.assertTrue(all(isinstance(c, FakeChatClient) for c in models.clients.values()))
        self.assertTrue(all(g.budget is models.budget and g.limiter is models.limiter for g in models.guards.values()))

        async def run():
            with call_context(match_id="m1"):
                return await models[Role.INTERPRETER].get_response([Message(role="user", contents=["hi"])])

        self.assertIn("offline interpreter", asyncio.run(run()).text)
        self.assertEqual([r.role for r in models.log.read()], ["interpreter"])


class WireTests(unittest.TestCase):
    """The real Agent Framework clients and our guard, against canned HTTP responses."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = UsageLog(Path(self.tmp.name) / "usage.jsonl")
        budget = BudgetGuard(self.log, match_budget_usd=1, match_max_calls=10, total_budget_usd=1)
        self.slept: list[float] = []

        async def sleep(s: float) -> None:
            self.slept.append(s)

        self.guard = ModelGuard(role="explainer", deployment="luna-e", log=self.log, budget=budget,
                                limiter=ConcurrencyLimiter(4), rng=random.Random(1), sleep=sleep,
                                max_output_tokens=256)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ask(self, cls, server: Server):
        client = cls(model="luna-e", async_client=server.client(), middleware=[self.guard])

        async def run():
            with call_context(match_id="m1", finding_id="m1:siege:0"):
                return await client.get_response([Message(role="user", contents=["Explain the siege."])])

        return asyncio.run(run())

    def test_responses_usage_including_cached_tokens_is_logged(self) -> None:
        server = Server(httpx2.Response(200, json=RESPONSES_OK))
        result = self.ask(OpenAIChatClient, server)
        self.assertEqual(result.text, "The siege lasted nine minutes.")
        path, body = server.requests[0]
        self.assertTrue(path.endswith("/openai/v1/responses"))
        self.assertEqual(body["model"], "luna-e")
        self.assertEqual(body.get("max_output_tokens"), 256, "the guard's output cap reaches the wire")
        [r] = self.log.read()
        self.assertEqual((r.input_tokens, r.cached_tokens, r.output_tokens, r.reasoning_tokens), (1200, 1000, 40, 8))
        self.assertEqual(r.model, "gpt-6-luna-2026-09-01")
        self.assertAlmostEqual(r.cost_usd, (200 * 0.10 + 1000 * 0.01 + 40 * 0.50) / 1e6)

    def test_chat_completions_usage_is_logged(self) -> None:
        server = Server(httpx2.Response(200, json=CHAT_OK))
        self.ask(OpenAIChatCompletionClient, server)
        path, body = server.requests[0]
        self.assertTrue(path.endswith("/openai/v1/chat/completions"))
        [r] = self.log.read()
        self.assertEqual((r.input_tokens, r.output_tokens), (1200, 40))
        self.assertEqual(r.cached_tokens, 1000)

    def test_rate_limit_from_the_service_is_retried(self) -> None:
        server = Server(
            httpx2.Response(429, headers={"retry-after": "2"}, json={"error": {"code": "429", "message": "slow"}}),
            httpx2.Response(200, json=RESPONSES_OK),
        )
        self.assertIn("siege", self.ask(OpenAIChatClient, server).text)
        self.assertEqual([r.status for r in self.log.read()], ["error", "ok"])
        self.assertGreaterEqual(self.slept[0], 2)

    def test_auth_failure_from_the_service_explains_the_fix(self) -> None:
        server = Server(httpx2.Response(401, json={"error": {"code": "PermissionDenied", "message": "no"}}))
        with self.assertRaises(AuthFailed) as ctx:
            self.ask(OpenAIChatClient, server)
        self.assertIn("az login", str(ctx.exception))
        self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
