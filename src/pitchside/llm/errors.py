"""What callers see when a model call fails, and how raw failures are classified.

Agent Framework wraps every OpenAI SDK failure in a plain ``ChatClientException`` with the ``openai`` error as
its cause, so ``classify`` walks the cause chain to find out what really happened.

Callers catch ``LLMError`` and fall back to a deterministic template narrative (milestone 3), so offline,
over-budget and outage runs still produce output.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Literal, Optional, TypeVar

import openai
from agent_framework.exceptions import ChatClientContentFilterException, ChatClientInvalidAuthException
from azure.core.exceptions import ClientAuthenticationError
from pydantic import BaseModel, ValidationError

ErrorKind = Literal[
    "rate_limit", "timeout", "connection", "server_error", "auth", "not_found", "bad_request", "content_filter",
    "unknown",
]

AUTH_HINT = (
    "The identity running Pitchside could not call the Foundry deployment. Run `az login` (locally) or give the "
    "app a managed identity, and make sure that identity has the 'Cognitive Services OpenAI User' role on the "
    "Foundry resource (or 'Foundry User', formerly 'Azure AI User', on the project). Role assignments can take a "
    "few minutes to apply."
)
NOT_FOUND_HINT = "Check FOUNDRY_ENDPOINT (the project endpoint) and the MODEL_* deployment names in .env."


class LLMError(Exception):
    """Base class for every model-call failure a caller needs to handle."""

    kind: str = "unknown"

    def __init__(self, message: str, *, role: Optional[str] = None, kind: Optional[str] = None) -> None:
        super().__init__(message)
        self.role = role
        if kind is not None:
            self.kind = kind


class BudgetExhausted(LLMError):
    """A spending or call cap was reached; no request was sent."""

    kind = "budget"

    def __init__(self, message: str, *, scope: Literal["match", "match_calls", "total"], role: Optional[str] = None):
        super().__init__(message, role=role)
        self.scope = scope


class ServiceUnavailable(LLMError):
    """Retryable failures (rate limit, timeout, connection, 5xx) persisted after every retry."""


class RequestRejected(LLMError):
    """The service refused the request; retrying will not help (bad request, missing deployment, content filter)."""


class AuthFailed(RequestRejected):
    kind = "auth"


class MalformedOutput(LLMError):
    """The model answered, but not in the format the caller asked for."""

    kind = "malformed"


@dataclass(frozen=True)
class Classified:
    kind: ErrorKind
    retryable: bool
    retry_after_s: Optional[float] = None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or getattr(exc, "inner_exception", None)


def _retry_after(exc: openai.APIStatusError) -> Optional[float]:
    headers = exc.response.headers
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return max(float(value) / scale, 0.0)
        except ValueError:
            continue
    return None


def classify(exc: BaseException) -> Classified:
    """Map any exception from a chat client to a kind and whether retrying can help."""
    for e in _chain(exc):
        if isinstance(e, (openai.APITimeoutError, asyncio.TimeoutError, TimeoutError)):
            return Classified("timeout", True)
        if isinstance(e, openai.APIConnectionError):
            return Classified("connection", True)
        if isinstance(e, openai.RateLimitError):
            return Classified("rate_limit", True, _retry_after(e))
        if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError,
                          ClientAuthenticationError, ChatClientInvalidAuthException)):
            return Classified("auth", False)
        if isinstance(e, ChatClientContentFilterException):
            return Classified("content_filter", False)
        if isinstance(e, openai.NotFoundError):
            return Classified("not_found", False)
        if isinstance(e, openai.BadRequestError):
            return Classified("content_filter" if e.code == "content_filter" else "bad_request", False)
        if isinstance(e, openai.APIStatusError):
            if e.status_code >= 500 or e.status_code in (408, 409):
                return Classified("server_error", True, _retry_after(e))
            return Classified("bad_request", False)
    return Classified("unknown", False)


def to_llm_error(exc: BaseException, c: Classified, *, role: str, attempts: int) -> LLMError:
    """The exception a caller sees once we give up on a call."""
    tries = f"after {attempts} attempt{'s' if attempts != 1 else ''}"
    if c.kind == "auth":
        return AuthFailed(f"{role}: authentication failed ({_short(exc)}). {AUTH_HINT}", role=role)
    if c.kind == "not_found":
        return RequestRejected(f"{role}: deployment not found ({_short(exc)}). {NOT_FOUND_HINT}", role=role,
                               kind="not_found")
    if c.retryable:
        return ServiceUnavailable(f"{role}: model service unavailable {tries}: {c.kind} ({_short(exc)})",
                                  role=role, kind=c.kind)
    return RequestRejected(f"{role}: request rejected: {c.kind} ({_short(exc)})", role=role, kind=c.kind)


def _short(exc: BaseException, limit: int = 300) -> str:
    root = list(_chain(exc))[-1]
    text = f"{type(root).__name__}: {root}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


Model = TypeVar("Model", bound=BaseModel)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json(text: str, model: type[Model], *, role: Optional[str] = None) -> Model:
    """Parse a model reply into ``model``. Accepts a bare JSON object or one inside a ```json fence."""
    match = _FENCE.match(text or "")
    body = match.group(1) if match else (text or "").strip()
    try:
        return model.model_validate(json.loads(body))
    except (json.JSONDecodeError, ValidationError) as error:
        snippet = body[:120] + ("..." if len(body) > 120 else "")
        raise MalformedOutput(f"{role or 'model'}: reply is not a valid {model.__name__}: {snippet!r}",
                              role=role) from error
