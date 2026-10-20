# CLAUDE.md: project guide for Pitchside

Read this first. It is the shared context for anyone (human or AI) working in this repo.

## What this is

**Pitchside** turns a live stream of *synthetic* football events into explainable, personalized match
intelligence. It is a solo entry to the **Inside the Game: Developer Hackathon** (Microsoft + Premier League,
challenge: "The Synthetic Match Insights Engine for Premier League Studio").

The challenge asks for one pipeline with five stages. Our mapping:

| Stage | Our component | Status |
|---|---|---|
| Ingest | Seeded match generator, event schema, replay | **Built** (v0.1.0) |
| Interpret | Deterministic stats engine + key-moment detector | **Built** (v0.2.0); some recall below the 80% bar on held-out seeds, see README |
| Explain | Interpreter, explainer, two-layer verifier (deterministic checker + semantic agent), recaps, audit trail | **Built offline** (milestone 3); live run pending, so no model-quality numbers yet |
| Render | Timed, machine-readable overlay JSON + simple web UI | Run bundles with overlay timing **built**; UI planned |
| Personalize | Analyst / casual fan / favourite-player modes, multiple languages | **Built offline**: en + es in one personalizer call per moment |

## Hard constraints (from the Official Rules, do not violate)

- **Deadline:** submissions close **Oct 27 2026, 11:59 pm Pacific** (about Oct 28 09:00 Helsinki). Target: submit the evening of **Oct 26**.
- **Synthetic data only.** All clubs and players are invented. No real match data. **No Premier League logos, names of real players/clubs, or other third-party trademarks** in the repo, UI or demo video.
- **Newly built project:** no reused old code. Commit small and often; history is our evidence.
- **Public GitHub repo** is required for the entry. Never commit secrets (`.env` is git-ignored; keep `.env.example` current).
- **Demo video** under 2 minutes, public URL; judges may judge from the video and text alone, so the first 30 seconds must show the best moment.
- **Pitch/description must name the Microsoft and Azure technologies actually used.** The README may only claim what is built. Mark anything else "planned".
- **The project must stay accessible to judges, free and unrestricted, until the Judging Period ends on Nov 10** (Rules 4.8), and must work as shown in the video (Rules 4.3). Judges may skip testing and judge from the video and text alone. We satisfy this with a near-zero-cost static demo of recorded runs plus a one-command local run (see "Recorded demo mode"). Do not tear the demo down before Nov 10.
- After tagging `submission-v1`, **freeze `main`** (do any later work on a branch).

## How we are judged (5 equal criteria, 20% each)

1. Technological implementation (quality code, docs, effective use of hero technologies, creative synthetic data)
2. Agentic design and innovation (distinct agent roles, shared state, handoffs, failure recovery, MCP integration)
3. Real-world impact and applicability (production readiness)
4. User experience and presentation (UI, demo clarity, balanced frontend and backend)
5. Adherence to the challenge description

Prizes we are aiming at with one architecture: Grand Prize, Best Use of Microsoft Foundry, Best Multi-Agent
System, Best Azure Cloud Native Integration, Best Enterprise Solution (transparency, audit trail, controls).

## Design principles

1. **Numbers first, words second.** The stats engine is plain deterministic code. Agents only narrate figures it
   computed. A **verifier agent** rejects any claim not supported by the evidence attached to a finding.
2. **Ground truth for evaluation.** The generator writes an answer key (`*.key.json`) listing planted phases and
   scripted goals. Agent output is *scored* against it, not eyeballed.
3. **One event contract.** Everything uses the pydantic models in `src/pitchside/schema.py`
   (JSON Schema in `docs/event.schema.json`; CI fails if it is stale).
4. **Deterministic replays.** `(seed, clubs, storyline)` fully determines a match. Never break this.
5. **Model names live in config**, never hard-coded, and the model client sits behind a small interface so a
   fake or different endpoint can be swapped in for tests and development.

## Current code

- `src/pitchside/schema.py`: event models (pass, shot, tackle, possession_change, pressure, period/match bookends)
- `generator.py`, `storylines.py`, `teams.py`: seeded simulator, 4 storylines (`balanced`, `comeback`, `siege`, `end_to_end`)
- `replay.py`, `jsonl.py`, `cli.py`: replay at adjustable speed, JSONL I/O, CLI (`pitchside generate|replay|summary|schema|storylines`)
- `src/pitchside/stats/`: the stats engine (no LLM). `StatsEngine.ingest(event) -> list[Finding]`, `snapshot()`;
  `metrics.py` (rolling/cumulative numbers), `indices.py` (momentum, chaos), `detector.py` (all thresholds in
  `DetectorConfig`), `findings.py` (`Finding` + evidence, schema in `docs/finding.schema.json`), `evaluate.py` (scores
  findings against answer keys; `uv run pitchside eval-stats --seeds 201-220`; `pitchside findings <jsonl>`)
