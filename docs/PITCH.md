# Project page description

Text for the hackathon project page. It names only Microsoft and Azure technologies that Pitchside uses, and every
figure in it is measured or logged. The placeholders of the draft were filled on 2026-10-26 from the published runs,
the live evaluation ([LIVE_EVAL.md](LIVE_EVAL.md)) and the deployment; the table at the end records where each value
came from.

`uv run python scripts/check_submission.py` fails if a double brace comes back into this file or the README. Check the
project page form for its own length limits; they are not recorded here.

## Tagline

> Synthetic football events in, match insights out: numbers checked by code, claims traced to their evidence.

## Short description

> Pitchside turns a live stream of synthetic football events into explainable match intelligence, so a studio can
> explain a match as it happens without putting numbers on screen that nobody computed. A deterministic stats engine
> finds the key moments and computes every figure. Four Microsoft Agent Framework agents running on GPT-5 mini in
> Microsoft Foundry explain them for analysts, casual fans and favourite-player followers, in English and Spanish. A
> deterministic checker tests each claim's numbers against its evidence, a verifier agent judges whether the evidence
> supports the claim, and every caption links back to the events behind it. The demo site runs on Azure Static Web
> Apps, and the stats engine's MCP server on Azure Container Apps.

## Long description

### The problem it solves

A match studio wants to explain a game while it is being played, and to explain it differently for different
viewers: the figures for an analyst, the story for a casual fan, one player for a supporter, and not only in English.
Language models write that text quickly, but they also write plausible numbers that nobody computed, and a wrong
number on a broadcast graphic is worse than none. Pitchside keeps the two jobs apart. Plain code computes every figure
and decides which moments matter, agents only put words around those figures, code checks the numbers in each claim
and persona text before it is shown, and every caption can be traced back to the events behind it.

### What we built

One pipeline that covers the challenge's five stages: ingest (a seeded synthetic match generator with an answer key),
interpret (a deterministic stats engine, also served as an MCP server), explain (four agents and a deterministic
checker in one Agent Framework workflow per moment), render (a timed overlay contract and a static web UI) and
personalize (three audiences, two languages). It is deployed on Azure, and the public demo replays three runs recorded
through the live pipeline. The code, tests, Bicep template and decision log are in a public, MIT-licensed repository.

### What it does

A football match is a stream of events: passes, shots, tackles, pressure, possession changes. Pitchside generates
that stream for invented clubs from a seed, works out what matters as it happens, and explains it on a broadcast-style
overlay. Every explanation can be opened to show the numbers and events behind it.

- **Key moments, found by code.** A deterministic stats engine keeps each team's possession, field tilt, pressing
  rate, pass completion and difficulty, shots and xG over a rolling 5-minute window and since kick-off. From them it
  computes a momentum index (-1 to +1, a weighted blend of final-third passes 0.35, shots 0.25, xG 0.20 and pressing
  0.20) and a control-vs-chaos index. Pitchside does not measure tempo directly; the control-vs-chaos index (turnover
  rate, pass completion, possession-spell length) describes how settled or fragmented play is. Its detector emits
  seven types of finding (goal, big chance, counter-attack, pressure surge, siege, momentum shift, chaos spell), each
  carrying its evidence numbers and time window.
- **Explained by agents, checked before broadcast.** Each moment worth a model call runs through its own Agent
  Framework graph workflow: an interpreter picks the angle and evidence, an explainer writes structured claims that
  cite evidence ids, and a deterministic checker and a verifier agent test them. A personalizer then writes the
  analyst, casual-fan and favourite-player texts in every language in one call, and the deterministic checker
  re-checks each of those texts (the verifier does not). Half-time and full-time recaps go through the same flow.
- **Personalized.** The broadcast view shows one audience at a time. The analyst dashboard shows all three side by
  side, with live stats, momentum and control-vs-chaos sparklines, and a key-moment timeline.
- **Transparent.** An evidence drawer on every overlay shows the shown text, the claims it was written from, each
  claim's ledger numbers with metric and time window, the triggering events, and the full audit trail: every step,
  rejected drafts with the checker's reasons, retries, fallbacks and model calls.

### How it is built: Microsoft and Azure

- **Microsoft Foundry, GPT-5 mini** (Global Standard, Sweden Central) for all four agent roles, through the Responses
  API with reasoning effort `low`. Each role reads its own deployment name from configuration, so the model client
  stays swappable. The recorded runs on the demo site were generated by these deployments.
