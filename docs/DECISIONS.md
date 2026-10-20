# Decision log

Lightweight record of decisions that shape the architecture, models, cost or how we evaluate.
Newest entries at the bottom. Each entry: date, decision, why, and what we rejected.
Add an entry in the same commit as any change that touches these areas.

## 1. One small model for every agent: GPT-6 Luna, no Sol (2026-10-18)

**Decision:** every agent role (interpreter, explainer, verifier, personalizer) runs on GPT-6 Luna in Microsoft Foundry (Global Standard, Sweden Central), each role reading its own deployment name from an env var.
**Why:** cost. Luna is $0.10 in / $0.01 cached / $0.50 out per 1M tokens, and a deterministic detector keeps us to about 30 model calls per match.
**Rejected:** Sol or any larger model, even for the explainer. If quality falls short we improve prompts (compact structured evidence, tight output format, examples) and lean on the verifier.

## 2. Microsoft Agent Framework calling model deployments directly (2026-10-18)

**Decision:** agents are built with Microsoft Agent Framework and call the Foundry model deployments directly, authenticated with Entra ID.
**Why:** Foundry Agent Service pricing is unconfirmed and we need to keep the cost near zero until Nov 10.
**Rejected:** Foundry Agent Service for now. Revisit only if its pricing is confirmed and it earns its cost.

## 3. Public demo is recorded runs, with a local offline mode (2026-10-18)

**Decision:** the public demo is a static site replaying recorded run bundles; live generation is off or rate limited. A one-command local mode works with a fake model client.
**Why:** Rules 4.8 require the project to stay accessible and free to judges until Nov 10, and judges clicking around must not burn the budget.
**Rejected:** an always-on live pipeline as the public default (cost, abuse risk), and presenting cached output as live (the UI labels recorded runs honestly).

## 4. Numbers first, words second (2026-10-17)

**Decision:** a deterministic stats engine computes every figure and finding; agents only narrate those figures; a verifier agent rejects any claim not backed by a finding's evidence.
**Why:** it makes the explanations auditable and testable against the generator's answer keys, which is our main answer to "explainable".
**Rejected:** letting a model read raw events and interpret them directly (unverifiable, expensive).

## 5. Evaluation discipline (2026-10-18)

**Decision:** thresholds are tuned on seeds 1-40 only; bars are checked on held-out seeds 101-120 and fresh seeds 201-220; matching rules, time tolerances and the 80% recall bar are never loosened to make a number pass. Failing bars are marked `xfail(strict=True)` with the real numbers and stated in the README.
**Why:** the answer keys are only evidence if we cannot fit to them. The first run on 101-120 already showed that tuning on six seeds does not generalise.
**Rejected:** tuning on the held-out seeds, widening tolerances, dropping hard phases from the score. Note: the surge, siege and momentum rules were tuned on seeds 1-6 only; the counter-attack rule (entry 7) and the chaos rule (entry 9) on 1-40.

## 6. Public repository from the start (2026-10-17)

**Decision:** the repo is public from the first commit, with small, frequent commits; the link is added to the project page near submission.
**Why:** the Official Rules require a public repo, and a visible history is our evidence that the project is newly built.
**Rejected:** developing privately and publishing at the end. No secrets in git: `.env` is ignored and `.env.example` is kept current.

## 7. Counter-attack rule: shot or box entry, 12 s, and what the held-out runs showed (2026-10-18)

**Decision:** a counter-attack is an open-play regain followed within 12 s by a shot or a completed pass into the box, at most 4 passes and at least 30 m gained. We keep this rule, report its real recall, and do not tune further.
**Why:** the first rule (any shot within 20 s) scored 6/20 on 101-120. Retuned on seeds 1-40 it hits 19/40 (48%), but only 5/20 (25%) on 101-120 and 11/20 (55%) on fresh 201-220, so about 40% pooled held-out. On a per-event basis the planted counters look like ordinary quick transitions; xG and regain-depth cuts lost hits faster than noise.
**Rejected:** loosening the 80% bar or the matching window; accepting a rule that fires 5-8 times per balanced match. Open option, not done: detect counters as a *rate* over a window (a counter spell) instead of single moments.

