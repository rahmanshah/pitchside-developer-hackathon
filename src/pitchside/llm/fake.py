"""Offline chat client for tests and local mode: deterministic replies, scriptable failures.

``FakeChatClient`` is a real Agent Framework chat client (same layers as ``OpenAIChatClient``), so agents and
middleware run against it unchanged. Failures are raised in exactly the shape the live client produces: a
``ChatClientException`` whose cause is a genuine ``openai`` error, so the classifier is tested against
production shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, Sequence, Union

import httpx2
import openai
from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    FunctionInvocationLayer,
    Message,
    UsageDetails,
)
from agent_framework.exceptions import ChatClientException, ChatClientInvalidRequestException

FailKind = Literal[
    "rate_limit", "timeout", "connection", "server_error", "auth", "not_found", "bad_request", "content_filter"
]
FAKE_URL = "https://fake.services.ai.azure.com/api/projects/pitchside/openai/v1/responses"


@dataclass(frozen=True)
class Reply:
    """A successful answer. Token counts default to a deterministic estimate (characters / 4)."""

    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: int = 0


@dataclass(frozen=True)
class Fail:
    kind: FailKind
    retry_after_s: Optional[float] = None


@dataclass(frozen=True)
class Malformed:
    """Answer text that will not parse as the JSON the caller asked for."""

    text: str = "Sure! Here is the narrative you asked for, as prose rather than JSON."


@dataclass(frozen=True)
class Delay:
    """Wait (e.g. past the guard's timeout), then do ``then`` (default: the normal reply)."""

    seconds: float
    then: Optional[Union[Reply, Fail, Malformed]] = None


Step = Union[Reply, Fail, Malformed, Delay]


@dataclass
class FakeCall:
    text: str
    options: dict[str, Any] = field(default_factory=dict)


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _transcript(messages: Sequence[Message]) -> str:
    return "\n".join(f"{m.role}: {m.text}" for m in messages)


def default_responder(role: str) -> Callable[[str], str]:
    def respond(transcript: str) -> str:
        digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:8]
        return f"[offline {role} reply {digest}]"

    return respond


def openai_error(kind: FailKind, retry_after_s: Optional[float] = None) -> openai.OpenAIError:
    """A genuine ``openai`` exception of the given kind, as the SDK would raise it."""
    request = httpx2.Request("POST", FAKE_URL)
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    if kind == "connection":
        return openai.APIConnectionError(request=request)
    status, cls, code = {
        "rate_limit": (429, openai.RateLimitError, "429"),
        "server_error": (503, openai.InternalServerError, "ServiceUnavailable"),
        "auth": (401, openai.AuthenticationError, "PermissionDenied"),
        "not_found": (404, openai.NotFoundError, "DeploymentNotFound"),
        "bad_request": (400, openai.BadRequestError, "invalid_request_error"),
        "content_filter": (400, openai.BadRequestError, "content_filter"),
    }[kind]
    headers = {"retry-after": str(retry_after_s)} if retry_after_s is not None else {}
    response = httpx2.Response(status, headers=headers, request=request)
    message = f"simulated {kind}"
    return cls(message, response=response, body={"code": code, "message": message})


class FakeChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Deterministic offline chat client. ``script`` steps are used in order, then ``responder`` takes over."""

    def __init__(
        self,
        role: str = "agent",
        *,
        script: Sequence[Step] = (),
        responder: Optional[Callable[[str], str]] = None,
        model: str = "fake-luna",
        sleep: Callable[[float], Any] = asyncio.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.role = role
        self.model = model
        self.script: list[Step] = list(script)
        self.responder = responder or default_responder(role)
        self.calls: list[FakeCall] = []
        self._sleep = sleep

    def add(self, *steps: Step) -> None:
        self.script.extend(steps)

    def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Any, **kwargs: Any):
        if stream:
            raise ChatClientInvalidRequestException("FakeChatClient does not stream")
        return self._respond(messages, dict(options or {}))

    async def _respond(self, messages: Sequence[Message], options: dict[str, Any]) -> ChatResponse:
        transcript = _transcript(messages)
        self.calls.append(FakeCall(transcript, options))
        step: Optional[Step] = self.script.pop(0) if self.script else None
        if isinstance(step, Delay):
            await self._sleep(step.seconds)
            step = step.then
        if isinstance(step, Fail):
            error = openai_error(step.kind, step.retry_after_s)
            raise ChatClientException(
                f"{type(self)} service failed to complete the prompt: {error}", inner_exception=error
            ) from error
        if isinstance(step, Malformed):
            step = Reply(step.text)
        reply = step if isinstance(step, Reply) else Reply(self.responder(transcript))
        usage = UsageDetails(
            input_token_count=reply.input_tokens if reply.input_tokens is not None else _tokens(transcript),
            output_token_count=reply.output_tokens if reply.output_tokens is not None else _tokens(reply.text),
            cache_read_input_token_count=reply.cached_tokens,
        )
        return ChatResponse(
            messages=[Message(role="assistant", contents=[reply.text])],
            model=self.model,
            usage_details=usage,
            finish_reason="stop",
        )
