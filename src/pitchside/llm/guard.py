"""The guard every model round-trip passes through: an Agent Framework ``ChatMiddleware``.

Per attempt: reserve worst-case cost against the budget, wait for a concurrency slot, call with a deadline,
log usage and cost, settle the budget. Retryable failures back off (exponential, full jitter, honouring
``retry-after``) outside the concurrency slot. When it gives up, the caller gets an ``LLMError``.

Callers say which run, match, finding, pipeline step and step attempt a call belongs to with ``call_context``;
the role is fixed per client. The match budget is kept per run of a match (``run_id``/``match_id``), so re-running
a match starts from a fresh match budget while the total budget still covers the whole log.

A reply cut off at the output cap (finish reason ``length``) is logged and then raised as ``MalformedOutput``
with kind ``truncated``. Failures injected on purpose (``faults.py``) are logged as such and never charged.
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

from .budget import BudgetGuard, Reservation, budget_key
from .errors import BudgetExhausted, LLMError, MalformedOutput, RequestRejected, classify, to_llm_error
from .prices import PRICE_TABLE_VERSION, Price, cost_usd, price_for
from .usage import UsageLog, UsageRecord

DEFAULT_MAX_OUTPUT_TOKENS = 1024
INJECTED = "pitchside_injected"  # attribute / additional property that marks a deliberately injected fault


@dataclass(frozen=True)
class CallContext:
    match_id: Optional[str] = None
    finding_id: Optional[str] = None
    run_id: Optional[str] = None
    step: Optional[str] = None  # pipeline step, e.g. "explain" or "recap:explain"
    attempt: Optional[int] = None  # attempt of that step (not the transport retry)


_CALL_CONTEXT: ContextVar[CallContext] = ContextVar("pitchside_call_context", default=CallContext())
_UNSET: Any = object()


def current_call_context() -> CallContext:
    return _CALL_CONTEXT.get()


@contextmanager
def call_context(match_id: Optional[str] = _UNSET, finding_id: Optional[str] = _UNSET, *,
                 run_id: Optional[str] = _UNSET, step: Optional[str] = _UNSET,
                 attempt: Optional[int] = _UNSET) -> Iterator[CallContext]:
    """Tag every model call made inside this block (in this task). Fields not given keep the enclosing value."""
    current = _CALL_CONTEXT.get()
    given = dict(match_id=match_id, finding_id=finding_id, run_id=run_id, step=step, attempt=attempt)
    merged = CallContext(**{k: (getattr(current, k) if v is _UNSET else v) for k, v in given.items()})
    token = _CALL_CONTEXT.set(merged)
    try:
        yield merged
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
                reservation = self.budget.reserve(budget_key(ctx.run_id, ctx.match_id), worst_case)
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
                self._record_success(context, ctx, attempt, reservation, latency_ms, max_out)
                return

            verdict = classify(error)
            injected = _injected(error)
            if injected:
                charged, detail = 0.0, f"injected fault: {injected} (never sent, not charged)"
            elif verdict.kind == "timeout":
                charged, detail = reservation.amount_usd, "timed out: charged the worst-case estimate (may be billed)"
            else:
                charged, detail = 0.0, None
            self.budget.settle(reservation, charged)
            self._write(ctx, attempt, "error", error_kind=verdict.kind, cost=charged, latency_ms=latency_ms,
                        detail=detail or _brief(error), injected=injected)
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
                        latency_ms: float, max_out: int) -> None:
        result = context.result
        model = getattr(result, "model", None)
        finish = getattr(result, "finish_reason", None)
        finish = str(finish) if finish is not None else None
        injected = (getattr(result, "additional_properties", None) or {}).get(INJECTED)
        usage = dict(getattr(result, "usage_details", None) or {})
        if injected and not usage:
            self.budget.settle(reservation, 0.0)
            self._write(ctx, attempt, "ok", model=model, latency_ms=latency_ms, finish_reason=finish,
                        injected=injected, detail=f"injected fault: {injected} (never sent, not charged)")
        elif not usage.get("input_token_count") and not usage.get("output_token_count"):
            # No usage reported: charge the worst case rather than log the call as free.
            self.budget.settle(reservation, reservation.amount_usd)
            self._write(ctx, attempt, "ok", model=model, cost=reservation.amount_usd, latency_ms=latency_ms,
                        finish_reason=finish, detail="no usage reported: charged the worst-case estimate")
        else:
            tokens_in = int(usage.get("input_token_count") or 0)
            cached = int(usage.get("cache_read_input_token_count") or 0)
            tokens_out = int(usage.get("output_token_count") or 0)
            cost = cost_usd(self.price, tokens_in, cached, tokens_out)
            self.budget.settle(reservation, cost)
            self._write(
                ctx, attempt, "ok", model=model, cost=cost, latency_ms=latency_ms, finish_reason=finish,
                injected=injected, input_tokens=tokens_in, cached_tokens=cached, output_tokens=tokens_out,
                reasoning_tokens=int(usage.get("reasoning_output_token_count") or 0),
            )
        if finish == "length":
            # Cut off at the cap: the JSON is incomplete. The caller decides whether to retry with a larger cap.
            raise MalformedOutput(f"{self.role}: reply truncated at the output cap (max_tokens={max_out})",
                                  role=self.role, kind="truncated")

    def _write(self, ctx: CallContext, attempt: int, status: str, *, error_kind: Optional[str] = None,
               detail: Optional[str] = None, model: Optional[str] = None, cost: float = 0.0,
               latency_ms: float = 0.0, finish_reason: Optional[str] = None, injected: Optional[str] = None,
               **tokens: int) -> None:
        self.log.append(UsageRecord(
            run_id=ctx.run_id, match_id=ctx.match_id, finding_id=ctx.finding_id, step=ctx.step,
            step_attempt=ctx.attempt, role=self.role, deployment=self.deployment, model=model, attempt=attempt,
            status=status, error_kind=error_kind, detail=detail, finish_reason=finish_reason, injected=injected,
            cost_usd=cost, latency_ms=round(latency_ms, 1), price_model=self.price_model,
            price_version=PRICE_TABLE_VERSION, **tokens,
        ))


def _injected(error: BaseException) -> Optional[str]:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, INJECTED, None):
            return getattr(error, INJECTED)
        error = error.__cause__ or getattr(error, "inner_exception", None)
    return None


def _brief(error: BaseException, limit: int = 200) -> str:
    text = f"{type(error).__name__}: {error}"
    return text if len(text) <= limit else text[: limit - 3] + "..."