- **Microsoft Agent Framework (Python):** four `Agent`s, a `WorkflowBuilder` graph per moment with typed handoffs and
  a bounded verifier loop, function tools, MCP client tools, and chat and function middleware. The middleware reserves
  budget, takes a concurrency slot, enforces a deadline, retries with backoff and logs usage on every model call.
- **Microsoft Entra ID** (`DefaultAzureCredential` via Azure Identity) and the **Azure AI Projects SDK** for the
  Foundry project endpoint. There are no API keys in code or configuration.
- **Model Context Protocol:** the stats engine is also a read-only MCP server with twelve typed tools. Every stats tool
  requires an `as_of_s` bound, enforced on the server, so an agent can never see the future of the match. In the
  recorded runs the interpreter called it over Streamable HTTP.
- **Azure Container Apps** hosts the MCP server (Consumption, scale to zero, IP allow-list, in-app rate limit), and the
  recorded runs were made through it.
- **Azure Static Web Apps** (Free plan) serves the web UI and the recorded run bundles to judges at
  https://green-cliff-068ccc50f.3.azurestaticapps.net/.
- **Azure Monitor Log Analytics** keeps the MCP server's container logs, one JSON line per request, and a **Cost
  Management budget** sends alerts.
- Infrastructure is one **Bicep** template, deployed by a staged script. GitHub Actions builds the container image
  (GitHub Container Registry) and publishes the site.

### Agentic design

- **Distinct roles, explicit handoffs.** Interpreter → explainer → checker → verifier → personalizer, each with a
  strict reply model. Rejection reasons go back to the explainer as feedback, with at most three drafts.
- **Shared state is the evidence ledger.** Every number a model may use has an id: finding evidence, stats-tool
  results and player numbers. Per-player tallies of goals, shots and xG feed the evidence ledger and the
  favourite-player view follows one player; there is no player-influence score yet. Claims must cite ids and list
  each number they use, and the checker rejects a number that does not match the evidence it cites.
- **Routing in plain code.** A director decides which moments get model calls (severity above a floor, spell starts,
  goals, recaps). Routing has one correct answer that code can compute, so no coordinator agent is needed.
- **Failure recovery on every path.** Interpreter down: a rule-based brief. Explainer down, or every claim rejected:
  a template narrative from the same numbers, labelled as a template with its reason. Verifier down: claims accepted on
  the deterministic check alone and flagged as such. Personalizer down: template texts for those personas. Over budget:
  later moments go straight to templates, and the run says so. An opt-in fault injector exercises all of these, and the
  demo shows them.

### Trust, cost and controls

- **Honest labels.** The UI shows what produced each text: the model deployment, a template or an offline stand-in.
  It says "Recorded run generated by the live pipeline" only for a Foundry run, and shows a badge whenever faults were
  injected.
- **Audit trail** per moment: prompt versions and digests, claims, verdicts with reason codes, feedback, retries,
  fallbacks, and a usage row for each model call.
- **Spending caps checked before each call**, per match and overall, with a concurrency limit. Prices fail closed, and
  a refused call is never sent. Logged cost: about $0.10 per match on GPT-5 mini ($0.101 mean, $0.180 max over the 60
  matches of the three live evaluation blocks), computed from token counts and the price table. No match exceeded its
  $0.25 budget.
- **Cross-checked against the Azure bill.** The usage log records $6.81 for every live call made (the smoke test, the
  three evaluation blocks and the recordings), including $0.21 charged as a worst case for calls that returned no
  usage. The Azure portal showed EUR 5.83 for the period. The price table converts the EUR list prices at
  1 USD = 0.88 EUR, so EUR 5.83 is about $6.63: the logged figure is about 3% above the bill, and without the
  worst-case charges ($6.60, about EUR 5.81) it is within EUR 0.02 of it. The bill is what was charged; the log is our
  own estimate.
- **Runs without Azure.** `uv run pitchside serve --open` replays the recorded runs locally with no credentials. The
  whole pipeline also runs offline on a deterministic fake model client.

### Results: measured numbers only

- **Stats engine (measured on held-out seeds 101-120, 201-220 and 301-320, not used for tuning):** goals found 100%
  of the time, sieges 95-100%, momentum shifts 85-95%, chaos spells 60-80%, counter-attacks 25-55%. The 80% recall bar
  we set ourselves is met in 1 of 9 storyline checks. Counter-attacks are therefore low-severity context only and never
  trigger a model call on their own. The false-alarm cap on balanced matches (at most 1.0 spell finding per match) is
  met everywhere, at 0.45-0.50.