## 8. Counter-attack work is closed; counters are low-severity context (2026-10-18)

**Decision:** `counter_attack` findings are kept but capped at severity 0.3 (0.1 base, up to 0.3 for a counter that ends in a high-xG goal), below the 0.5 floor (`LLM_TRIGGER_SEVERITY`) at which a finding can trigger a model call on its own. A counter that scores is narrated through its `goal` finding. No more tuning on counters.
**Why:** per-event counters do not separate planted counters from normal quick transitions: 48% when tuned (seeds 1-40), 25-55% on held-out seeds, and about 3 outside planted windows per balanced match. Treating each one as a headline would spend model calls on noise.
**Rejected:** dropping counters entirely (they are still cheap, useful context); raising severity to match how a counter looks; more tuning. Open option, not done: counter *spells* as a rate over a window.

## 9. Chaos spell: 8-minute match window and stricter start (2026-10-18)

**Decision:** match-wide numbers (turnovers, completion, spell length) use an 8-minute window instead of 5. A chaos spell starts at chaos index 0.75 (was 0.8) with a completion part of at least 0.3 (was 0.5), held 90 s at 90% duty, and ends below 0.4. Tuned on seeds 1-40 only, with 101-120 and 201-220 deliberately not run while tuning.
**Why:** on 201-220 the old rule fired on balanced play (balanced spell findings 1.10 per match against a cap of 1.0): 5-minute passing windows are only about 40 passes, so noise looks like chaos. Longer windows cut that without losing the planted spells. On seeds 1-40: comeback chaos 62% to 72%, end-to-end 88% to 88%, balanced spell findings 0.82 to 0.50 per match. The cost is lag: end-to-end median detection +5.5 min (was +4.1), so some hits sit near the +8 min edge.
**Rejected:** loosening the matching window or the 80% bar; a higher chaos threshold alone (with the 8-minute window, 0.9 gave comeback 22/40 and end-to-end 30/40 on seeds 1-40, fewer hits than 0.75 for little extra quiet). Only the match-wide window was lengthened; the 5-minute team windows that siege and surge use are unchanged.
**Outcome (held-out, frozen rule):** balanced spell findings 0.50 on 101-120, 0.50 on 201-220 and 0.45 on fresh 301-320, so the 1.0 cap now holds everywhere (it was 0.70 and 1.10). Chaos recall did not improve: comeback chaos 65% / 70% / 60% and end-to-end 70% / 75% / 80% on 101-120 / 201-220 / 301-320. On the two older ranges combined it fell from 75% to 68% (comeback) and from 80% to 73% (end-to-end), so we traded recall for quiet. The 80% recall bar is met in 1 of 9 storyline results (end-to-end on 301-320, at exactly 80%).
**Decided:** keep the rule (the false-positive fix is real and the recall loss is modest), stop tuning chaos in this milestone, and mark each failing recall bar `xfail(strict=True)` per storyline with the measured numbers, so only bars that truly fail are expected failures. Reaching 80% on chaos will need a different signal, not another threshold or window change.

## 10. Model client: Agent Framework's chat client is the seam; Foundry via the project endpoint (2026-10-19)

