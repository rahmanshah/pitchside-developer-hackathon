"""Manual live smoke test for the Foundry model client. NOT part of the test suite; it spends a few cents at most.

Run after `az login`, with FOUNDRY_ENDPOINT and the MODEL_* deployment names in .env:

    uv run python scripts/smoke_foundry.py

What it does:
1. Probes the explainer deployment with the Responses API, falling back to Chat Completions, and reports
   which API path works and the model name the service returned.
2. Makes one tiny call per agent role on that path and prints tokens, cost and latency (logged to the usage
   log under match id "smoke-<time>", so it counts toward PITCHSIDE_TOTAL_BUDGET_USD).
3. Checks that a near-zero budget refuses a call without sending it.
4. Runs one goal through the live agent pipeline (interpreter with tools, explainer, checker, verifier,
   personalizer) and prints the claims, the audit trail and per-call tokens, including reasoning tokens.

Exit codes: 0 ok, 2 configuration or auth problem, 3 service unavailable or no API path worked.
"""

from __future__ import annotations

import asyncio
import time

from agent_framework import Message

from pitchside.llm import ConfigError, Role, load_settings
from pitchside.llm.errors import AuthFailed, BudgetExhausted, LLMError, RequestRejected
from pitchside.llm.factory import build_clients
from pitchside.llm.guard import call_context

PROMPT = [Message(role="user", contents=["Reply with the single word OK."])]
OPTIONS = {"max_tokens": 32}
APIS = ("responses", "chat_completions")


async def ask(client, match_id: str, finding_id: str):
    with call_context(match_id=match_id, finding_id=finding_id):
        return await client.get_response(PROMPT, options=dict(OPTIONS))


async def probe(settings, match_id: str):
    """First API path that answers, with its response; None if neither does."""
    for api in APIS:
        models = build_clients(settings.model_copy(update={"foundry_api": api}))
        try:
            response = await ask(models[Role.EXPLAINER], match_id, f"probe:{api}")
            print(f"  {api:<17} OK    model returned by the service: {response.model or '(not reported)'}")
            return api, response
        except AuthFailed:
            raise
        except RequestRejected as error:
            print(f"  {api:<17} FAIL  {error}")
        finally:
            await models.aclose()
    return None, None


async def main() -> int:
    try:
        settings = load_settings()
        settings = settings.model_copy(update={"mode": "foundry"})
        settings.require_live()
    except ConfigError as error:
        print(f"configuration: {error}")
        return 2

    match_id = f"smoke-{time.strftime('%Y%m%dT%H%M%S')}"
    print(f"endpoint: {settings.foundry_endpoint}\nusage log: {settings.usage_log}  (match id {match_id})\n")
    print("1. API path (explainer deployment)")
    try:
        api, _ = await probe(settings, match_id)
    except AuthFailed as error:
        print(f"\nAUTH FAILED: {error}")
        return 2
    except LLMError as error:
        print(f"\nSERVICE: {error}")
        return 3
    if api is None:
        print("\nNeither the Responses API nor Chat Completions worked; check the deployment names and model.")
        return 3
    if api != settings.foundry_api:
        print(f"  -> set FOUNDRY_API={api} in .env (currently {settings.foundry_api})")

    print(f"\n2. One call per role ({api})")
    models = build_clients(settings.model_copy(update={"foundry_api": api}))
    failures = 0
    try:
        for role in Role:
            try:
                response = await ask(models[role], match_id, f"role:{role.value}")
            except LLMError as error:
                failures += 1
                print(f"  {role.value:<13} FAIL  {error}")
                continue
            print(f"  {role.value:<13} OK    deployment {settings.deployment(role)!r}, model {response.model!r}, "
                  f"reply {response.text.strip()[:20]!r}")
    finally:
        await models.aclose()
    records = [r for r in models.log.read() if r.match_id == match_id]
    for r in records:
        print(f"     {r.role:<13} attempt {r.attempt} {r.status:<7} in {r.input_tokens:>5} (cached {r.cached_tokens:>4}) "
              f"out {r.output_tokens:>4}  ${r.cost_usd:.6f}  {r.latency_ms:.0f} ms"
              + (f"  [{r.error_kind}]" if r.error_kind else ""))
    print(f"  smoke run cost: ${sum(r.cost_usd for r in records):.6f}")

    print("\n3. Budget guard refuses before sending")
    tight = settings.model_copy(update={"foundry_api": api, "match_budget_usd": 1e-9})
    models = build_clients(tight)
    try:
        await ask(models[Role.VERIFIER], f"{match_id}-refusal", "budget-check")
        print("  FAIL  a call went through a $0.000000001 budget")
        failures += 1
    except BudgetExhausted as error:
        print(f"  OK    refused: {error}")
    finally:
        await models.aclose()

    print("\n4. One goal through the live agent pipeline (structured output, reasoning option, all four agents)")
    failures += await pipeline_check(settings.model_copy(update={"foundry_api": api}), match_id)

    print("\nRESULT:", "OK" if failures == 0 else f"{failures} problem(s)")
    print(f"API path: {api}. Summary: uv run pitchside cost --match {match_id}")
    return 0 if failures == 0 else 3


async def pipeline_check(settings, match_id: str) -> int:
    """Run the first goal of seed 7 (comeback) through the job workflow on the live deployments."""
    from pitchside.agents.jobs import FindingPoint, walk_match
    from pitchside.agents.pipeline import RunConfig, build_pipeline
    from pitchside.agents.workflow import run_job
    from pitchside.generator import generate_match

    result = generate_match(seed=7, storyline="comeback")
    job = next(p.job for p in walk_match(result.events) if isinstance(p, FindingPoint) and p.job.label == "goal")
    pipeline = build_pipeline(settings)
    config = RunConfig.from_settings(settings)
    run_id = f"{match_id}-pipeline"
    try:
        with call_context(run_id=run_id, match_id=job.match_id, finding_id=job.job_id):
            record = await run_job(job, pipeline.agents, config.languages)
    finally:
        await pipeline.aclose()
    n = record.narrative
    print(f"  narrative source: {n.source}; semantic check: {n.semantic_check}; drafts: {len(record.drafts)}")
    for claim in n.claims:
        print(f"    claim: {claim.text}")
    print(f"  persona texts from the model: {sum(t.source == 'model' for t in n.texts)} of {len(n.texts)}")
    for entry in record.audit:
        notes = entry.data.get("notes") or []
        print(f"    {entry.step:<12} {entry.outcome:<22} {entry.detail[:90]}" + (f"  {notes}" if notes else ""))
    rows = [r for r in pipeline.models.log.read() if r.run_id == run_id]
    for r in rows:
        print(f"     {r.role:<13} {r.step or '-':<12} {r.status:<7} in {r.input_tokens:>5} out {r.output_tokens:>5} "
              f"(reasoning {r.reasoning_tokens:>5})  ${r.cost_usd:.6f}  {r.latency_ms:.0f} ms"
              + (f"  [{r.error_kind}: {r.detail}]" if r.error_kind else ""))
    print(f"  pipeline check cost: ${sum(r.cost_usd for r in rows):.6f}")
    if any(r.error_kind == "bad_request" for r in rows):
        print("  hint: a bad_request may mean the deployment rejects the reasoning option or the JSON schema; "
              "try PITCHSIDE_REASONING_EFFORT=off, then FOUNDRY_API=chat_completions")
    if n.source != "model":
        print(f"  FAIL  the goal fell back to a template: {n.reason}")
        return 1
    print("  OK    verified model narrative")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
