"""Build one guarded chat client per agent role, fake or Foundry, sharing one log, budget and limiter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .budget import BudgetGuard
from .config import LLMSettings, Role, load_settings
from .fake import FakeChatClient, Step
from .guard import ConcurrencyLimiter, ModelGuard
from .usage import UsageLog


@dataclass
class ModelClients:
    settings: LLMSettings
    clients: dict[Role, Any]
    guards: dict[Role, ModelGuard]
    log: UsageLog
    budget: BudgetGuard
    limiter: ConcurrencyLimiter
    connection: Optional[Any] = None  # foundry.FoundryConnection in live mode

    def __getitem__(self, role: Role) -> Any:
        return self.clients[role]

    async def aclose(self) -> None:
        if self.connection is not None:
            await self.connection.aclose()


def build_clients(
    settings: Optional[LLMSettings] = None,
    *,
    fake_scripts: Optional[Mapping[Role, Sequence[Step]]] = None,
    credential: Optional[Any] = None,
    **guard_kwargs: Any,
) -> ModelClients:
    """Clients for every role. ``PITCHSIDE_LLM=fake`` (default) never touches the network."""
    settings = settings or load_settings()
    log = UsageLog(settings.usage_log)
    budget = BudgetGuard(
        log,
        match_budget_usd=settings.match_budget_usd,
        match_max_calls=settings.match_max_calls,
        total_budget_usd=settings.total_budget_usd,
    )
    limiter = ConcurrencyLimiter(settings.max_concurrency)
    live = settings.mode == "foundry"
    connection = None
    if live:
        from . import foundry  # imported lazily: offline mode needs no Azure SDK set-up

        connection = foundry.connect(settings, credential)

    clients: dict[Role, Any] = {}
    guards: dict[Role, ModelGuard] = {}
    for role in Role:
        deployment = settings.deployment(role) if live else (getattr(settings, f"model_{role.value}") or "fake")
        guard = ModelGuard(
            role=role.value, deployment=deployment, log=log, budget=budget, limiter=limiter,
            price_model=settings.price_model, timeout_s=settings.timeout_s, max_retries=settings.max_retries,
            **guard_kwargs,
        )
        guards[role] = guard
        if live:
            clients[role] = foundry.chat_client(connection, deployment, api=settings.foundry_api, middleware=[guard])
        else:
            script = (fake_scripts or {}).get(role, ())
            clients[role] = FakeChatClient(role.value, script=script, middleware=[guard])
    return ModelClients(settings, clients, guards, log, budget, limiter, connection)
