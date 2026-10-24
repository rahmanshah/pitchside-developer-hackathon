"""The per-job workflow: an Agent Framework graph with explicit, typed handoffs and a bounded verifier loop.

    interpret -> explain -> check (deterministic) -> verify (semantic) -> personalize -> publish
                    ^          | revise                 | revise
                    +----------+------------------------+
    explain / check / verify -> fallback (template) -> publish

Every step reads and writes the job's working record (``Work``) in workflow state, and appends to its audit
trail: what went in (prompt version and digest), what came out (claims, verdicts, texts), and why the next step
followed. One fresh workflow runs per job: a workflow instance cannot run twice at once, and its state would
otherwise carry over between runs (both verified on agent-framework-core 1.22).

Failure recovery, per role:

* interpreter fails -> a rule-based brief, the job continues;
* explainer fails, or every claim is rejected after the last attempt -> template narrative with the reason;
* semantic verifier fails -> claims accepted on the deterministic check alone, flagged ``skipped: <reason>``;
* personalizer fails, or one of its texts fails the checks -> template text(s) for those personas.
"""

import dataclasses
from typing import Any, Optional, Sequence, Union

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, WorkflowViz, handler
from pydantic import BaseModel, ConfigDict, Field

from ..llm.config import Role
from ..llm.errors import BudgetExhausted, LLMError
from ..llm.guard import call_context
from .checker import CheckContext, check_claim, check_text, missing_goal_score
from .contracts import (
    AuditEntry, Claim, ClaimCheck, Draft, ExplainerReply, InterpreterReply, Job, JobRecord, Ledger, Narrative,
    PersonalizerReply, PersonaText, Reason, VerifierReply,
)
from .languages import Language
from .prompting import (
    digest, explainer_prompt, feedback_lines, interpreter_prompt, personalizer_prompt, personas_for,
    prompt_version, rule_brief, to_claims, verifier_prompt,
)
from .roles import AgentSet, ToolState, ask
from .templates import template_narrative

MAX_ATTEMPTS = 3  # explainer drafts per job (1 + 2 retries with feedback)
WORK = "work"


@dataclasses.dataclass
class RunFlags:
    """Shared by every job of a run: once the budget is exhausted, later jobs skip the models."""

    budget_exhausted: Optional[str] = None


