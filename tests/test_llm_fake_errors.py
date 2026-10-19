"""Tests for the fake chat client and error classification."""

from __future__ import annotations

import asyncio
import unittest

from agent_framework import Agent, Message
from agent_framework.exceptions import ChatClientException
from azure.core.exceptions import ClientAuthenticationError
from pydantic import BaseModel

from pitchside.llm.errors import (
    AUTH_HINT,
    AuthFailed,
    MalformedOutput,
    RequestRejected,
    ServiceUnavailable,
    classify,
    parse_json,
    to_llm_error,
)
from pitchside.llm.fake import Delay, Fail, FakeChatClient, Malformed, Reply


def ask(client: FakeChatClient, text: str = "hello"):
    return asyncio.run(client.get_response([Message(role="user", contents=[text])]))


class FakeClientTests(unittest.TestCase):
    def test_default_reply_is_deterministic_with_estimated_usage(self) -> None:
        a, b = ask(FakeChatClient("explainer")), ask(FakeChatClient("explainer"))
        self.assertEqual(a.text, b.text)
        self.assertTrue(a.text.startswith("[offline explainer reply"))
        self.assertNotEqual(a.text, ask(FakeChatClient("explainer"), "something else").text)
        self.assertGreater(a.usage_details["input_token_count"], 0)
        self.assertEqual(a.model, "fake-luna")

    def test_script_runs_in_order_then_falls_back(self) -> None:
        client = FakeChatClient(script=[Reply("one", input_tokens=10, output_tokens=2, cached_tokens=4), Reply("two")])
        first = ask(client)
        self.assertEqual(first.text, "one")
        self.assertEqual(first.usage_details["cache_read_input_token_count"], 4)
        self.assertEqual(ask(client).text, "two")
        self.assertIn("offline", ask(client).text)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("user: hello", client.calls[0].text)

    def test_failures_have_the_live_shape(self) -> None:
        client = FakeChatClient(script=[Fail("rate_limit", retry_after_s=3)])
        with self.assertRaises(ChatClientException) as ctx:
            ask(client)
        c = classify(ctx.exception)
        self.assertEqual((c.kind, c.retryable, c.retry_after_s), ("rate_limit", True, 3.0))

    def test_malformed_and_delay_steps(self) -> None:
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        client = FakeChatClient(script=[Malformed(), Delay(5, Reply("late"))], sleep=fake_sleep)
        self.assertIn("prose rather than JSON", ask(client).text)
        self.assertEqual(ask(client).text, "late")
        self.assertEqual(slept, [5])

    def test_runs_under_a_real_agent(self) -> None:
        client = FakeChatClient("verifier", script=[Reply("APPROVED")])
        agent = Agent(client=client, name="verifier", instructions="Check the claim.")
        result = asyncio.run(agent.run("Claim: 3 shots."))
        self.assertEqual(result.text, "APPROVED")
        self.assertIn("Check the claim.", client.calls[0].text + str(client.calls[0].options))


class ClassifyTests(unittest.TestCase):
    def kind_of(self, kind: str):
        try:
            ask(FakeChatClient(script=[Fail(kind)]))
        except ChatClientException as error:
            return classify(error)
        self.fail("no error raised")

    def test_retryable_kinds(self) -> None:
        for kind in ("rate_limit", "timeout", "connection", "server_error"):
            with self.subTest(kind):
                c = self.kind_of(kind)
                self.assertEqual(c.kind, kind)
                self.assertTrue(c.retryable)

    def test_permanent_kinds(self) -> None:
        for kind in ("auth", "not_found", "bad_request", "content_filter"):
            with self.subTest(kind):
                c = self.kind_of(kind)
                self.assertEqual(c.kind, kind)
                self.assertFalse(c.retryable)

    def test_credential_failure_and_outer_timeout(self) -> None:
        self.assertEqual(classify(ClientAuthenticationError("DefaultAzureCredential failed")).kind, "auth")
        self.assertEqual(classify(asyncio.TimeoutError()).kind, "timeout")
        self.assertEqual(classify(RuntimeError("boom")), classify(ValueError("x")))
        self.assertFalse(classify(RuntimeError("boom")).retryable)

    def test_auth_error_tells_the_user_what_to_do(self) -> None:
        exc = ChatClientException("wrapped")
        error = to_llm_error(exc, classify(ClientAuthenticationError("no login")), role="explainer", attempts=1)
        self.assertIsInstance(error, AuthFailed)
        self.assertIsInstance(error, RequestRejected)
        for needle in ("az login", "Cognitive Services OpenAI User", "explainer"):
            self.assertIn(needle, str(error))
        self.assertIn(AUTH_HINT, str(error))

    def test_given_up_errors(self) -> None:
        c = self.kind_of("server_error")
        error = to_llm_error(RuntimeError("503"), c, role="verifier", attempts=4)
        self.assertIsInstance(error, ServiceUnavailable)
        self.assertEqual(error.kind, "server_error")
        self.assertIn("after 4 attempts", str(error))
        nf = to_llm_error(RuntimeError("404"), self.kind_of("not_found"), role="verifier", attempts=1)
        self.assertIn("MODEL_", str(nf))


class Verdict(BaseModel):
    approved: bool
    reason: str


class ParseJsonTests(unittest.TestCase):
    def test_plain_and_fenced_json(self) -> None:
        self.assertTrue(parse_json('{"approved": true, "reason": "ok"}', Verdict).approved)
        fenced = '```json\n{"approved": false, "reason": "no evidence"}\n```'
        self.assertEqual(parse_json(fenced, Verdict).reason, "no evidence")

    def test_malformed_output_raises_with_a_snippet(self) -> None:
        for text in ("Sure! here you go", '{"approved": "maybe"}', ""):
            with self.subTest(text):
                with self.assertRaises(MalformedOutput):
                    parse_json(text, Verdict, role="verifier")


if __name__ == "__main__":
    unittest.main()
