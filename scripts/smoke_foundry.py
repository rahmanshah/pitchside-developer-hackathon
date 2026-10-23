"""Manual live smoke test for the Foundry model client. NOT part of the test suite; it spends a few cents at most.

Run after `az login`, with FOUNDRY_ENDPOINT and the MODEL_* deployment names in .env:

    uv run python scripts/smoke_foundry.py

What it does:
1. Probes the explainer deployment with the Responses API, falling back to Chat Completions, and reports
   which API path works and the model name the service returned.
2. Makes one tiny call per agent role on that path and prints tokens (input, cached, output, reasoning), cost
   and latency (logged to the usage log under match id "smoke-<time>", so it counts toward
   PITCHSIDE_TOTAL_BUDGET_USD). These calls send the configured reasoning effort and a cap of SMOKE_MAX_TOKENS:
   reasoning tokens count toward the cap, so a reasoning model needs room to think before it writes "OK".
3. Checks that a near-zero budget refuses a call without sending it.
4. Sends one real explainer request (the evidence list of a goal and the claim JSON schema) with the real
   explainer cap and no retry, and reports whether the reply is valid JSON and how many reasoning tokens it used.
5. Runs one goal through the live agent pipeline (interpreter with tools, explainer, checker, verifier,
   personalizer) and prints the claims, the audit trail and per-call tokens.
6. Compares the largest output (reasoning included) of each role in steps 4-5 with its cap, and says which
   PITCHSIDE_MAX_OUTPUT_<ROLE> to raise if a cap was hit or left less than CAP_HEADROOM times room.

Exit codes: 0 ok, 2 configuration or auth problem, 3 service unavailable, no API path worked or a check failed.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Iterable

from agent_framework import Message

from pitchside.llm import ConfigError, Role, load_settings
from pitchside.llm.errors import AuthFailed, BudgetExhausted, LLMError, MalformedOutput, RequestRejected, parse_json
from pitchside.llm.factory import build_clients
from pitchside.llm.guard import call_context
from pitchside.llm.usage import UsageRecord, total_cost

PROMPT = [Message(role="user", contents=["Reply with the single word OK."])]
# Room for low-effort reasoning plus a one-word reply; the call still costs at most $0.0016 at gpt-5-mini prices.
SMOKE_MAX_TOKENS = 800
APIS = ("responses", "chat_completions")
# Reasoning length varies from call to call, so a cap should be at least twice the largest output seen.
CAP_HEADROOM = 2.0
CAP_STEP = 500


def smoke_options(settings, api: str) -> dict:
    """The tiny calls carry the same reasoning option as the agents, so they test what the agents will send."""
    options: dict = {"max_tokens": SMOKE_MAX_TOKENS}
    if settings.reasoning_effort != "off" and api == "responses":
        options["reasoning"] = {"effort": settings.reasoning_effort}
    return options


def usage_line(r: UsageRecord) -> str:
    return (f"     {r.role:<13} {r.step or '-':<9} #{r.attempt} {r.status:<7} in {r.input_tokens:>5} "
            f"(cached {r.cached_tokens:>5})  out {r.output_tokens:>5} (reasoning {r.reasoning_tokens:>5})  "
            f"${r.cost_usd:.6f}  {r.latency_ms:>6.0f} ms"
            + (f"  finish {r.finish_reason}" if r.finish_reason not in (None, "stop") else "")
            + (f"  [{r.error_kind}: {r.detail}]" if r.error_kind else ""))


async def ask(client, settings, api: str, match_id: str, finding_id: str):
    with call_context(match_id=match_id, finding_id=finding_id):
        return await client.get_response(PROMPT, options=smoke_options(settings, api))


async def probe(settings, match_id: str):
    """First API path that answers, with its response; None if neither does."""
    for api in APIS:
        models = build_clients(settings.model_copy(update={"foundry_api": api}))
        try:
            response = await ask(models[Role.EXPLAINER], settings, api, match_id, f"probe:{api}")
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
    print(f"endpoint: {settings.foundry_endpoint}\nusage log: {settings.usage_log}  (match id {match_id})")
    print(f"price model {settings.price_model}, reasoning effort {settings.reasoning_effort}\n")
    print(f"1. API path (explainer deployment, max_tokens={SMOKE_MAX_TOKENS})")
    try:
        api, _ = await probe(settings, match_id)
    except AuthFailed as error:
        print(f"\nAUTH FAILED: {error}")
        return 2
    except MalformedOutput as error:  # e.g. the cap was spent on reasoning: the path works, the reply did not fit
        print(f"\nREPLY: {error}")
        return 3
    except LLMError as error:
        print(f"\nSERVICE: {error}")
        return 3
    if api is None:
        print("\nNeither the Responses API nor Chat Completions worked; check the deployment names and model.")
        return 3
    if api != settings.foundry_api:
        print(f"  -> set FOUNDRY_API={api} in .env (currently {settings.foundry_api})")
    settings = settings.model_copy(update={"foundry_api": api})

    print(f"\n2. One call per role ({api}, max_tokens={SMOKE_MAX_TOKENS})")
    models = build_clients(settings)
    failures = 0
    try:
        for role in Role:
            try:
                response = await ask(models[role], settings, api, match_id, f"role:{role.value}")
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
        print(usage_line(r))
    print(f"  smoke calls cost: ${total_cost(records):.6f}")

    print("\n3. Budget guard refuses before sending")
    tight = settings.model_copy(update={"match_budget_usd": 1e-9})
    models = build_clients(tight)
    try:
        await ask(models[Role.VERIFIER], tight, api, f"{match_id}-refusal", "budget-check")
        print("  FAIL  a call went through a $0.000000001 budget")
        failures += 1
    except BudgetExhausted as error:
        print(f"  OK    refused: {error}")
    finally:
        await models.aclose()

    print(f"\n4. One explainer request with structured output and the real cap "
          f"(max_tokens={settings.max_output(Role.EXPLAINER)}, no retry)")
    explainer_ok, explainer_rows = await explainer_check(settings, f"{match_id}-explainer")
    failures += not explainer_ok

    print("\n5. One goal through the live agent pipeline (structured output, reasoning option, all four agents)")
    pipeline_failures, pipeline_rows = await pipeline_check(settings, f"{match_id}-pipeline")
    failures += pipeline_failures

    print("\n6. Output caps against the largest output seen (reasoning included)")
    report = cap_report([*explainer_rows, *pipeline_rows], settings)
    for line in report.lines:
        print(f"  {line}")
    failures += bool(report.too_tight)

    print("\nRESULT:", "OK" if failures == 0 else f"{failures} problem(s)")
    print(f"API path: {api}. Summary: uv run pitchside cost --match {match_id}")
    return 0 if failures == 0 else 3


def first_goal_job():
    """The first goal of seed 7 (comeback): a real job with a real evidence ledger."""
    from pitchside.agents.jobs import FindingPoint, walk_match
    from pitchside.generator import generate_match

    result = generate_match(seed=7, storyline="comeback")
    return next(p.job for p in walk_match(result.events) if isinstance(p, FindingPoint) and p.job.label == "goal")


async def explainer_check(settings, run_id: str) -> tuple[bool, list[UsageRecord]]:
    """Send the explainer prompt for one goal (rule-based brief, no feedback) once, exactly as the workflow would
    on its first draft but without ``ask``'s widen-and-retry, so a tight cap shows up instead of being papered over."""
    from pitchside.agents.contracts import ExplainerReply
    from pitchside.agents.pipeline import build_pipeline
    from pitchside.agents.prompting import explainer_prompt, rule_brief
    from pitchside.agents.roles import options_for

    job = first_goal_job()
    prompt = explainer_prompt(job, rule_brief(job), [], 1)
    pipeline = build_pipeline(settings.model_copy(update={"tools": "local"}))  # the explainer uses no tools
    ok = False
    try:
        with call_context(run_id=run_id, match_id=job.match_id, finding_id=job.job_id, step="explain", attempt=1):
            response = await pipeline.agents[Role.EXPLAINER].run(
                [Message(role="user", contents=[prompt])],
                options=options_for(settings, Role.EXPLAINER, ExplainerReply))
        reply = parse_json(response.text or "", ExplainerReply, role="explainer")
        ok = True
        print(f"  OK    valid JSON: {len(reply.claims)} claim(s); headline {reply.headline[:70]!r}")
    except LLMError as error:
        print(f"  FAIL  {error}")
    finally:
        await pipeline.aclose()
    rows = [r for r in pipeline.models.log.read() if r.run_id == run_id]
    for r in rows:
        print(usage_line(r))
        if r.output_tokens:
            print(f"        reasoning share of output {r.reasoning_tokens / r.output_tokens:.0%}; "
                  f"{r.output_tokens} of the {settings.max_output(Role.EXPLAINER)}-token cap used")
    return ok, rows