- `src/pitchside/llm/`: model client and accounting (DECISIONS 10-11). `config.py` (env settings, `Role`), `factory.py`
  (`build_clients()` -> one guarded Agent Framework chat client per role, fake or Foundry), `foundry.py` (Entra ID,
  `AIProjectClient.get_openai_client(max_retries=0)` -> `OpenAIChatClient`/`OpenAIChatCompletionClient`), `fake.py`
  (`FakeChatClient` + scripted `Reply`/`Fail`/`Malformed`/`Delay`), `guard.py` (`ModelGuard` middleware: budget
  reservation, concurrency slot, deadline, retry/backoff, usage log; `call_context(match_id, finding_id)` tags calls),
  `budget.py`, `usage.py` (JSONL log, `pitchside cost`), `prices.py`, `errors.py` (`LLMError` types, `classify`,
  `parse_json`). Keep `llm/__init__.py` free of Agent Framework imports so the CLI starts fast.
- `src/pitchside/agents/`: milestone 3 (DECISIONS 12-14). `contracts.py` (ledger, job, strict reply models, claims,
  checks, narrative, audit, record), `ledger.py` (evidence ids `e*`/`c*`/`p*`, number formatting, recap ledgers),
  `languages.py` (phrase tables; add a language here + `PITCHSIDE_LANGUAGES`), `checker.py` (verifier layer 1),
  `templates.py` (every fallback; a test runs all template output through the checker), `jobs.py` (`walk_match`:
  findings, recap points, stats snapshots; context frozen at detection), `prompts/*.md` + `prompting.py` (versioned
  instructions, compact prompts), `roles.py` (the four Agent Framework `Agent`s, stats function tools, `ask()` with
  malformed/truncation recovery), `workflow.py` (per-job `WorkflowBuilder` graph, bounded verifier loop, recovery
  per role), `pipeline.py` (the plain-code director), `fake_agents.py` (offline agents that parse the real prompts),
  `faults.py` (opt-in fault injection, `demo` plan, `--inject-bad`), `bundle.py` (run bundles, schema in
  `docs/run_bundle.schema.json`), `evaluate.py` (`pitchside eval-agents`). Commands: `pitchside run`, `eval-agents`.
  Offline evaluation numbers are a plumbing check, not model quality; always label them so.
- `src/pitchside/stats/timeline.py`: `MatchTimeline.as_of(as_of_s, as_of_event_id)`, the only read path to a recorded
  match: returns the snapshot, findings and clock as of that moment (the no-future-leakage rule lives here);
  `stats/tools.py`: typed read-only tool functions over a `TimelineView`, shared by the local function tools and the server
- `src/pitchside/mcp_server.py` (`pitchside mcp`): FastMCP server, 12 read-only tools, every stats tool requires `as_of_s`;
  `agents/mcp_tools.py`: the interpreter's MCP tool (in-process server, or `PITCHSIDE_MCP_URL`); `agents/roles.py`
  `BoundToMoment` middleware pins the bound from the job. Docs: `docs/MCP.md`
- `scripts/smoke_foundry.py`: manual live check (not in the test suite); step 4 runs one goal through the live
  agents. `docs/LIVE_EVAL.md`: the manual live evaluation and recording steps
- `tests/`: determinism, schema, possession/score consistency, planted signals, stats engine, held-out answer-key
  bars (`test_stats_heldout.py`: each recall shortfall marked `xfail(strict=True)` per storyline), and the model client
  (`test_llm_*.py`, outbound network blocked by `tests/conftest.py`), the agents (`test_agents_*.py`, fake client
  only) and the MCP server and its use by the interpreter (`test_stats_timeline.py`, `test_stats_tools.py`,
  `test_mcp_*.py`; stdio runs a real local subprocess, HTTP runs in-process); whole suite takes about 2 minutes
- `docs/DECISIONS.md`: decision log (newest at the bottom)

Evaluation discipline for the stats engine: thresholds are tuned on seeds 1-40 only (surge, siege and momentum were
tuned on 1-6; counter-attack and chaos on 1-40); held-out seeds are 101-120, 201-220 and 301-320. Never tune on them,
and never loosen the matching rules or the 80% bar. Real results on the held-out seeds: goals 100%, siege 95-100%,
momentum shift 85-95%, chaos 60-80%, counter-attack 25-55%; the 80% recall bar is met in 1 of 9 storyline results, and the
balanced spell-finding cap (1.0 per match) is met everywhere (0.45-0.50). Counter-attack findings are low-severity context
(0.1 to 0.3) and never trigger a model call on their own; a goal finding carries how its possession began.

