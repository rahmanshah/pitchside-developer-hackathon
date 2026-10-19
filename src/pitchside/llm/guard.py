"""The guard every model round-trip passes through: an Agent Framework ``ChatMiddleware``.

Per attempt: reserve worst-case cost against the budget, wait for a concurrency slot, call with a deadline,
log usage and cost, settle the budget. Retryable failures back off (exponential, full jitter, honouring
``retry-after``) outside the concurrency slot. When it gives up, the caller gets an ``LLMError``.

Callers say which match and finding a call belongs to with ``call_context``; the role is fixed per client.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import weakref
from collections.abc import MutableMapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Optional

from agent_framework import ChatContext, ChatMiddleware

from .budget import BudgetGuard, Reservation
from .errors import BudgetExhausted, LLMError, RequestRejected, classify, to_llm_error
from .prices import PRICE_TABLE_VERSION, Price, cost_usd, price_for
from .usage import UsageLog, UsageRecord

DEFAULT_MAX_OUTPUT_TOKENS = 1024


@dataclass(frozen=True)
class CallContext:
    match_id: Optional[str] = None
    finding_id: Optional[str] = None


_CALL_CONTEXT: ContextVar[CallContext] = ContextVar("pitchside_call_context", default=CallContext())


@contextmanager
def call_context(match_id: Optional[str] = None, finding_id: Optional[str] = None) -> Iterator[CallContext]:
    """Tag every model call made inside this block (in this task) with a match and finding."""
    token = _CALL_CONTEXT.set(CallContext(match_id, finding_id))
    try:
        yield _CALL_CONTEXT.get()
    finally:
        _CALL_CONTEXT.reset(token)


class ConcurrencyLimiter:
    """At most ``limit`` model calls in flight, shared by every agent. Works across event loops (one per test)."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("concurrency limit must be at least 1")
        self.limit = limit
        self.in_flight = 0
        self.peak = 0
        self._semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
            weakref.WeakKeyDictionary()
        )

    @asynccontextmanager
    async def slot(self):
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.setdefault(loop, asyncio.Semaphore(self.limit))
        async with semaphore:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                yield
            finally:
                self.in_flight -= 1


def backoff_delay(attempt: int, rng: random.Random, *, base_s: float, cap_s: float,
                  retry_after_s: Optional[float] = None) -> float:
    """Full-jitter exponential backoff for retry number ``attempt`` (1-based); never sooner than retry-after."""
    delay = rng.uniform(0, min(cap_s, base_s * 2 ** (attempt - 1)))
    if retry_after_s is not None:
        delay = max(delay, min(retry_after_s, cap_s))
    return delay


def _estimate_tokens(context: ChatContext) -> int:
    text = sum(len(m.text or "") for m in context.messages)
    text += len(str((context.options or {}).get("instructions") or ""))
    return max(1, math.ceil(text / 4))


