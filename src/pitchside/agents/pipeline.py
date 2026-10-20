"""The director: run a whole match through the stats engine and the agents. Plain code, not a model.

It walks the match event by event, freezes each finding's context, sends model-eligible findings (and the two
recaps) through the job workflow, at most four at a time, and gives every other finding a template narrative
with the reason. Before a recap it waits for every earlier job, so the recap is built from all claims verified
so far. Records come back in match order, whatever order the jobs finished in.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from ..llm.config import LLMSettings
from ..llm.factory import ModelClients, build_clients
from ..llm.guard import call_context
from ..schema import Event
from ..stats.engine import Snapshot
from ..stats.findings import LLM_TRIGGER_SEVERITY, Finding
from .contracts import AuditEntry, Job, JobRecord, MatchFacts, Narrative
from .fake_agents import RESPONDERS
from .faults import FaultInjector, FaultPlan
from .jobs import FindingPoint, RecapPoint, StartPoint, StatsPoint, walk_match
from .languages import Language, resolve_languages
from .mcp_tools import McpTools
from .roles import AgentSet, build_agents
from .templates import template_narrative
from .workflow import MAX_ATTEMPTS, RunFlags, run_job

MAX_PARALLEL_JOBS = 4


@dataclass(frozen=True)
class RunConfig:
    languages: tuple[Language, ...]
    featured: Optional[tuple[str, ...]] = None
    max_attempts: int = MAX_ATTEMPTS
    max_parallel_jobs: int = MAX_PARALLEL_JOBS
    strict: bool = False  # re-raise unexpected errors instead of falling back (tests)

    @classmethod
    def from_settings(cls, settings: LLMSettings, **overrides: Any) -> "RunConfig":
        return cls(languages=resolve_languages(settings.language_codes), **overrides)


@dataclass
class MatchRun:
    run_id: str
    match_id: str
    facts: MatchFacts
    featured: list[str]
    events: list[Event]
    findings: list[Finding]
    snapshots: list[Snapshot]
    records: list[JobRecord]
    settings: LLMSettings
    config: RunConfig
    fault_plan: Optional[FaultPlan]
    faults_fired: list[dict[str, Any]]
    budget_exhausted: Optional[str]
    started_utc: str
    wall_s: float
    usage_log: Any = None  # the UsageLog the run wrote to
    answer_key: Optional[dict[str, Any]] = None
    tools_location: Optional[str] = None  # where the MCP server was ("in-process", or its URL); None = local tools

    def record(self, job_id: str) -> Optional[JobRecord]:
        return next((r for r in self.records if r.job_id == job_id), None)


@dataclass
class Pipeline:
    """Model clients, agents and (optionally) a fault injector for one run."""

    models: ModelClients
    agents: AgentSet
    injector: Optional[FaultInjector] = None
    tools: Optional[McpTools] = None  # the interpreter's MCP tools, when PITCHSIDE_TOOLS=mcp

    async def aclose(self) -> None:
        try:
            if self.tools is not None:
                await self.tools.aclose()
        finally:
            await self.models.aclose()


def build_pipeline(settings: LLMSettings, fault_plan: Optional[FaultPlan] = None, *,
                   mcp_http_client: Optional[Any] = None, **guard_kwargs: Any) -> Pipeline:
    """Fake agents offline, Foundry deployments live; faults are injected only if a plan is given.

    With ``PITCHSIDE_TOOLS=mcp`` the interpreter uses MCP tools: an in-process server, or the one at
    ``PITCHSIDE_MCP_URL`` (``mcp_http_client`` lets a test route that URL to an in-process app).
    """
    injector = FaultInjector(fault_plan) if fault_plan else None
    if settings.mode == "fake":
        guard_kwargs = {"backoff_base_s": 0.0, "backoff_cap_s": 0.0, **guard_kwargs}  # no real waiting offline
    models = build_clients(
        settings,
        fake_responders=RESPONDERS if settings.mode == "fake" else None,
        extra_middleware=(lambda role: [injector.middleware(role.value)]) if injector else (),
        **guard_kwargs,
    )
    tools = McpTools(settings.mcp_url, http_client=mcp_http_client) if settings.tools == "mcp" else None
    return Pipeline(models, build_agents(models, tools), injector, tools)


def new_run_id(seed_text: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{hashlib.sha256(f'{stamp}{seed_text}{time.perf_counter_ns()}'.encode()).hexdigest()[:6]}"


def template_record(job: Job, languages: Sequence[Language], reason: str) -> JobRecord:
    out = template_narrative(job, languages, reason=reason)
    narrative = Narrative(source="template", reason=reason, headline=out.headline, claims=out.claims,
                          semantic_check="not_run", texts=out.texts)
    audit = [AuditEntry(job_id=job.job_id, seq=1, step="template", actor="director", outcome="template",
                        detail=reason, data={"claims": [c.model_dump(mode="json") for c in out.claims]})]
    return JobRecord(job_id=job.job_id, kind=job.kind, label=job.label,
                     finding_id=job.finding.finding_id if job.finding else None, team_id=job.team_id,
                     clock_s=job.clock_s, route="template", ledger=job.ledger, drafts=[], checks=[],
                     narrative=narrative, audit=audit)


def _template_reason(finding: Finding) -> str:
    if finding.status == "end":
        return "spell end: always a template (decision 12)"
    return f"severity {finding.severity:.2f} is below the model floor {LLM_TRIGGER_SEVERITY}"


async def run_match(events: Iterable[Event], pipeline: Pipeline, config: RunConfig, *, run_id: str,
                    answer_key: Optional[dict[str, Any]] = None) -> MatchRun:
    """Run a whole match. MCP tools (if used) are opened and closed here, in this task."""
    events = list(events)
    if pipeline.tools is not None:
        pipeline.tools.register_match(events)
        await pipeline.tools.open()
    try:
        return await _run_match(events, pipeline, config, run_id, answer_key)
    finally:
        if pipeline.tools is not None:
            await pipeline.tools.aclose()


async def _run_match(events: list[Event], pipeline: Pipeline, config: RunConfig, run_id: str,
                     answer_key: Optional[dict[str, Any]]) -> MatchRun:
    started, clock = datetime.now(timezone.utc).isoformat(timespec="seconds"), time.perf_counter()
    flags = RunFlags()
    slots = asyncio.Semaphore(config.max_parallel_jobs)
    ordered: dict[int, JobRecord] = {}
    pending: dict[int, asyncio.Task] = {}
    findings: list[Finding] = []
    snapshots: list[Snapshot] = []
    facts: Optional[MatchFacts] = None
    featured: list[str] = []

    async def model_job(order: int, job: Job) -> None:
        async with slots:
            if flags.budget_exhausted:
                ordered[order] = template_record(job, config.languages,
                                                 f"budget exhausted earlier in this run: {flags.budget_exhausted}")
                return
            with call_context(run_id=run_id, match_id=job.match_id, finding_id=job.job_id, step=None, attempt=None):
                try:
                    ordered[order] = await run_job(job, pipeline.agents, config.languages, flags, config.max_attempts)
                except Exception as error:  # noqa: BLE001 - a bug in one job must not stop the match
                    if config.strict:
                        raise
                    ordered[order] = template_record(job, config.languages,
                                                     f"pipeline error: {type(error).__name__}: {error}")

    async def drain() -> None:
        if pending:
            await asyncio.gather(*pending.values())
            pending.clear()

    order = 0
    for point in walk_match(events, config.featured):
        if isinstance(point, StartPoint):
            facts, featured = point.facts, point.featured
        elif isinstance(point, StatsPoint):
            snapshots.append(point.snapshot)
        elif isinstance(point, FindingPoint):
            order += 1
            findings.append(point.finding)
            if point.route == "model":
                if pipeline.injector:
                    pipeline.injector.register(point.job)  # in match order, so goal.2 is the second goal
                pending[order] = asyncio.create_task(model_job(order, point.job))
            else:
                ordered[order] = template_record(point.job, config.languages, _template_reason(point.finding))
        elif isinstance(point, RecapPoint):
            await drain()
            order += 1
            records = [ordered[k] for k in sorted(ordered)]
            job = point.make(records)
            if pipeline.injector:
                pipeline.injector.register(job)
            await model_job(order, job)
    await drain()
    assert facts is not None, "no match_start event"
    return MatchRun(
        run_id=run_id, match_id=facts.match_id, facts=facts, featured=featured, events=events, findings=findings,
        snapshots=snapshots, records=[ordered[k] for k in sorted(ordered)], settings=pipeline.models.settings,
        config=config, fault_plan=pipeline.injector.plan if pipeline.injector else None,
        faults_fired=[f.__dict__ for f in pipeline.injector.fired] if pipeline.injector else [],
        budget_exhausted=flags.budget_exhausted, started_utc=started, wall_s=round(time.perf_counter() - clock, 2),
        usage_log=pipeline.models.log, answer_key=answer_key,
        tools_location=pipeline.tools.location if pipeline.tools is not None else None,
    )