Known simplifications: the home side always attacks towards x = 105; ball and shot speeds are estimated, not
tracked. Do not describe them as real tracking data.

## Environment and commands

Solo developer on **Windows (PowerShell)**. Use **uv**.

```powershell
uv sync                      # install (commit uv.lock)
uv run pytest                # tests
uv run pitchside generate --seed 7 --storyline comeback
uv add <package>             # add a dependency
uv run pitchside cost        # model usage and cost from runs/llm_usage.jsonl
uv run pitchside mcp [--transport streamable-http --stateless --json-response --allowed-host H]  # MCP server
uv run pitchside run --seed 7 --storyline comeback [--inject-faults demo] [--tools mcp]   # agents offline, run bundle
uv run pitchside eval-agents --seeds 1-3 [--inject-bad 0.3]                 # offline: plumbing check, not model quality
uv run pitchside eval-agents --seeds 201-205 --live   # manual, needs PITCHSIDE_LLM=foundry; see docs/LIVE_EVAL.md
uv run python scripts/smoke_foundry.py   # manual live check after az login (not in the test suite)
```

Conventions: Python 3.11+, type hints, pydantic v2, small focused modules, tests for every behaviour, no
network calls in unit tests (use the fake model client). Regenerate the schema with
`uv run pitchside schema > docs/event.schema.json` when models change. Do not run destructive git commands
and do not push unless asked.

## Model and Azure plan