async def pipeline_check(settings, run_id: str) -> tuple[int, list[UsageRecord]]:
    """Run the first goal of seed 7 (comeback) through the job workflow on the live deployments."""
    from pitchside.agents.pipeline import RunConfig, build_pipeline
    from pitchside.agents.workflow import run_job

    job = first_goal_job()
    pipeline = build_pipeline(settings)
    config = RunConfig.from_settings(settings)
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
        print(usage_line(r))
    print(f"  pipeline check cost: ${total_cost(rows):.6f}")
    if any(r.error_kind == "bad_request" for r in rows):
        print("  hint: a bad_request may mean the deployment rejects the reasoning option or the JSON schema; "
              "try PITCHSIDE_REASONING_EFFORT=off, then FOUNDRY_API=chat_completions")
    if n.source != "model":
        print(f"  FAIL  the goal fell back to a template: {n.reason}")
        return 1, rows
    print("  OK    verified model narrative")
    return 0, rows


@dataclass
class CapReport:
    lines: list[str]
    too_tight: dict[str, int]  # role -> suggested cap


def suggested_cap(largest_output: int) -> int:
    return max(CAP_STEP, math.ceil(largest_output * CAP_HEADROOM / CAP_STEP) * CAP_STEP)


def cap_report(rows: Iterable[UsageRecord], settings) -> CapReport:
    """Per role: the largest output seen against the cap. A cap is too tight if a reply hit it (finish ``length``)
    or the largest output used more than 1 / CAP_HEADROOM of it. Only successful calls with usage count."""
    lines: list[str] = []
    too_tight: dict[str, int] = {}
    seen = [r for r in rows if r.status == "ok" and r.output_tokens]
    for role in Role:
        mine = [r for r in seen if r.role == role.value]
        cap = settings.max_output(role)
        env = f"PITCHSIDE_MAX_OUTPUT_{role.name}"
        if not mine:
            lines.append(f"{role.value:<13} cap {cap:>5}  no calls with usage seen")
            continue
        largest = max(mine, key=lambda r: r.output_tokens)
        hit = any(r.finish_reason == "length" for r in mine)
        line = (f"{role.value:<13} cap {cap:>5}  largest output {largest.output_tokens:>5} "
                f"(reasoning {largest.reasoning_tokens:>5}) = {largest.output_tokens / cap:.0%} of the cap")
        if hit or largest.output_tokens * CAP_HEADROOM > cap:
            suggestion = max(suggested_cap(largest.output_tokens), cap * 2 if hit else 0)
            too_tight[role.value] = suggestion
            why = "a reply hit the cap" if hit else f"less than {CAP_HEADROOM:g}x headroom"
            line += f"  TOO TIGHT ({why}): set {env}={suggestion}"
        else:
            line += "  ok"
        lines.append(line)
    return CapReport(lines, too_tight)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
