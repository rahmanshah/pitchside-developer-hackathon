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
**Rejected:** tuning on the held-out seeds, widening tolerances, dropping hard phases from the score. Note: the spell rules (surge, siege, momentum, chaos) were tuned on seeds 1-6 only; the counter-attack rule on 1-40.

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
