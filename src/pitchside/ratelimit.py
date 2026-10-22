"""Request rate limit and access log for the MCP server over HTTP (an ASGI middleware, no dependencies).

Azure Container Apps has no built-in request rate limiting, so the deployed MCP server limits itself: a token
bucket per client plus one global bucket, answering 429 with ``Retry-After`` when either is empty. The app runs as
one replica (``maxReplicas: 1``), so in-memory buckets are the whole story. The client is the peer address, or,
behind a trusted proxy (``trust_forwarded``, the Container Apps ingress), the address that proxy appended last to
``X-Forwarded-For``. Paths in ``exempt`` (the health probe) are never limited.

Measured: a whole storyline match makes about 10 MCP requests with the offline interpreter (tests count them); the
defaults leave room for a live model that calls tools several times per moment.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, MutableMapping, Optional, Sequence

Scope = MutableMapping
ASGIApp = Callable[[Scope, Callable, Callable], Awaitable[None]]

DEFAULT_PER_MINUTE = 120.0
DEFAULT_BURST = 60
DEFAULT_GLOBAL_PER_MINUTE = 300.0
log = logging.getLogger("pitchside.http")


@dataclass
class Bucket:
    """Token bucket: ``capacity`` tokens, refilled at ``per_minute`` tokens per minute."""

    per_minute: float
    capacity: float
    tokens: float = field(default=-1.0)
    updated: float = 0.0

    def take(self, now: float) -> float:
        """Take one token; return 0 if allowed, else the seconds until one is available."""
        if self.tokens < 0:
            self.tokens, self.updated = self.capacity, now
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.per_minute / 60.0)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) * 60.0 / self.per_minute


class RateLimit:
    def __init__(self, app: ASGIApp, *, per_minute: float = DEFAULT_PER_MINUTE, burst: int = DEFAULT_BURST,
                 global_per_minute: float = DEFAULT_GLOBAL_PER_MINUTE, trust_forwarded: bool = False,
                 exempt: Sequence[str] = ("/healthz",), clock: Callable[[], float] = time.monotonic,
                 max_clients: int = 10_000) -> None:
        if per_minute <= 0 or burst < 1 or global_per_minute <= 0:
            raise ValueError("rate limits must be positive")
        self.app = app
        self.per_minute, self.burst = per_minute, burst
        self.global_bucket = Bucket(global_per_minute, max(burst, global_per_minute / 2))
        self.trust_forwarded = trust_forwarded
        self.exempt = tuple(exempt)
        self.clock = clock
        self.max_clients = max_clients
        self.buckets: dict[str, Bucket] = {}
        self.limited = 0

    def client_of(self, scope: Scope) -> str:
        if self.trust_forwarded:
            for name, value in scope.get("headers") or []:
                if name == b"x-forwarded-for":
                    hops = [h.strip() for h in value.decode("latin-1").split(",") if h.strip()]
                    if hops:
                        return hops[-1]
        client = scope.get("client")
        return client[0] if client else "unknown"

    def bucket(self, key: str, now: float) -> Bucket:
        if key not in self.buckets and len(self.buckets) >= self.max_clients:  # bound memory: drop the stalest
            del self.buckets[min(self.buckets, key=lambda k: self.buckets[k].updated)]
        return self.buckets.setdefault(key, Bucket(self.per_minute, float(self.burst)))

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # lifespan passes through (the MCP session manager needs it)
            return
        path = scope.get("path", "")
        client = self.client_of(scope)
        start = self.clock()
        wait = 0.0
        if not path.startswith(self.exempt):
            wait = max(self.bucket(client, start).take(start), self.global_bucket.take(start))
        if wait > 0:
            self.limited += 1
            await self._too_many(send, wait)
            _access(scope, client, 429, start, self.clock())
            return
        status: list[Optional[int]] = [None]

        async def tracked_send(message: MutableMapping) -> None:
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        finally:
            _access(scope, client, status[0], start, self.clock())

    @staticmethod
    async def _too_many(send: Callable, wait: float) -> None:
        body = json.dumps({"error": "rate_limited", "retry_after_s": round(wait, 1)}).encode()
        await send({"type": "http.response.start", "status": 429,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", str(max(1, round(wait))).encode()),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _access(scope: Scope, client: str, status: Optional[int], start: float, end: float) -> None:
    """One JSON line per request (Container Apps sends stdout/stderr to Log Analytics)."""
    if log.isEnabledFor(logging.INFO):
        log.info(json.dumps({"method": scope.get("method"), "path": scope.get("path"), "status": status,
                             "client": client, "ms": round((end - start) * 1000, 1)}))