**Decision:** we do not define our own model interface. Every agent gets an Agent Framework chat client (`SupportsChatGetResponse`): live, `agent_framework.openai.OpenAIChatClient` (Responses API) on an `AsyncOpenAI` client from `azure.ai.projects.aio.AIProjectClient(FOUNDRY_ENDPOINT, DefaultAzureCredential()).get_openai_client(max_retries=0, timeout=…)`; offline, our own `FakeChatClient` built on Agent Framework's `BaseChatClient`. Budget, concurrency, timeout, retry and usage logging live in one Agent Framework `ChatMiddleware` attached to each client, so they apply to every model round-trip, including tool-call loops. `FOUNDRY_ENDPOINT` is the Foundry *project* endpoint. The default mode is `fake`; live calls need `PITCHSIDE_LLM=foundry`.
**Why:** milestone 3 agents can then be real Agent Framework `Agent`s, unchanged between fake and live. Verified against agent-framework-core 1.22.0 / agent-framework-openai 1.17.0 / azure-ai-projects 2.9.0 and the Learn docs (Oct 2026): `get_openai_client` appends `/openai/v1` and uses scope `https://ai.azure.com/.default`; the OpenAI SDK retries twice by default, and Agent Framework wraps every SDK error in a plain `ChatClientException` with the `openai` error as its cause. A middleware can call `call_next()` again, so retries can live there.
**Rejected:** our own thin `complete()` interface (Agent Framework would only be a transport and the fake would not run real agents); `agent_framework.foundry.FoundryChatClient` (its constructor exposes no timeout or retry setting, so the SDK's hidden retries would make our per-attempt log and budget wrong); keeping the SDK's built-in retries; API keys.
**Also:** `FOUNDRY_API=responses|chat_completions` (default Responses) because we have not yet confirmed that the Luna deployment serves the Responses API; `scripts/smoke_foundry.py` probes both and says which works. `aiohttp` is a dependency because the async Azure SDK pipeline (`azure.ai.projects.aio`, and managed identity in `azure.identity.aio`) uses it as its HTTP transport.

## 11. Spending caps, concurrency and the failure contract (2026-10-19)

**Decision:** before every model-call attempt the guard reserves its worst-case cost (estimated input plus the output cap, `max_tokens`, which it sets to 1024 when the caller has not) against three caps read from env: **$0.10 per match** (`PITCHSIDE_MATCH_BUDGET_USD`), **200 calls per match** (`PITCHSIDE_MATCH_MAX_CALLS`) and **$10 across the whole usage log** (`PITCHSIDE_TOTAL_BUDGET_USD`). A refused call is never sent and is logged as `refused`. Spending is reloaded from the JSONL usage log at startup. At most **4 calls are in flight** across all agents (`PITCHSIDE_MAX_CONCURRENCY`); backoff waits happen outside the slot. Retries: rate limit, timeout, connection and 5xx, up to 3 retries with full-jitter exponential backoff (base 1 s, cap 20 s, never sooner than `retry-after`); auth, missing deployment, bad request and content filter are not retried. Prices fail closed: an unknown price model is a startup error, a response without usage is charged the worst case, and a **timed-out attempt is charged the worst case** because the service may still have billed it (so the log's cost is an upper bound in that case, and says so).
**Caller contract:** every failure is an `LLMError` (`BudgetExhausted`, `ServiceUnavailable`, `RequestRejected`/`AuthFailed`, `MalformedOutput`). Milestone 3 agents catch it and fall back to a deterministic template narrative built from the finding's evidence, marked as such, so offline, over-budget and outage runs still produce output and recorded runs never depend on the service.
**Why:** about 30 detector-selected moments per match times four roles, with verifier retries, is *estimated* at about $0.04 per match at Luna prices (to be replaced by measured cost from the first live runs); $0.10 leaves headroom without letting a bug loop. Reservations stop concurrent calls overshooting a cap. A low deployment quota (tokens per minute) is easier to respect by limiting calls in flight than by retrying 429s.
**Rejected:** checking spend only after calls (concurrent calls could overshoot); logging timeouts as free; a cap shared across processes (the ledger is per process; a deployed multi-replica service needs shared state, planned for Cosmos DB in milestone 6).

## 12. Which findings reach the models, output caps, and run-scoped match budgets (2026-10-20)

**Decision:** a finding goes to the agents only if `can_trigger_model_call` (severity at least 0.5) **and** it is a moment or a spell *start*; spell *ends* always get a deterministic template (they only inherit their start's severity). Measured on seeds 1-10 that is about **4.5 findings per balanced match and 12-13 per storyline match** (not the "about 30" assumed in entries 1 and 11), plus two recaps per match (half-time, full-time). Every role passes its own output cap, which counts reasoning tokens: interpreter 1500, explainer 3000, verifier 1500, personalizer 4000 (`PITCHSIDE_MAX_OUTPUT_<ROLE>`), with Responses API reasoning effort `low` (`PITCHSIDE_REASONING_EFFORT`). A reply cut off at the cap (finish reason `length`) is logged and raised as `MalformedOutput(kind="truncated")`; the agent retries once at twice the cap. The match budget is now kept **per run of a match** (`run_id/match_id`), and offline (fake) runs write their usage log inside their own run directory.
**Why:** the 1024-token default from milestone 2 could truncate JSON once a reasoning model spends tokens thinking. A per-match budget keyed only by match id meant re-recording a match (same seed, same id) started from the earlier run's spending and would quietly fall back to templates; fake costs in the live log would have eaten the $10 total budget after a few offline evaluations. **Recorded demo runs use `PITCHSIDE_MATCH_BUDGET_USD=0.25`** so none of their findings is downgraded for budget reasons; the bundle manifest lists budget fallbacks, and there should be none.
**Rejected:** narrating spell ends with a model call (about 25% more calls for "the siege is over"); one global cap for every role (the personalizer writes 8+ texts, the verifier a short verdict); dropping cost accounting in fake mode (offline evaluation should exercise the cost plumbing too).

## 13. Agent architecture: a graph workflow per job, four roles, no LLM coordinator (2026-10-20)

**Decision:** each model-eligible finding, and each half-time and full-time recap, runs through its own Agent Framework graph workflow (`WorkflowBuilder`): interpreter -> explainer -> deterministic checker -> semantic verifier -> personalizer -> publish, with typed handoff messages on the edges, a bounded retry loop back to the explainer (at most 3 drafts, with the rejection reasons as feedback) and a template fallback. The four roles are Agent Framework `Agent`s, each on its own guarded chat client. A plain-code director (not a model) picks the jobs, freezes their context at detection time, runs them four at a time and merges the results. Per-job working state (ledger, brief, drafts, checks, audit) lives in workflow state; match-level state (all records) is written only by the director. The interpreter calls the stats tools as function tools now; milestone 4 serves the same functions over MCP. A2A is not used.
**Why:** our routing has one correct answer that code can compute (severity, status, checker and verifier results), so a model-driven router would add cost, nondeterminism and a failure mode while deciding nothing. `HandoffBuilder` (agent-framework-orchestrations 1.3.2) hands off through LLM tool calls, is interactive by default and its autonomous mode is experimental. One fresh workflow per job because a `Workflow` instance cannot run twice at once and its state carries over between runs (both checked on agent-framework-core 1.22.0), and because the superstep barrier would make jobs in one run wait for each other. The agent-framework-a2a package is still beta and nothing here needs a remote agent.
**Failure recovery:** interpreter fails -> rule-based brief; explainer fails, or every claim fails the checks after the last draft -> template narrative with the reason; after the last draft only claims that passed both layers are shown; semantic verifier fails -> claims accepted on the deterministic check alone, flagged `semantic_check: skipped: <reason>`; personalizer fails or one of its texts fails the persona checks -> template text for those personas. A budget refusal marks the run so later jobs go straight to templates. Faults can be injected on purpose (`--inject-faults`, opt-in) to show each path; injected faults are listed in the run bundle.
**Rejected:** an LLM coordinator or planner agent (about 15-20% more calls, nothing to decide); `HandoffBuilder`/`SequentialBuilder`; one workflow run for a whole match (superstep barrier, shared-state races); the experimental functional `@workflow` API; showing claims only the deterministic layer has seen without saying so.
