"""The stats engine as a Model Context Protocol server: read-only tools over recorded matches.

Run it with one command::

    uv run pitchside mcp                         # stdio, for local MCP clients (VS Code, Claude Desktop, Inspector)
    uv run pitchside mcp --transport streamable-http --host 0.0.0.0 --port 8000     # for Container Apps

**No future leakage.** Every tool requires ``as_of_s`` (match seconds): the server only ever computes from events
at or before it, so a client asking about minute 30 cannot learn anything that happened in minute 31, whatever it
passes. ``as_of_event_id`` can tighten the bound to an exact event (events can share a clock reading). The bound
is enforced in ``stats/timeline.py`` and tested by replacing the future with another match.

**Read-only.** Every tool is annotated ``readOnlyHint``; there are no write tools, no resources that change, and
clients cannot add matches (the host process loads them at start).

Built on the MCP Python SDK 1.x (``FastMCP``). Agent Framework 1.22 requires ``mcp<2``; see docs/DECISIONS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional, Sequence

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .generator import generate_match
from .jsonl import read_events
from .stats import tools
from .stats.findings import FindingKind
from .stats.timeline import MatchStore, TimelineError, TimelineView
from .storylines import STORYLINES

SERVER_NAME = "pitchside-stats"
DEFAULT_MATCH = "7:comeback"
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Football match statistics for synthetic (invented) matches, computed by a deterministic stats engine.

Every tool needs `as_of_s`: the moment you are asking about, in match seconds (45:00 = 2700). Results use only what
happened at or before that moment; nothing later is available. Use `match_id` when the server holds more than one
match (see list_matches). Numbers come back as items with a metric, club, value, unit and time window; quote them
as given. Tools are read-only.
"""

AsOf = Annotated[float, Field(ge=0, description="The moment to answer for, in match seconds since kick-off "
                                                "(45:00 = 2700). Nothing after it is used.")]
MatchId = Annotated[Optional[str], Field(description="Which match; optional when the server holds only one.")]
EventId = Annotated[Optional[int], Field(ge=0, description="Optional, tighter bound: also exclude events with a "
                                                          "larger id (events can share a clock reading).")]
Scope = Annotated[tools.Scope, Field(description="'rolling' = the last few minutes; 'since_kickoff' = the match so "
                                                 "far (metric names then end in _total).")]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClubInfo(_Model):
    team_id: str
    name: str


class MatchInfo(_Model):
    match_id: str
    home: ClubInfo
    away: ClubInfo


class MatchesResult(_Model):
    matches: list[MatchInfo]