class ModelGuard(ChatMiddleware):
    def __init__(
        self,
        *,
        role: str,
        deployment: str,
        log: UsageLog,
        budget: BudgetGuard,
        limiter: ConcurrencyLimiter,
        price_model: str = "gpt-6-luna",
        timeout_s: float = 20.0,
        max_retries: int = 3,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 20.0,
        rng: Optional[random.Random] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.role = role
        self.deployment = deployment
        self.log = log
        self.budget = budget
        self.limiter = limiter
        self.price_model = price_model
        self.price: Price = price_for(price_model)  # fails closed at construction for unknown models
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.clock = clock

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if context.stream:
            raise RequestRejected(f"{self.role}: streaming calls are not supported", role=self.role,
                                  kind="bad_request")
        ctx = _CALL_CONTEXT.get()
        max_out = self._cap_output(context)
        worst_case = cost_usd(self.price, _estimate_tokens(context), 0, max_out)

        attempt = 0
        while True:
            attempt += 1
            try:
                reservation = self.budget.reserve(ctx.match_id, worst_case)
            except BudgetExhausted as refusal:
                refusal.role = self.role
                self._write(ctx, attempt, "refused", error_kind="budget", detail=str(refusal))
                raise

            async with self.limiter.slot():
                start = self.clock()
                try:
                    await asyncio.wait_for(call_next(), timeout=self.timeout_s)
                    error: Optional[BaseException] = None
                except LLMError:
                    self.budget.settle(reservation, 0.0)
                    raise
                except Exception as exc:  # noqa: BLE001 - every failure is classified below
                    error = exc
                latency_ms = (self.clock() - start) * 1000

            if error is None:
                self._record_success(context, ctx, attempt, reservation, latency_ms)
                return

            verdict = classify(error)
            if verdict.kind == "timeout":
                charged, detail = reservation.amount_usd, "timed out: charged the worst-case estimate (may be billed)"
            else:
                charged, detail = 0.0, None
            self.budget.settle(reservation, charged)
            self._write(ctx, attempt, "error", error_kind=verdict.kind, cost=charged, latency_ms=latency_ms,
                        detail=detail or _brief(error))
            if not verdict.retryable or attempt > self.max_retries:
                raise to_llm_error(error, verdict, role=self.role, attempts=attempt) from error
            await self.sleep(backoff_delay(attempt, self.rng, base_s=self.backoff_base_s,
                                           cap_s=self.backoff_cap_s, retry_after_s=verdict.retry_after_s))

    def _cap_output(self, context: ChatContext) -> int:
        """Make sure every call has an output-token cap, so the worst-case reservation is a real bound."""
        options = context.options
        requested = (options or {}).get("max_tokens")
        if requested:
            return int(requested)
        if isinstance(options, MutableMapping):
            options["max_tokens"] = self.max_output_tokens
        elif options is None:
            context.options = {"max_tokens": self.max_output_tokens}
        return self.max_output_tokens

    def _record_success(self, context: ChatContext, ctx: CallContext, attempt: int, reservation: Reservation,
                        latency_ms: float) -> None:
        result = context.result
        usage = dict(getattr(result, "usage_details", None) or {})
        if not usage.get("input_token_count") and not usage.get("output_token_count"):
            # No usage reported: charge the worst case rather than log the call as free.
            self.budget.settle(reservation, reservation.amount_usd)
            self._write(ctx, attempt, "ok", model=getattr(result, "model", None), cost=reservation.amount_usd,
                        latency_ms=latency_ms, detail="no usage reported: charged the worst-case estimate")
            return
        tokens_in = int(usage.get("input_token_count") or 0)
        cached = int(usage.get("cache_read_input_token_count") or 0)
        tokens_out = int(usage.get("output_token_count") or 0)
        cost = cost_usd(self.price, tokens_in, cached, tokens_out)
        self.budget.settle(reservation, cost)
        self._write(
            ctx, attempt, "ok", model=getattr(result, "model", None), cost=cost, latency_ms=latency_ms,
            input_tokens=tokens_in, cached_tokens=cached, output_tokens=tokens_out,
            reasoning_tokens=int(usage.get("reasoning_output_token_count") or 0),
        )

    def _write(self, ctx: CallContext, attempt: int, status: str, *, error_kind: Optional[str] = None,
               detail: Optional[str] = None, model: Optional[str] = None, cost: float = 0.0,
               latency_ms: float = 0.0, **tokens: int) -> None:
        self.log.append(UsageRecord(
            match_id=ctx.match_id, finding_id=ctx.finding_id, role=self.role, deployment=self.deployment,
            model=model, attempt=attempt, status=status, error_kind=error_kind, detail=detail, cost_usd=cost,
            latency_ms=round(latency_ms, 1), price_model=self.price_model, price_version=PRICE_TABLE_VERSION,
            **tokens,
        ))


def _brief(error: BaseException, limit: int = 200) -> str:
    text = f"{type(error).__name__}: {error}"
    return text if len(text) <= limit else text[: limit - 3] + "..."