- **Live agents (GPT-5 mini, held-out seeds 301-305, 20 matches):** the one block whose output was not read before it
  was measured (prompt wording was tuned after reading seeds 201-210). 19 of 20 shown claims were judged supported in
  a hand-labelled spot check; 0 of 263 model jobs fell back to a template; all 19 planted phases the stats engine
  detected were narrated naming the right club, 79% of them in a model-written narrative. Over all three blocks
  (60 matches), 54 of 60 spot-checked claims were judged supported, and each unsupported one quoted its numbers
  correctly but misread what they meant. Later prompt and checker changes were validated offline and on the three
  published recordings only.
- **Offline pipeline checks (fake model client, seeds 1-3, 12 matches: they show the wiring, not model quality):**
  with 30% of first drafts deliberately corrupted (wrong number, wrong club, impossible score, minute outside the
  window, and more), the checker caught 42 of 42, and none of the shown claims failed a fresh re-check.

### Synthetic data

All clubs, players and matches are invented and generated from a seed. A storyline plants tactical phases (a pressing
surge, a siege, a chaotic spell) and scripted goals. The generator writes an answer key, so the stats engine and the
agents are scored against ground truth instead of being judged by eye. The same `(seed, clubs, storyline)` always
gives the same match.

### What's next (planned, not built)

A managed identity and a cloud recorder job, so recordings run in Azure instead of on a laptop; Entra ID
authentication on the MCP server; Application Insights tracing; more languages (one phrase table each).

### Links

- Demo: https://green-cliff-068ccc50f.3.azurestaticapps.net/
- Video (1:53): https://www.youtube.com/watch?v=VqwtphDpU5Q
- Code (MIT licence): https://github.com/rahmanshah/pitchside-developer-hackathon

## Placeholders

| Placeholder | What it is | Where it comes from | Value (filled 2026-10-26) |
|---|---|---|---|
| `model_name` | The model behind every agent role, as the judges should read it ([decision 19](DECISIONS.md)) | `pipeline.price_model` in the published live bundles' `manifest.json`; the `model` field of their `usage.jsonl` shows what the service reported. All published runs must agree | GPT-5 mini: `price_model` is `gpt-5-mini` in all three manifests, and every `usage.jsonl` reports `gpt-5-mini-2025-08-07` |
| `cost_per_match_usd` | Mean logged model cost per match | [LIVE_EVAL.md](LIVE_EVAL.md) section 4, cost table | $0.101 mean / $0.180 max over 60 live matches (logged from token counts, not the bill). Bill cross-check: EUR 5.83 in the Azure portal against $6.81 in `uv run pitchside cost` over `runs/llm_usage.jsonl` |
| `grounding_rate` | Share of shown claims a person judged supported | the hand labels in `spotcheck.md` of the live evaluation (20 claims per block) | 19 of 20 on seeds 301-305; 54 of 60 over all three blocks |
| `fallback_rate` | Share of model jobs that ended in a template in the clean live evaluation | `report.txt` / `summary.json` of the live evaluation | 0 of 263 on seeds 301-305 (0 in every block) |
| `narration_recall` | Detected planted phases narrated with the right club, live | `report.txt` of the live evaluation | 19 of 19 on seeds 301-305, 79% of them by a model narrative |
| `static_web_app_url` | Public demo URL | `staticWebAppUrl` in `infra/.deploy-outputs.json` ([AZURE.md](AZURE.md) step 6) | https://green-cliff-068ccc50f.3.azurestaticapps.net/ |
| `video_url` | Public demo video | upload ([DEMO.md](DEMO.md)) | https://www.youtube.com/watch?v=VqwtphDpU5Q (1:53, recorded 2026-10-25) |
| `repo_url` | Public GitHub repository | `git remote` | https://github.com/rahmanshah/pitchside-developer-hackathon |
| PENDING live run | Live-model wording | [LIVE_EVAL.md](LIVE_EVAL.md) step 1 passes and the published bundles read "Recorded run generated by the live pipeline" | Final wording kept: all three published manifests have `llm_mode` `foundry` |
| PENDING live eval | Live agent numbers | [LIVE_EVAL.md](LIVE_EVAL.md) step 2, reviewed by the owner | Filled from the clean block, seeds 301-305, not 201-205: prompts were tuned after reading 201-210 |
| PENDING deploy | Azure hosting wording | [AZURE.md](AZURE.md) steps 6-10 done, and the README updated to match | Final wording kept: the README lists Container Apps, Static Web Apps, Log Analytics and the budget as deployed |