def build_server(
    store: MatchStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    stateless_http: bool = False,
    json_response: bool = False,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
) -> FastMCP:
    """An MCP server answering about the matches in ``store``.

    ``allowed_hosts`` is the allow-list of Host headers for Streamable HTTP (DNS-rebinding protection): behind a
    real hostname, such as a Container Apps ingress FQDN, requests are refused with 421 until it is listed.
    """
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*allowed_hosts, *(f"{h}:*" for h in allowed_hosts)],
            allowed_origins=list(allowed_origins),
        )
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, host=host, port=port, stateless_http=stateless_http,
                  json_response=json_response, transport_security=security,
                  log_level="WARNING")  # FastMCP configures root logging; keep stdio and CLI output quiet

    def view(match_id: Optional[str], as_of_s: float, as_of_event_id: Optional[int]) -> TimelineView:
        return store.get(match_id).as_of(as_of_s, as_of_event_id)

    @mcp.tool(name="list_matches", annotations=READ_ONLY)
    def list_matches() -> MatchesResult:
        """List the matches this server can answer about (ids and clubs). Reveals nothing about how they unfold."""
        return MatchesResult(matches=[
            MatchInfo(match_id=t.match_id, home=ClubInfo(team_id=t.home_id, name=t.team_names[t.home_id]),
                      away=ClubInfo(team_id=t.away_id, name=t.team_names[t.away_id])) for t in store.timelines()])

    @mcp.tool(name="possession", annotations=READ_ONLY)
    def possession(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None,
                   scope: Scope = "rolling") -> tools.StatsResult:
        """Each club's share of possession (percent of the time it had the ball)."""
        return tools.possession(view(match_id, as_of_s, as_of_event_id), scope)

    @mcp.tool(name="field_tilt", annotations=READ_ONLY)
    def field_tilt(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None,
                   scope: Scope = "rolling") -> tools.StatsResult:
        """Field tilt: each club's share of the passes played in the final third (50% is even), and the number
        of final-third passes."""
        return tools.field_tilt(view(match_id, as_of_s, as_of_event_id), scope)

    @mcp.tool(name="pressure", annotations=READ_ONLY)
    def pressure(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None,
                 scope: Scope = "rolling") -> tools.StatsResult:
        """Pressing: pressures per minute (the pressure index), pressure count and tackles won, per club."""
        return tools.pressure(view(match_id, as_of_s, as_of_event_id), scope)

    @mcp.tool(name="passing", annotations=READ_ONLY)
    def passing(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None,
                scope: Scope = "rolling") -> tools.StatsResult:
        """Passing: passes, pass completion (percent) and average pass difficulty (0 easy to 1 hard), per club."""
        return tools.passing(view(match_id, as_of_s, as_of_event_id), scope)

    @mcp.tool(name="shots_and_xg", annotations=READ_ONLY)
    def shots_and_xg(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None,
                     scope: Scope = "rolling") -> tools.StatsResult:
        """Shots, expected goals (xG) and goals, per club."""
        return tools.shots_and_xg(view(match_id, as_of_s, as_of_event_id), scope)

    @mcp.tool(name="match_totals", annotations=READ_ONLY)
    def match_totals(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None) -> tools.StatsResult:
        """Since kick-off, per club: possession, shots, xG and pass completion. A quick overview of the match so far."""
        return tools.match_totals(view(match_id, as_of_s, as_of_event_id))

    @mcp.tool(name="recent_window", annotations=READ_ONLY)
    def recent_window(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None) -> tools.StatsResult:
        """The last few minutes, per club: field tilt, possession, shots, xG and pressures per minute."""
        return tools.recent_window(view(match_id, as_of_s, as_of_event_id))

    @mcp.tool(name="momentum", annotations=READ_ONLY)
    def momentum(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None) -> tools.StatsResult:
        """Momentum, -1 to +1 from each club's point of view: positive means that club is on top right now."""
        return tools.momentum(view(match_id, as_of_s, as_of_event_id))

    @mcp.tool(name="control_vs_chaos", annotations=READ_ONLY)
    def control_vs_chaos(as_of_s: AsOf, match_id: MatchId = None, as_of_event_id: EventId = None) -> tools.StatsResult:
        """Is the play settled or broken? Chaos index -1 (controlled) to +1 (chaotic), with turnovers per minute,
        pass completion and average possession length over the last 8 minutes, both clubs together."""
        return tools.control_vs_chaos(view(match_id, as_of_s, as_of_event_id))

    @mcp.tool(name="key_moments", annotations=READ_ONLY)
    def key_moments(
        as_of_s: AsOf,
        match_id: MatchId = None,
        as_of_event_id: EventId = None,
        kinds: Annotated[Optional[list[FindingKind]], Field(description="Only these kinds; default all.")] = None,
        min_severity: Annotated[float, Field(ge=0, le=1, description="Only moments at least this notable.")] = 0.0,
        limit: Annotated[int, Field(ge=1, le=tools.MAX_MOMENTS, description="How many of the most recent.")]
        = tools.DEFAULT_MOMENTS,
    ) -> tools.KeyMomentsResult:
        """Key moments detected by as_of_s: goals, big chances, pressure surges, sieges, momentum shifts, chaotic
        spells and counter-attacks, oldest first. Use evidence_lookup with a finding_id for the numbers."""
        return tools.key_moments(view(match_id, as_of_s, as_of_event_id), kinds, min_severity, limit)

    @mcp.tool(name="evidence_lookup", annotations=READ_ONLY)
    def evidence_lookup(
        as_of_s: AsOf,
        finding_id: Annotated[str, Field(description="A finding_id from key_moments.")],
        match_id: MatchId = None,
        as_of_event_id: EventId = None,
    ) -> tools.EvidenceResult:
        """The numbers (value, unit, time window, baseline) the stats engine attached to one key moment."""
        return tools.evidence_lookup(view(match_id, as_of_s, as_of_event_id), finding_id)

    return mcp


# --------------------------------------------------------------------------- #
# Loading matches (the host decides what the server knows; clients cannot add matches)
# --------------------------------------------------------------------------- #


def parse_generate(spec: str) -> tuple[int, str]:
    """'7:comeback' -> (7, 'comeback'); '7' -> (7, 'comeback')."""
    seed_text, _, storyline = spec.partition(":")
    storyline = storyline or "comeback"
    try:
        seed = int(seed_text)
    except ValueError:
        raise ValueError(f"bad --generate {spec!r}: expected SEED or SEED:STORYLINE, e.g. 7:comeback") from None
    if storyline not in STORYLINES:
        raise ValueError(f"unknown storyline {storyline!r}; choose from {', '.join(sorted(STORYLINES))}")
    return seed, storyline


def load_store(events: Sequence[Path] = (), generate: Sequence[str] = ()) -> MatchStore:
    """Matches from recorded JSONL files and/or generated from (seed, storyline). Default: one demo match.

    Generated matches are fully determined by their seed and storyline, so a deployed server and a pipeline that
    agree on ``7:comeback`` agree on every event.
    """
    store = MatchStore()
    for path in events:
        store.add(list(read_events(Path(path))))
    specs = list(generate) or ([] if events else [DEFAULT_MATCH])
    for spec in specs:
        seed, storyline = parse_generate(spec)
        store.add(generate_match(seed=seed, storyline=storyline).events)
    return store


def serve(store: MatchStore, transport: str = "stdio", **options) -> None:
    """Run the server until the client disconnects. Everything but protocol traffic goes to stderr."""
    server = build_server(store, **options)
    print(f"{SERVER_NAME}: {len(store.ids())} match(es) [{', '.join(store.ids())}], transport {transport}",
          file=sys.stderr, flush=True)
    server.run(transport)  # type: ignore[arg-type]


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "TimelineError", "build_server", "load_store", "parse_generate", "serve"]
