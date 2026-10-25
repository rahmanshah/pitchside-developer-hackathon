# The stats engine as an MCP server

`uv run pitchside mcp` serves the deterministic stats engine over the [Model Context Protocol](https://modelcontextprotocol.io)
as **read-only tools**. The interpreter agent can use it (`PITCHSIDE_TOOLS=mcp`), and so can any MCP client. Decisions and
what was rejected: [DECISIONS.md](DECISIONS.md) entry 15.

## Run it

```powershell
uv run pitchside mcp                                   # stdio, one demo match (seed 7, comeback)
uv run pitchside mcp --generate 1:siege --generate 3:siege:har:wst   # choose matches: SEED[:STORYLINE[:HOME:AWAY]], repeatable
uv run pitchside mcp --events matches/aur_irv_comeback_0007.jsonl   # or a recorded match file
uv run pitchside mcp --transport streamable-http --port 8000        # Streamable HTTP at http://127.0.0.1:8000/mcp (rate limited, /healthz)
```

The host loads the matches; clients cannot add any. Generated matches depend only on seed, storyline and clubs, so a
server and a pipeline that agree on `3:siege:har:wst` agree on every event. Over stdio, only protocol messages go to stdout; the one-line
banner goes to stderr.

## The tools

All are annotated `readOnlyHint` and return typed, structured results (output schemas are advertised). Every number comes
back as an item: `metric`, `team_id` (null for match-wide), `value`, `unit` (`pct`, `per_min`, `xg`, `index`, `s`, `count`,
`m`), `window_start_s`, `window_end_s`, and a `baseline` where there is one.

| Tool | Returns |
|---|---|
| `possession` | share of the time each club had the ball |
| `field_tilt` | share of final-third passes (50% is even) and the pass count |
| `pressure` | pressures per minute (the pressure index), pressure count, tackles won |
| `passing` | passes, pass completion, average pass difficulty (0 easy to 1 hard) |
| `shots_and_xg` | shots, expected goals, goals |
| `match_totals` | since kick-off: possession, shots, xG, pass completion |
| `recent_window` | last few minutes: field tilt, possession, shots, xG, pressures per minute |
| `momentum` | -1 to +1 from each club's view; positive means on top |
| `control_vs_chaos` | chaos index (-1 controlled to +1 chaotic), turnovers per minute, pass completion, mean possession length |
| `key_moments` | goals, big chances, spells and counters detected so far (filter by kind and severity; most recent N) |
| `evidence_lookup` | the numbers the stats engine attached to one key moment (`finding_id` from `key_moments`) |
| `list_matches` | match ids and clubs (nothing about how they unfold) |

`possession`, `field_tilt`, `pressure`, `passing` and `shots_and_xg` take `scope`: `rolling` (the last few minutes; 5 for
team numbers) or `since_kickoff` (metric names then end in `_total`).

## No future leakage

Every tool except `list_matches` **requires `as_of_s`** (match seconds; 45:00 = 2700). The server computes the answer only
from events at or before it, so a client asking about minute 30 cannot learn what happened in minute 31, whatever else it
passes. This is enforced server-side, in one place ([`stats/timeline.py`](../src/pitchside/stats/timeline.py)): the only
way to read a match is `as_of(as_of_s)`, which returns the snapshot, findings and clock as they were then.

- `as_of_event_id` (optional) makes the bound tighter still: events with a larger id are excluded too. Events can share a
  clock reading, and the agents use it to reproduce, exactly, the moment a finding was detected.
- A spell that has not ended by `as_of_s` has no end; a finding detected later does not exist. Asking for one gives the
  same error as asking for an id that never existed.
- The tests (`tests/test_stats_timeline.py`, `test_stats_tools.py`, `test_mcp_server.py`) replace everything after the bound
  with another match's events and require identical answers from every tool, over the real protocol; they check that no
  timestamp in any response is after `as_of_s`; and deliberately breaking the bound makes them fail.

## Using it from the interpreter

`PITCHSIDE_TOOLS=local` (default): two function tools, `match_totals` and `recent_window`, over the job's frozen snapshot.
`PITCHSIDE_TOOLS=mcp`: all eleven stats tools from this server, through Agent Framework's `MCPStreamableHTTPTool`.

- With no `PITCHSIDE_MCP_URL`, the real server runs in-process behind the real Streamable HTTP transport (in-memory, no
  sockets), loaded with the match being run.
- With `PITCHSIDE_MCP_URL=https://<host>/mcp`, the pipeline uses a deployed server, which must hold the same match.
- The **host** sets the time bound: a function middleware overwrites `as_of_s`, `match_id` and `as_of_event_id` from the job
  on every call, so a model that passes `as_of_s=99999` or another match is overridden.
- Every number a tool returns gets a ledger id (`c*`) and a quote before the model sees it, exactly as for local tools, so
  claims that use them go through the same checker. A test runs the same matches in both modes and requires identical
  ledgers, drafts, checks, narratives and audit trails.
- Eleven tool schemas add about 2,800 tokens to each interpreter call (about $0.0007 at gpt-5-mini input prices, $0.0003 at Luna's).

```powershell
uv run pitchside run --seed 7 --storyline comeback --tools mcp
uv run pitchside eval-agents --seeds 1-3 --tools mcp
```

## Using it from other MCP clients

Only Agent Framework's `MCPStdioTool` and `MCPStreamableHTTPTool` and the SDK's own in-memory client are tested here. Other
clients take the standard shape; for a stdio client the command is `uv` with arguments `run pitchside mcp` and the working
directory set to this repository (for example the `servers` entry of a VS Code `mcp.json`, or the MCP Inspector).

## Deploying on Azure Container Apps

The server is deployed on Azure Container Apps (Consumption, `swedencentral`) with the settings below, from the image
([`Dockerfile`](../Dockerfile)) built from `90ee028` and the template ([`infra/main.bicep`](../infra/main.bicep)). The
three published runs were recorded through it. Deployment steps are in [AZURE.md](AZURE.md).

| Setting | Why |
|---|---|
| `pitchside mcp --transport streamable-http --host 0.0.0.0 --port 8000 --stateless --json-response` | Streamable HTTP at `/mcp`. `--stateless` keeps no per-client server state, so scale-to-zero needs no sticky sessions; `--json-response` returns plain JSON. |
| `--allowed-host <app name>.<environment default domain>` (computed in Bicep) | The SDK's DNS-rebinding protection refuses (HTTP 421) any Host header not on the list. |
| `--trust-forwarded --rate-limit 120 --burst 60 --global-rate-limit 300` | Container Apps has no built-in request rate limiting; the server limits itself per client (last `X-Forwarded-For` hop added by the ingress) and overall, answering 429 with `Retry-After`. A whole match makes about 10 requests offline. |
| `--generate 7:comeback:aur:irv --generate 3:siege:har:wst --generate 4:end_to_end:wst:aur` | The published runs' matches (`SEED:STORYLINE:HOME:AWAY`); a test checks the template covers every run in `web/data`. Measured at 0.25 vCPU: ready in about 15 s, 89 MiB. |
| 0.25 vCPU / 0.5 GiB, replicas 0..1, `/healthz` probes | Scales to zero inside the free grant; one replica keeps the in-memory rate limit global. |
| Ingress: external HTTPS, **IP allow-list** | Only the addresses given to `deploy.ps1`; everyone else is refused. Entra ID built-in auth is the planned production option (AZURE.md). |
| Pipeline: `PITCHSIDE_TOOLS=mcp`, `PITCHSIDE_MCP_URL=https://<app FQDN>/mcp` | The agents then use the deployed server; the run bundle records its URL. |

Each request is logged as one JSON line (method, path, status, client, milliseconds) to Log Analytics. The public demo
serves recorded run bundles and does not need this server to be reachable.

## Version note

The server uses the MCP Python SDK **1.x** (`mcp.server.fastmcp.FastMCP`), pinned `mcp>=1.24,<2`. SDK 2.x renames `FastMCP`
to `MCPServer`, but Agent Framework 1.22.0 (the latest release) declares `mcp<2` and fails against 2.x (it reads
`protocolVersion`, `isError` and `McpError`, which 2.x renamed), and the interpreter needs Agent Framework as the client.
The server code is a thin layer over typed functions, so moving to 2.x later means changing the import and the
`build_server` constructor arguments, once Agent Framework supports it.
