"""The four agents (Agent Framework ``Agent``s), the interpreter's stats tools, and one way to ask any of them.

``ask`` sends a prompt with the role's output cap, reasoning effort and JSON schema, parses the reply, and
recovers from the two output failures a small model has: a reply that is not the JSON asked for (re-asked once)
and a reply cut off at the cap (retried once at twice the cap). Anything else surfaces as an ``LLMError`` for the
workflow to handle. Tool results are given ledger ids by ``LedgerRecorder`` before the model sees them, so every
number a model can cite is checkable.
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, TypeVar

from agent_framework import Agent, FunctionInvocationContext, FunctionMiddleware, Message, tool
from pydantic import BaseModel

from ..llm.config import LLMSettings, Role
from ..llm.errors import MalformedOutput, parse_json
from ..llm.factory import ModelClients
from ..stats import tools as stats_tools
from ..stats.timeline import TimelineView
from .contracts import Job, Ledger
from .ledger import say
from .prompting import instructions

if TYPE_CHECKING:
    from .mcp_tools import McpTools

MAX_TOOL_CALLS = 2
HARD_MAX_OUTPUT = 32000
RETRY_JSON = "Your previous reply was not the JSON object described. Reply again with only that JSON object."

Model = TypeVar("Model", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@dataclass
class ToolState:
    """The job a running interpreter call belongs to: tools read its snapshot and add numbers to its ledger."""

    job: Job
    ledger: Ledger
    calls: list[dict[str, Any]] = field(default_factory=list)


_TOOL_STATE: ContextVar[Optional[ToolState]] = ContextVar("pitchside_tool_state", default=None)


def _run_tool(name: str) -> str:
    state = _TOOL_STATE.get()
    if state is None or state.job.snapshot is None:
        return json.dumps({"items": [], "note": "no match context available"})
    view = TimelineView.from_snapshot(state.job.snapshot)
    return stats_tools.LOCAL_TOOLS[name](view).model_dump_json()


@tool(name="match_totals", description="Since kick-off, per club: possession, shots, xG and pass completion.")
def match_totals_tool() -> str:
    return _run_tool("match_totals")


@tool(name="recent_window",
      description="The last few minutes, per club: field tilt, possession, shots, xG and pressures per minute.")
def recent_window_tool() -> str:
    return _run_tool("recent_window")


def result_text(raw: Any) -> str:
    """A tool result as text, whether a local function returned a string or an MCP tool returned content items."""
    if isinstance(raw, str):
        return raw
    return "".join(getattr(c, "text", None) or "" for c in (raw if isinstance(raw, (list, tuple)) else [raw]) if c)


class LedgerRecorder(FunctionMiddleware):
    """Give every number a tool returns a ledger id (``c*``) and a quote, and show the model those instead.

    Works the same for local function tools (a JSON string) and MCP tools (a list of text content): both carry the
    tool's JSON, whose ``items`` are the numbers. Everything else in the result (moments, notes) passes through.
    """

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        await call_next()
        state = _TOOL_STATE.get()
        if state is None:
            return
        try:
            data = json.loads(result_text(context.result))
            items = data.get("items", [])
        except (TypeError, ValueError, AttributeError):
            return
        if not isinstance(items, list):
            return
        shown, refs = [], []
        for entry in items:
            team = state.job.facts.team(entry.get("team_id"))
            phrase = say(entry["metric"], entry["value"], entry["unit"])
            item = state.ledger.add_item("c", metric=entry["metric"], team_id=entry.get("team_id"),
                                         value=entry["value"], unit=entry["unit"],
                                         window_start_s=entry["window_start_s"], window_end_s=entry["window_end_s"],
                                         baseline=entry.get("baseline"),
                                         say=f"{team.name}: {phrase}" if team else phrase, source="context")
            shown.append({"ref": item.ref, "metric": item.metric, "team_id": item.team_id, "value": item.value,
                          "unit": item.unit, "say": item.say})
            refs.append(item.ref)
        state.calls.append({"tool": context.function.name, "refs": refs})
        if "items" in data:
            data["items"] = shown
            # The bound the host applied is not the model's business, and an echo of times invites confusion.
            for key in ("as_of_s", "data_through_s", "match_id"):
                data.pop(key, None)
            context.result = json.dumps(data)


class BoundToMoment(FunctionMiddleware):
    """MCP tools take their time bound as arguments. The host sets them from the job, whatever the model passed.

    The server only ever answers about what is known at ``as_of_s`` (see ``stats/timeline.py``), so this is what
    makes the interpreter's tools see exactly the moment the finding was detected, and no later: a model that asks
    for ``as_of_s=99999`` or another ``match_id`` is overridden. Arguments are validated after middleware runs, so a
    model that omits the (required) bound still gets a valid call.
    """

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        state = _TOOL_STATE.get()
        if state is None:
            raise RuntimeError("stats tools can only be called for a job: no moment to bound them to")
        job = state.job
        pins: dict[str, Any] = {"as_of_s": job.clock_s, "match_id": job.match_id,
                                "as_of_event_id": job.as_of_event_id}
        arguments = context.arguments
        for name, value in pins.items():
            if isinstance(arguments, MutableMapping):
                if value is None:
                    arguments.pop(name, None)
                else:
                    arguments[name] = value
            elif name in type(arguments).model_fields:
                setattr(arguments, name, value)
        await call_next()


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #

LOCAL_TOOL_NAMES = tuple(stats_tools.LOCAL_TOOLS)


@dataclass
class AgentSet:
    agents: dict[Role, Agent]
    settings: LLMSettings
    tool_names: tuple[str, ...] = LOCAL_TOOL_NAMES  # the interpreter's tools, for its prompt

    def __getitem__(self, role: Role) -> Agent:
        return self.agents[role]


def build_agents(models: ModelClients, mcp_tools: Optional["McpTools"] = None) -> AgentSet:
    """One Agent Framework ``Agent`` per role, each on its own guarded chat client (fake or Foundry).

    The interpreter's tools are local function tools, or (``PITCHSIDE_TOOLS=mcp``) the MCP server's tools.
    """
    settings = models.settings
    if settings.tools == "mcp" and mcp_tools is None:
        raise ValueError("PITCHSIDE_TOOLS=mcp needs an McpTools (build_pipeline makes one)")
    agents: dict[Role, Agent] = {}
    tool_names = LOCAL_TOOL_NAMES
    for role in Role:
        client = models[role]
        kwargs: dict[str, Any] = {}
        if role is Role.INTERPRETER:
            if settings.tools == "mcp":
                tool_names = mcp_tools.tool_names
                kwargs = dict(tools=[mcp_tools.tool], middleware=[BoundToMoment(), LedgerRecorder()])
            else:
                kwargs = dict(tools=[match_totals_tool, recent_window_tool], middleware=[LedgerRecorder()])
            config = getattr(client, "function_invocation_configuration", None)
            if isinstance(config, dict):
                config.update(max_function_calls=MAX_TOOL_CALLS, max_iterations=MAX_TOOL_CALLS + 1)
        agents[role] = Agent(client=client, instructions=instructions(role.value), name=f"pitchside-{role.value}",
                             description=f"Pitchside {role.value} agent", **kwargs)
    return AgentSet(agents, settings, tuple(tool_names))


@dataclass
class Answer:
    value: Any
    text: str
    notes: list[str]  # recoveries made inside this call, for the audit trail
    max_tokens: int


def options_for(settings: LLMSettings, role: Role, schema: type[BaseModel], max_tokens: Optional[int] = None) -> dict:
    options: dict[str, Any] = {"response_format": schema, "max_tokens": max_tokens or settings.max_output(role)}
    if settings.reasoning_effort != "off" and (settings.mode == "fake" or settings.foundry_api == "responses"):
        options["reasoning"] = {"effort": settings.reasoning_effort}
    return options


async def ask(agents: AgentSet, role: Role, prompt: str, schema: type[Model], *,
              tool_state: Optional[ToolState] = None) -> Answer:
    """Ask one agent; returns the parsed reply. Raises ``LLMError`` if it cannot get a valid one."""
    settings = agents.settings
    cap = settings.max_output(role)
    notes: list[str] = []
    messages = [Message(role="user", contents=[prompt])]
    re_asked = widened = False
    token = _TOOL_STATE.set(tool_state)
    try:
        while True:
            try:
                response = await agents[role].run(messages, options=options_for(settings, role, schema, cap))
            except MalformedOutput as error:
                if error.kind == "truncated" and not widened and cap < HARD_MAX_OUTPUT:
                    widened, cap = True, min(cap * 2, HARD_MAX_OUTPUT)
                    notes.append(f"reply cut off at the output cap; retried with max_tokens={cap}")
                    continue
                raise
            text = response.text or ""
            try:
                return Answer(parse_json(text, schema, role=role.value), text, notes, cap)
            except MalformedOutput:
                if re_asked:
                    raise
                re_asked = True
                notes.append("reply was not the JSON asked for; re-asked once")
                messages = [*messages, Message(role="assistant", contents=[text]),
                            Message(role="user", contents=[RETRY_JSON])]
    finally:
        _TOOL_STATE.reset(token)