class Work(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: Job
    ledger: Ledger
    brief: Optional[InterpreterReply] = None
    brief_source: str = "rules"
    drafts: list[Draft] = Field(default_factory=list)
    checks: list[ClaimCheck] = Field(default_factory=list)
    audit: list[AuditEntry] = Field(default_factory=list)
    semantic_check: str = "not_run"

    def view(self) -> Job:
        """The job as the prompts see it, with the ledger including numbers the tools added."""
        return self.job.model_copy(update={"ledger": self.ledger})

    def log(self, step: str, actor: str, outcome: str, detail: str, *, attempt: Optional[int] = None,
            next_step: Optional[str] = None, prompt: Optional[str] = None, role: Optional[str] = None,
            **data: Any) -> None:
        self.audit.append(AuditEntry(
            job_id=self.job.job_id, seq=len(self.audit) + 1, step=step, actor=actor, attempt=attempt,
            outcome=outcome, detail=detail, next_step=next_step,
            prompt_version=prompt_version(role) if role else None,
            input_digest=digest(prompt) if prompt else None, data=_jsonable(data),
        ))


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        return value
    return {k: convert(v) for k, v in data.items()}


# --------------------------------------------------------------------------- #
# Handoff messages
# --------------------------------------------------------------------------- #


class _Msg(BaseModel):
    model_config = ConfigDict(frozen=True)


class BriefReady(_Msg):
    pass


class Revise(_Msg):
    attempt: int
    feedback: list[str]


class DraftReady(_Msg):
    attempt: int


class ToVerify(_Msg):
    attempt: int
    claims: list[Claim]


class Verified(_Msg):
    claims: list[Claim]


class Fallback(_Msg):
    reason: str


class Done(_Msg):
    narrative: Narrative


def _llm_reason(role: str, error: LLMError) -> str:
    return f"{role} {error.kind}: {error}"


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #


class _Step(Executor):
    def __init__(self, id: str, agents: AgentSet, languages: Sequence[Language], flags: RunFlags,
                 max_attempts: int = MAX_ATTEMPTS) -> None:
        super().__init__(id=id)
        self.agents = agents
        self.languages = languages
        self.flags = flags
        self.max_attempts = max_attempts

    def note_budget(self, error: LLMError) -> None:
        if isinstance(error, BudgetExhausted) and self.flags.budget_exhausted is None:
            self.flags.budget_exhausted = str(error)


class InterpretStep(_Step):
    @handler
    async def start(self, job: Job, ctx: WorkflowContext[BriefReady]) -> None:
        work = Work(job=job, ledger=job.ledger.model_copy(deep=True))
        prompt = interpreter_prompt(job, tools=self.agents.tool_names)
        state = ToolState(job=job, ledger=work.ledger)
        try:
            with call_context(step="interpret", attempt=1):
                answer = await ask(self.agents, Role.INTERPRETER, prompt, InterpreterReply, tool_state=state)
            brief = answer.value
            unknown = [r for r in brief.focus_refs if not work.ledger.has(r)]
            brief = brief.model_copy(update={"focus_refs": [r for r in brief.focus_refs if work.ledger.has(r)]})
            if not brief.focus_refs:
                brief, work.brief_source = rule_brief(work.view()), "rules"
                outcome = "brief_rules"
                detail = "interpreter cited no known evidence; rule-based brief used"
            else:
                work.brief_source, outcome = "model", "brief"
                detail = f"angle: {brief.angle}"
            work.log("interpret", "pitchside-interpreter", outcome, detail, attempt=1, next_step="explain",
                     prompt=prompt, role="interpreter", brief=brief, tool_calls=state.calls,
                     dropped_refs=unknown, notes=answer.notes)
        except LLMError as error:
            self.note_budget(error)
            brief, work.brief_source = rule_brief(work.view()), "rules"
            work.log("interpret", "pitchside-interpreter", "failed", _llm_reason("interpreter", error), attempt=1,
                     next_step="explain", prompt=prompt, role="interpreter", brief=brief,
                     recovery="rule-based brief")
        work.brief = brief
        ctx.set_state(WORK, work)
        await ctx.send_message(BriefReady())


class ExplainStep(_Step):
    @handler
    async def first(self, message: BriefReady, ctx: WorkflowContext[Union[DraftReady, Fallback]]) -> None:
        await self._draft(1, [], ctx)

    @handler
    async def revise(self, message: Revise, ctx: WorkflowContext[Union[DraftReady, Fallback]]) -> None:
        await self._draft(message.attempt, message.feedback, ctx)

    async def _draft(self, attempt: int, feedback: list[str], ctx: WorkflowContext[Union[DraftReady, Fallback]]) -> None:
        work: Work = ctx.get_state(WORK)
        prompt = explainer_prompt(work.view(), work.brief, feedback, attempt)
        try:
            with call_context(step="explain", attempt=attempt):
                answer = await ask(self.agents, Role.EXPLAINER, prompt, ExplainerReply)
        except LLMError as error:
            self.note_budget(error)
            reason = _llm_reason("explainer", error)
            work.log("explain", "pitchside-explainer", "failed", reason, attempt=attempt, next_step="fallback",
                     prompt=prompt, role="explainer", feedback=feedback)
            ctx.set_state(WORK, work)
            await ctx.send_message(Fallback(reason=reason))
            return
        draft = Draft(attempt=attempt, headline=answer.value.headline, claims=to_claims(answer.value, attempt))
        work.drafts.append(draft)
        work.log("explain", "pitchside-explainer", "draft", f"{len(draft.claims)} claim(s): {draft.headline}",
                 attempt=attempt, next_step="check", prompt=prompt, role="explainer", feedback=feedback,
                 claims=draft.claims, notes=answer.notes)
        ctx.set_state(WORK, work)
        await ctx.send_message(DraftReady(attempt=attempt))


class CheckStep(_Step):
    @handler
    async def check(self, message: DraftReady, ctx: WorkflowContext[Union[Revise, ToVerify, Fallback]]) -> None:
        work: Work = ctx.get_state(WORK)
        draft = work.drafts[-1]
        context = CheckContext.for_job(work.view())
        checks = [check_claim(c, context, message.attempt) for c in draft.claims]
        work.checks += checks
        passed = [c for c, k in zip(draft.claims, checks) if k.ok]
        rejected = len(draft.claims) - len(passed)
        feedback = feedback_lines(draft.claims, checks) or (
            [] if draft.claims else ["the reply had no claims: write 1 to 3 claims"])
        # A goal must keep one claim that cites goal_margin and states the score (decision 23).
        score_gap = missing_goal_score(work.view(), passed) if passed else None
        if draft.claims and not rejected and score_gap is None:
            out: Union[Revise, ToVerify, Fallback] = ToVerify(attempt=message.attempt, claims=passed)
            outcome, detail, nxt = "passed", f"all {len(passed)} claim(s) passed", "verify"
        elif message.attempt < self.max_attempts:
            feedback = feedback + ([f"all claims: {score_gap}"] if score_gap else [])
            out = Revise(attempt=message.attempt + 1, feedback=feedback)
            detail = (f"{rejected} of {len(draft.claims)} claim(s) rejected; retry" if rejected
                      else "no claim states the score with goal_margin; retry")
            outcome, nxt = "rejected", "explain"
        elif passed and score_gap is None:
            out = ToVerify(attempt=message.attempt, claims=passed)
            outcome, detail, nxt = "partial", f"last attempt: keeping {len(passed)} claim(s), dropping {rejected}", "verify"
        elif passed:
            out = Fallback(reason=f"checker rejected the goal claims after {message.attempt} attempt(s): none "
                                  "stated the score")
            outcome, detail, nxt = "rejected", out.reason, "fallback"
        else:
            out = Fallback(reason=f"checker rejected every claim after {message.attempt} attempt(s)")
            outcome, detail, nxt = "rejected", out.reason, "fallback"
        work.log("check", "checker", outcome, detail, attempt=message.attempt, next_step=nxt, checks=checks,
                 feedback=feedback if isinstance(out, Revise) else [])
        ctx.set_state(WORK, work)
        await ctx.send_message(out)


class VerifyStep(_Step):
    @handler
    async def verify(self, message: ToVerify, ctx: WorkflowContext[Union[Revise, Verified, Fallback]]) -> None:
        work: Work = ctx.get_state(WORK)
        claims = message.claims
        prompt = verifier_prompt(work.view(), claims)
        try:
            with call_context(step="verify", attempt=message.attempt):
                answer = await ask(self.agents, Role.VERIFIER, prompt, VerifierReply)
        except LLMError as error:
            self.note_budget(error)
            reason = _llm_reason("verifier", error)
            work.semantic_check = f"skipped: {reason}"
            work.log("verify", "pitchside-verifier", "skipped", f"semantic check skipped ({reason}); accepted on "
                     "the deterministic check alone", attempt=message.attempt, next_step="personalize",
                     prompt=prompt, role="verifier")
            ctx.set_state(WORK, work)
            await ctx.send_message(Verified(claims=claims))
            return
        verdicts = {v.claim: v for v in answer.value.verdicts}
        checks = []
        for n, claim in enumerate(claims, 1):
            verdict = verdicts.get(n)
            ok = bool(verdict and verdict.supported)
            reason = (verdict.reason if verdict else "the verifier gave no verdict for this claim")
            checks.append(ClaimCheck(claim_id=claim.claim_id, attempt=message.attempt, layer="semantic", ok=ok,
                                     reasons=[] if ok else [Reason(code="unsupported", detail=reason)]))
        work.checks += checks
        supported = [c for c, k in zip(claims, checks) if k.ok]
        rejected = len(claims) - len(supported)
        # Dropping an unsupported claim must not drop a goal's score (decision 23).
        score_gap = missing_goal_score(work.view(), supported) if supported else None
        if not rejected:
            work.semantic_check = "passed"
            out: Union[Revise, Verified, Fallback] = Verified(claims=supported)
            outcome, detail, nxt = "passed", f"all {len(supported)} claim(s) supported", "personalize"
        elif message.attempt < self.max_attempts:
            feedback = feedback_lines(claims, checks) + ([f"all claims: {score_gap}"] if score_gap else [])
            out = Revise(attempt=message.attempt + 1, feedback=feedback)
            outcome, detail, nxt = "rejected", f"{rejected} of {len(claims)} claim(s) unsupported; retry", "explain"
        elif supported and score_gap is None:
            work.semantic_check = "passed"
            out = Verified(claims=supported)
            outcome, detail, nxt = "partial", f"last attempt: keeping {len(supported)}, dropping {rejected}", "personalize"
        elif supported:
            out = Fallback(reason=f"semantic verifier rejected the score claim after {message.attempt} attempt(s)")
            outcome, detail, nxt = "rejected", out.reason, "fallback"
        else:
            out = Fallback(reason=f"semantic verifier rejected every claim after {message.attempt} attempt(s)")
            outcome, detail, nxt = "rejected", out.reason, "fallback"
        work.log("verify", "pitchside-verifier", outcome, detail, attempt=message.attempt, next_step=nxt,
                 prompt=prompt, role="verifier", checks=checks, notes=answer.notes)
        ctx.set_state(WORK, work)
        await ctx.send_message(out)


class PersonalizeStep(_Step):
    @handler
    async def personalize(self, message: Verified, ctx: WorkflowContext[Done]) -> None:
        work: Work = ctx.get_state(WORK)
        job = work.view()
        claims = message.claims
        personas = personas_for(job)
        wanted = [(p, lang) for lang in self.languages for p in personas]
        context = CheckContext.for_job(job)
        refs = [r for c in claims for r in c.refs]
        prompt = personalizer_prompt(job, claims, personas, self.languages)
        replaced: list[dict[str, Optional[str]]] = []
        templates: Optional[dict[tuple[str, str], PersonaText]] = None

        def template_text(persona: str, lang: Language, reason: str) -> PersonaText:
            nonlocal templates
            if templates is None:
                out = template_narrative(job, self.languages)
                templates = {(t.persona, t.language): t for t in out.texts}
            return templates[persona, lang.code].model_copy(update={"reason": reason})

        try:
            with call_context(step="personalize", attempt=1):
                answer = await ask(self.agents, Role.PERSONALIZER, prompt, PersonalizerReply)
            given = {(t.persona, t.language): t.text for t in answer.value.texts}
            texts = []
            for persona, lang in wanted:
                text = given.get((persona, lang.code))
                if text is None:
                    reason = "missing from the personalizer reply"
                else:
                    problems = check_text(text, context, refs, lang)
                    if not problems:
                        texts.append(PersonaText(persona=persona, language=lang.code, text=text, source="model"))
                        continue
                    reason = "persona text rejected: " + "; ".join(f"{r.code}: {r.detail}" for r in problems)
                texts.append(template_text(persona, lang, reason))
                # The rejected text stays in the audit trail only (decision 23): never shown, never in the overlay.
                replaced.append({"persona": persona, "language": lang.code, "reason": reason, "text": text})
            outcome = "texts" if not replaced else "texts_partly_template"
            detail = f"{len(texts) - len(replaced)} of {len(texts)} text(s) from the model"
            work.log("personalize", "pitchside-personalizer", outcome, detail, attempt=1, next_step="publish",
                     prompt=prompt, role="personalizer", replaced=replaced, notes=answer.notes)
        except LLMError as error:
            self.note_budget(error)
            reason = _llm_reason("personalizer", error)
            texts = [template_text(p, lang, reason) for p, lang in wanted]
            work.log("personalize", "pitchside-personalizer", "failed", f"{reason}; template texts used",
                     attempt=1, next_step="publish", prompt=prompt, role="personalizer")
        narrative = Narrative(source="model", headline=work.drafts[-1].headline, claims=claims,
                              semantic_check=work.semantic_check, texts=texts)
        ctx.set_state(WORK, work)
        await ctx.send_message(Done(narrative=narrative))


class FallbackStep(_Step):
    @handler
    async def fallback(self, message: Fallback, ctx: WorkflowContext[Done]) -> None:
        work: Work = ctx.get_state(WORK)
        out = template_narrative(work.view(), self.languages, reason=message.reason)
        narrative = Narrative(source="template", reason=message.reason, headline=out.headline, claims=out.claims,
                              semantic_check="not_run", texts=out.texts)
        work.log("fallback", "templates", "template", f"template narrative: {message.reason}", next_step="publish",
                 claims=out.claims)
        ctx.set_state(WORK, work)
        await ctx.send_message(Done(narrative=narrative))


class PublishStep(_Step):
    @handler
    async def publish(self, message: Done, ctx: WorkflowContext[Any, JobRecord]) -> None:
        work: Work = ctx.get_state(WORK)
        n = message.narrative
        work.log("publish", "director", n.source, f"shown: {n.source} narrative with {len(n.claims)} claim(s); "
                 f"semantic check {n.semantic_check}")
        job = work.job
        await ctx.yield_output(JobRecord(
            job_id=job.job_id, kind=job.kind, label=job.label,
            finding_id=job.finding.finding_id if job.finding else None, team_id=job.team_id, clock_s=job.clock_s,
            route="model", ledger=work.ledger, drafts=work.drafts, checks=work.checks, narrative=n,
            audit=work.audit,
        ))


def _is(kind: type) -> Any:
    return lambda message: isinstance(message, kind)


def build_job_workflow(agents: AgentSet, languages: Sequence[Language], flags: Optional[RunFlags] = None,
                       max_attempts: int = MAX_ATTEMPTS) -> Workflow:
    """A fresh workflow for one job."""
    flags = flags or RunFlags()
    args = (agents, languages, flags, max_attempts)
    interpret, explain, check = InterpretStep("interpret", *args), ExplainStep("explain", *args), CheckStep("check", *args)
    verify, personalize = VerifyStep("verify", *args), PersonalizeStep("personalize", *args)
    fallback, publish = FallbackStep("fallback", *args), PublishStep("publish", *args)
    return (
        WorkflowBuilder(start_executor=interpret, max_iterations=60, name="pitchside-job",
                        description="Narrate one finding or recap: interpret, explain, check, verify, personalize.")
        .add_edge(interpret, explain)
        .add_edge(explain, check, condition=_is(DraftReady))
        .add_edge(explain, fallback, condition=_is(Fallback))
        .add_edge(check, explain, condition=_is(Revise))
        .add_edge(check, verify, condition=_is(ToVerify))
        .add_edge(check, fallback, condition=_is(Fallback))
        .add_edge(verify, explain, condition=_is(Revise))
        .add_edge(verify, personalize, condition=_is(Verified))
        .add_edge(verify, fallback, condition=_is(Fallback))
        .add_edge(personalize, publish)
        .add_edge(fallback, publish)
        .build()
    )


def workflow_mermaid(agents: AgentSet, languages: Sequence[Language]) -> str:
    """The job workflow as a Mermaid diagram (for the README)."""
    return WorkflowViz(build_job_workflow(agents, languages)).to_mermaid()


async def run_job(job: Job, agents: AgentSet, languages: Sequence[Language], flags: Optional[RunFlags] = None,
                  max_attempts: int = MAX_ATTEMPTS) -> JobRecord:
    result = await build_job_workflow(agents, languages, flags, max_attempts).run(job)
    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], JobRecord):
        raise RuntimeError(f"job workflow for {job.job_id} produced {len(outputs)} output(s)")
    return outputs[0]
