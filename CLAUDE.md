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
| Explain | Interpreter, explainer, two-layer verifier (deterministic checker + semantic agent), recaps, audit trail | **Built and run live** (milestone 3, v0.3.0): gpt-5-mini on Microsoft Foundry; three live evaluation blocks of 20 matches on 2026-10-24 (`docs/LIVE_EVAL.md` section 4) |
| Render | Timed, machine-readable overlay JSON + simple web UI | **Deployed** (milestone 5): `overlay.json` contract, static web UI on Azure Static Web Apps replaying the three recorded live runs in `web/data`, e2e smoke tests and README screenshots |
| Personalize | Analyst / casual fan / favourite-player modes, multiple languages | **Built and run live**: en + es in one personalizer call per moment |
| (hosting) | Azure: Static Web Apps + Container Apps (MCP server) + Log Analytics + budget, one Bicep template | **Deployed** (milestone 6, 2026-10-24): Static Web App in eastus2, the rest in swedencentral; the recorded runs went through the cloud MCP server |

**Status on 2026-10-26.** The live site (https://green-cliff-068ccc50f.3.azurestaticapps.net/) replays three recorded
live runs, the demo video is published (https://www.youtube.com/watch?v=VqwtphDpU5Q, 1:53), `docs/PITCH.md` is
filled and `LICENSE` is MIT. Tags `v0.1.0`, `v0.2.0` and `v0.3.0` exist on origin (`v0.3.0` at `e6fbcc0`).
`submission-v1` is not tagged yet: the owner tags it after the final check. Remaining: the GitHub About box, the
project page, tag `submission-v1`, freeze `main`, submit, and the teardown after the judging period (see "Cost
controls").

**History rewrite (2026-10-25).** The author e-mail of every commit was replaced by the GitHub noreply address, so
every commit id changed and the tags were re-pointed. Docs cite the new ids. The deployed MCP server image keeps its
original GHCR tag: the old full id of `e6fbcc0` (`90ee028…`).

## Hard constraints (from the Official Rules, do not violate)

- **Deadline:** submissions close **Oct 27 2026, 11:59 pm Pacific** (about Oct 28 09:00 Helsinki). Target: submit the evening of **Oct 26**.
- **Synthetic data only.** All clubs and players are invented. No real match data. **No Premier League logos, names of real players/clubs, or other third-party trademarks** in the repo, UI or demo video.
- **Newly built project:** no reused old code. Commit small and often; history is our evidence.
- **Public GitHub repo** is required for the entry. Never commit secrets (`.env` is git-ignored; keep `.env.example` current).
- **Demo video** under 2 minutes, public URL; judges may judge from the video and text alone, so the first 30 seconds must show the best moment.
- **Pitch/description must name the Microsoft and Azure technologies actually used.** The README may only claim what is built. Mark anything else "planned".
- **The project must stay accessible to judges, free and unrestricted, until the Judging Period ends on Nov 10, 23:59 PT (Nov 11, 09:59 Helsinki)** (Rules 4.8), and must work as shown in the video (Rules 4.3). Judges may skip testing and judge from the video and text alone. We satisfy this with a near-zero-cost static demo of recorded runs plus a one-command local run (see "Recorded demo mode"). Do not tear the demo down before Nov 11; winners are announced by Nov 20, so keep it up until then if the budget allows (DECISIONS 29).
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
  `BoundToMoment` middleware pins the bound from the job. Docs: `docs/MCP.md`. Over Streamable HTTP it runs behind
  `src/pitchside/ratelimit.py` (per-client + global token buckets, 429, JSON access log; DECISIONS 18), has `/healthz`,
  and `--generate SEED:STORYLINE:HOME:AWAY` loads matches with non-default clubs
- `infra/` (milestone 6, DECISIONS 18): `main.bicep` (+ `main.bicepparam`, which reads image/IP/e-mail from env vars;
  `bicepconfig.json`), `deploy.ps1` (staged, idempotent, `-FromStage`), `teardown.ps1` (refuses before 2026-11-11).
  `Dockerfile` (uv pattern, non-root). Workflows: `container.yml` (GHCR image), `static-web-app.yml` (manual deploy of
  `web/`), `ci.yml` job `infra` (Bicep build + lint via `tests/test_infra.py`). Never run `az` commands that create,
  change or delete resources from here; the owner deploys (`docs/AZURE.md`)
- `src/pitchside/render/` (milestone 5, DECISIONS 16-17): `overlay.py` (the overlay contract `overlay.json`, built from a
  bundle: replay timeline with half-time/full-time segments, lower-third conflicts resolved here, evidence chain, audit
  badges; schema `docs/overlay.schema.json`, doc `docs/OVERLAY.md`), `provenance.py` (claims -> ledger -> events;
  `triggering` vs `in_window`), `crests.py` (generated, contrast-checked club colours), `demo_data.py`
  (`pitchside demo-data`: offline samples + `web/data/index.json`; `--bundle` publishes a recorded run), `serve.py`
  (`pitchside serve`)
- `web/`: static UI, no build step. `js/replay.js`, `js/timeline.js`, `js/honesty.js` are pure (no DOM, no imports) and
  unit-tested in V8 via mini-racer (`tests/test_web_js.py`); `js/main.js` (state, URL params, keyboard, `window.pitchside`
  test hook), `js/views/` (broadcast, analyst, drawer, crest, pitch). `css/tokens.css` holds the colour tokens (contrast
  checked by `tests/test_web_static.py`). `web/data/` is generated: regenerate with `uv run pitchside demo-data`, never
  edit by hand; only one sample run has injected faults. `tests/e2e/` (marked `e2e`, Chromium via pytest-playwright)
  serves `web/` with its own fixture runs, built fresh on the fake client in a temporary directory
  (`fixture-comeback`, `fixture-faults`, `live-fixture`, `unknown-fixture`; never in `web/data`), so replacing
  `web/data` cannot break them; only `test_e2e_published.py` reads `web/data`, and it checks each run against its own
  manifest. The page exposes `window.pitchside` and `?t=` so tests seek instead of waiting
- `scripts/smoke_foundry.py`: manual live check (not in the test suite; its offline parts are tested in
  `tests/test_smoke_script.py`); step 4 sends one explainer request with the real cap, step 5 runs one goal through the
  live agents, step 6 checks each role's output cap against the largest output seen (reasoning tokens count toward
  it). `docs/LIVE_EVAL.md`: the manual live evaluation and recording steps
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
uv run pitchside serve [--open] [--data DIR]   # web UI on http://127.0.0.1:8000 (recorded runs from web/data)
uv run pitchside demo-data                     # regenerate web/data samples (offline); --bundle DIR --slug S --title T publishes one; never replaces a live (foundry) run without --overwrite-live
uv run playwright install chromium             # once
uv run pytest -m e2e                           # browser smoke tests (excluded from the default run by addopts)
uv run python scripts/capture_screenshots.py   # docs/img/ screenshots, 1920x1080, reproducible
uv run python scripts/check_submission.py      # gate before tagging submission-v1 (fails until live runs are published)
bicep build infra/main.bicep; bicep lint infra/main.bicep   # offline template checks (winget install -e --id Microsoft.Bicep)
docker build -t pitchside-mcp .                # MCP server image (Docker Desktop must be running)
./infra/deploy.ps1 -Image ghcr.io/<owner>/pitchside-mcp:<sha> -AllowedIp <ip> -BudgetEmail <e-mail>   # OWNER ONLY
uv run pitchside eval-agents --seeds 201-205 --live   # manual, needs PITCHSIDE_LLM=foundry; see docs/LIVE_EVAL.md
uv run python scripts/smoke_foundry.py   # manual live check after az login (not in the test suite)
```

Conventions: Python 3.11+, type hints, pydantic v2, small focused modules, tests for every behaviour, no
network calls in unit tests (use the fake model client). Sum floats with `math.fsum` (costs: `llm.usage.total_cost`)
whenever the result is rounded, written to a file or compared: built-in `sum()` changed in Python 3.12, and outputs
must be identical on 3.11 and 3.13 (CI runs both). Reproduce 3.11 locally without touching `.venv`:
`$env:UV_PROJECT_ENVIRONMENT=".venv311"; uv run --python 3.11 pytest` (never `uv run --python` on `.venv` itself). Regenerate the schema with
`uv run pitchside schema > docs/event.schema.json` when models change. Do not run destructive git commands
and do not push unless asked.

## Model and Azure plan

- **Models** (Microsoft Foundry, Global Standard deployment, Sweden Central): one small model for **every agent,
  including the explainer**. The target is **GPT-6 Luna**, but the trial subscription has 0 Luna quota, so **for
  now every role runs on gpt-5-mini** (DECISIONS 19); switch back to Luna if quota is granted. This is the owner's
  decision, made for cost: **do not switch to Sol or any larger model, and do not propose it.** If output quality
  falls short, improve the prompts first: give each agent a compact structured evidence list, a tight output format,
  two or three example outputs, and let the verifier reject weak or unsupported claims. Each agent role still reads
  its own deployment name from an env var (`MODEL_INTERPRETER`, `MODEL_EXPLAINER`, `MODEL_VERIFIER`,
  `MODEL_PERSONALIZER`), all set to the same deployment, so the model client stays swappable for tests;
  `PITCHSIDE_PRICE_MODEL` names the model behind it (`gpt-5-mini` or `gpt-6-luna`). Prices per 1M tokens (price table
  version 2026-10-23 in `llm/prices.py`): gpt-5-mini $0.25 in / $0.03 cached / $2.00 out (Azure OpenAI pricing page
  in EUR, Sweden Central Global, at 1 USD = 0.88 EUR); Luna $0.10 / $0.01 / $0.50 (short context, Global Standard;
  Microsoft's Foundry blog and Azure OpenAI pricing page, Oct 2026). gpt-5-mini accepts reasoning effort `minimal`,
  `low`, `medium` or `high`, not `none`; the default is `low`. Name the model in docs only where it is true;
  `docs/PITCH.md` names GPT-5 mini, taken from the recorded runs' manifests.
  Use Global, not Data Zone (Data Zone costs more and our data is synthetic).
- **Auth:** Microsoft Entra ID via `DefaultAzureCredential`, no API keys in code or git.
  `FOUNDRY_ENDPOINT` is the Foundry *project* endpoint (`https://<resource>.services.ai.azure.com/api/projects/<project>`).
  The identity needs the Cognitive Services OpenAI User role (or Foundry User, formerly Azure AI User, on the project).
  `FOUNDRY_API` picks the Responses API (default) or Chat Completions. Check the current official Azure/Foundry Python
  SDK docs before coding; the Agent Framework Python API was renamed in 2026 (`Agent`, `Message`, `model`), and many
  samples online still use old names.
- **Agents:** Microsoft Agent Framework (open-source SDK) calling the model deployments directly. Avoid Foundry
  Agent Service for now; its pricing is unconfirmed.
- **Azure services (DECISIONS 18, template in `infra/`):** Static Web Apps (Free) for the UI and recorded runs;
  Container Apps (Consumption, 0.25 vCPU / 0.5 GiB, 0..1 replicas) for the MCP server, protected by an IP allow-list
  and the in-app rate limit; Log Analytics; a budget. Image on GHCR (public package). **Planned, not built:** managed
  identity + cloud recorder job, Entra ID auth on the MCP server, Application Insights. **Not used:** Cosmos DB,
  Event Grid, Event Hubs, Functions, Translator, Speech, AKS, ACR, Foundry Agent Service.
- **Confirmed live:** the gpt-5-mini deployments serve the Responses API with our structured output and reasoning
  option (`scripts/smoke_foundry.py` on 2026-10-23, DECISIONS 20; the live evaluations on 2026-10-24). **Unconfirmed:** whether Luna quota will be
  granted, and any Agent Service charges.

## Cost controls (build these in)

- Log token usage per call to JSONL (model, input, output, cached, agent role, match id) and print a cost
  summary from a price table in config. Real measured cost replaces all estimates.
- A deterministic detector decides which moments are worth LLM calls. Measured on seeds 1-10: about 4.5
  model-eligible findings per balanced match and 12-13 per storyline match (goals, big chances and spell *starts*
  with `can_trigger_model_call`; spell ends always get a template), plus 2 recaps (half-time, full-time). Each goes
  through about 5 model calls. Estimated at gpt-5-mini prices (DECISIONS 19): about $0.0083 per moment, $0.05 per
  balanced and $0.12 per storyline match, so the default match budget is $0.25; never lower it to $0.10 for live runs.
  Measured (DECISIONS 29): $0.101 mean / $0.180 max per match over 60 live matches; $6.81 logged in total ($6.60
  without worst-case timeout charges) against an Azure bill of EUR 5.83 (actual cost, 2026-10-23 to 2026-10-26). The
  bill is the authoritative figure.
- The public demo serves **recorded runs** by default (see "Recorded demo mode"); live generation is off or sits
  behind a small rate limit. Judges clicking around must not burn the budget.
- Keep deployment token quotas low. Set an Azure budget alert.
- Teardown: not before 2026-11-11 (the judging period ends 2026-11-10 23:59 PT, 2026-11-11 09:59 Helsinki), and
  preferably after the winners are announced (by 2026-11-20) if the budget allows (DECISIONS 29). Then delete the whole
  resource group (`./infra/teardown.ps1`, which runs `az group delete`). Keep the repo public.
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
   **Done and run live:** `src/pitchside/llm/`. The smoke test ran on the gpt-5-mini deployments through the Responses
   API (DECISIONS 20); measured cost in DECISIONS 29. The smoke test's own cost is not recorded in the repo.
3. **Agents** (Agent Framework): interpreter, explainer, verifier (retry with feedback, rejects unsupported
   claims), personalizer/translator. Shared state, explicit handoffs, failure recovery. An evaluation script
   scores output against the answer key. Tag `v0.3.0`.
   **Done, run live and tagged `v0.3.0`** (at `e6fbcc0`; re-pointed on 2026-10-25 after the history rewrite). Three
   live evaluation blocks of 20 matches on 2026-10-24, spot checks reviewed by the owner (`docs/LIVE_EVAL.md` section
   4). Decisions 24-26 came after the tag and were validated offline and on the three published recordings only.
   Recorded runs use `PITCHSIDE_MATCH_BUDGET_USD=0.25`.
4. **MCP server** exposing the stats engine as tools (possession, pressure, momentum, key moments).
   **Done and deployed:** `uv run pitchside mcp` (stdio and Streamable HTTP), `PITCHSIDE_TOOLS=local|mcp` for the
   interpreter; runs on Azure Container Apps (milestone 6), and the recorded runs used it. SDK pinned `mcp>=1.24,<2` because Agent Framework 1.22 breaks on mcp 2.x (DECISIONS 15).
5. **Overlay renderer and web UI** (`web/`): timed overlay JSON schema plus a simple front end showing overlays on a
   simulated match feed. Three personas side by side (analyst, casual fan, favourite-player mode), multi-language.
   Optional spoken commentary via Speech. The UI must be able to replay a **run bundle** (see "Recorded demo mode").
   **Built and deployed:** contract, UI (broadcast, analyst, evidence drawer, honesty labels, URL params), Playwright
   e2e suite (`uv run pytest -m e2e`, non-blocking CI job), `scripts/capture_screenshots.py` -> `docs/img/`.
   `web/data` holds the three recorded live runs (since 2026-10-24); the screenshots were re-captured from them.
6. **Azure deployment** (`infra/`, Bicep): Container Apps, Static Web Apps, cost controls, the
   recorded demo mode as the public default. Verify a clean deploy from scratch, then record the final run bundles
   and the demo video from the deployed system.
   **Deployed** (2026-10-24, following `docs/AZURE.md`): the three live runs were recorded through the cloud MCP server
   and published, and the video was recorded from the deployed site on 2026-10-25. The README may claim only services
   actually deployed.
7. **Submission:** honest README with architecture diagram, pitch naming real Microsoft/Azure tech, demo video
   (under 2 minutes), repo link added to the project page, tag `submission-v1`, freeze `main`.
   **In progress:** README, pitch, video and `LICENSE` are done. Remaining: the GitHub About box, the project page,
   tag `submission-v1` (the owner), freeze `main`, submit.

## Definition of done for any change

Tests pass (`uv run pytest`), schema doc is current, README status table is honest, no secrets, commit message
says what and why. Any decision that changes architecture, models, cost, or an evaluation rule gets an entry in
`docs/DECISIONS.md` in the same commit.