- **Models** (Microsoft Foundry, Global Standard deployment, Sweden Central): **GPT-6 Luna** (small, cheap) for
  **every agent, including the explainer**. This is the owner's decision, made for cost: **do not switch to Sol or
  any larger model, and do not propose it.** If output quality falls short, improve the prompts first: give each
  agent a compact structured evidence list, a tight output format, two or three example outputs, and let the
  verifier reject weak or unsupported claims. Each agent role still reads its own deployment name from an env var
  (`MODEL_INTERPRETER`, `MODEL_EXPLAINER`, `MODEL_VERIFIER`, `MODEL_PERSONALIZER`), all set to the Luna deployment,
  so the model client stays swappable for tests. Luna prices per 1M tokens (short context, Global Standard;
  Microsoft's Foundry blog and Azure OpenAI pricing page, Oct 2026): $0.10 in / $0.01 cached / $0.50 out.
  Use Global, not Data Zone (Data Zone costs more and our data is synthetic).
- **Auth:** Microsoft Entra ID via `DefaultAzureCredential`, no API keys in code or git.
  `FOUNDRY_ENDPOINT` is the Foundry *project* endpoint (`https://<resource>.services.ai.azure.com/api/projects/<project>`).
  The identity needs the Cognitive Services OpenAI User role (or Foundry User, formerly Azure AI User, on the project).
  `FOUNDRY_API` picks the Responses API (default) or Chat Completions. Check the current official Azure/Foundry Python
  SDK docs before coding; the Agent Framework Python API was renamed in 2026 (`Agent`, `Message`, `model`), and many
  samples online still use old names.
- **Agents:** Microsoft Agent Framework (open-source SDK) calling the model deployments directly. Avoid Foundry
  Agent Service for now; its pricing is unconfirmed.
- **Planned Azure services (free tiers mostly):** Container Apps (consumption, right-sized to 0.25 vCPU / 0.5 GiB,
  scale to zero), Static Web Apps (Free), Functions, Cosmos DB free tier (shared agent state and audit log),
  Event Grid (key-moment events only), Translator and Speech (multi-language, optional audio), Azure Monitor.
  Event Hubs is optional because it bills hourly. Skip AKS.
- **Unconfirmed, check before relying on them:** that the Foundry project can deploy Luna/Sol (quota or access),
  Event Grid operation counting, and any Agent Service charges.

## Cost controls (build these in)

- Log token usage per call to JSONL (model, input, output, cached, agent role, match id) and print a cost
  summary from a price table in config. Real measured cost replaces all estimates.
- A deterministic detector decides which moments are worth LLM calls. Measured on seeds 1-10: about 4.5
  model-eligible findings per balanced match and 12-13 per storyline match (goals, big chances and spell *starts*
  with `can_trigger_model_call`; spell ends always get a template), plus 2 recaps (half-time, full-time). Each goes
  through about 5 model calls.
- The public demo serves **recorded runs** by default (see "Recorded demo mode"); live generation is off or sits
  behind a small rate limit. Judges clicking around must not burn the budget.
- Keep deployment token quotas low. Set an Azure budget alert.
- After Nov 10: delete the whole resource group (`az group delete --name <rg> --yes`). Keep the repo public.
  Anything billed hourly (for example Event Hubs, if used) can be removed right after submission.

## Recorded demo mode

- The full pipeline can export a **run bundle**: the event stream, stats, findings, agent outputs (narratives per
  persona and language), verifier decisions, overlay JSON and the token/cost log, as plain JSON files.
- The public demo is a **static site (Azure Static Web Apps, Free plan)** that replays run bundles at adjustable
  speed. No servers or model calls are needed to view it.
- **Be honest in the UI:** label it "Recorded run generated by the live pipeline". Never present cached output as live.
- A **live mode** exists for the demo video and local use. On the public link it is off or tightly rate-limited.
- A **local mode** (`uv run ...`, one command) works with a fake or offline model client, so a judge can try the
  project without Azure credentials.

## README screenshot hygiene

Screenshots go in `docs/img/` as compressed PNGs. Show overlays, persona views and the architecture diagram.
Before committing each image, check it for subscription IDs, emails, tenant names, resource names or keys. No real
club logos or real names. Redact or recrop anything sensitive.

## Roadmap (do in order; each milestone ends with tests and a commit, tag as noted)

1. **Stats engine** (`src/pitchside/stats/`, no LLM): incremental `ingest(event) -> findings`. Rolling and
   cumulative windows for possession, field tilt, pressure index, pass completion and difficulty, shots and xG,
   tempo/rhythm, a momentum score, and a control-vs-chaos index. A key-moment detector emits typed `Finding`
   objects (goal, big chance, momentum shift, pressure surge, siege, chaos spell, counter-attack) each carrying
   its **evidence numbers and time window**. Test it against the answer keys: the planted phases must be
   detected within a reasonable time tolerance, with few false positives on `balanced`. Tag `v0.2.0`.
   **Done, with caveats:** tagged `v0.2.0`; recall bars are not all met on held-out seeds (counter-attack and chaos are
   the weak spots), recorded in the README and `docs/DECISIONS.md` rather than hidden.
2. **Model client and accounting:** interface, Foundry client (Entra auth), fake client, token/cost logger, per-role
   model config (see Model and Azure plan).
   **Done offline, live run pending:** `src/pitchside/llm/`, tested offline only. Run `scripts/smoke_foundry.py` once
   the Foundry project and Luna deployments exist; record the API path and the measured cost.
3. **Agents** (Agent Framework): interpreter, explainer, verifier (retry with feedback, rejects unsupported
   claims), personalizer/translator. Shared state, explicit handoffs, failure recovery. An evaluation script
   scores output against the answer key. Tag `v0.3.0`.
   **Built and tested offline, live run pending; not tagged yet.** Offline evaluation passes (a plumbing check).
   Remaining: the smoke test and one manual live evaluation (`docs/LIVE_EVAL.md`); tag `v0.3.0` only after the owner
   has reviewed the evaluation results. Recorded runs use `PITCHSIDE_MATCH_BUDGET_USD=0.25`.
4. **MCP server** exposing the stats engine as tools (possession, pressure, momentum, key moments).
   **Done offline:** `uv run pitchside mcp` (stdio and Streamable HTTP), `PITCHSIDE_TOOLS=local|mcp` for the interpreter;
   not deployed (milestone 6). SDK pinned `mcp>=1.24,<2` because Agent Framework 1.22 breaks on mcp 2.x (DECISIONS 15).
5. **Overlay renderer and web UI** (`web/`): timed overlay JSON schema plus a simple front end showing overlays on a
   simulated match feed. Three personas side by side (analyst, casual fan, favourite-player mode), multi-language.
   Optional spoken commentary via Speech. The UI must be able to replay a **run bundle** (see "Recorded demo mode").
6. **Azure deployment** (`infra/`, Bicep or azd): Container Apps, Static Web Apps, Cosmos DB, cost controls, the
   recorded demo mode as the public default. Verify a clean deploy from scratch, then record the final run bundles
   and the demo video from the deployed system.
7. **Submission:** honest README with architecture diagram, pitch naming real Microsoft/Azure tech, demo video
   (under 2 minutes), repo link added to the project page, tag `submission-v1`, freeze `main`.

## Definition of done for any change

Tests pass (`uv run pytest`), schema doc is current, README status table is honest, no secrets, commit message
says what and why. Any decision that changes architecture, models, cost, or an evaluation rule gets an entry in
`docs/DECISIONS.md` in the same commit.
