"""Deliberate fault injection, to show (and test) failure recovery. Opt-in only.

A ``FaultPlan`` is a list of rules: *which role*, *what goes wrong*, *on which job* and *on which attempt of the
step*. Jobs are targeted by kind and order ("the second goal" is ``goal#2``), not by call order, so a plan fires
on the same jobs however the calls interleave. The ``FaultInjector`` is a chat middleware that sits *inside* the
model guard, so injected failures take the same retry, logging and budget path as real ones; transport failures
are never sent and never charged, corruptions alter a real reply.

Kinds:

* transport: ``rate_limit``, ``timeout``, ``connection``, ``server_error`` (the guard retries these);
* output: ``malformed`` (prose instead of JSON), ``truncated`` (cut off at the cap);
* corrupted explainer claims: ``corrupt_number``, ``corrupt_team``, ``corrupt_no_evidence``,
  ``corrupt_unknown_ref``, ``corrupt_unlisted_number``, ``corrupt_window``, ``corrupt_score``, ``overclaim``,
  ``corrupt_random`` (one of the deterministic ones, picked per job) and ``corrupt_all`` (every claim loses its
  evidence);
* ``semantic_reject`` (verifier says no to every claim), ``corrupt_persona`` (a wrong number in a persona text).

Spec grammar (comma separated): ``role:kind[@target][#attempt][xN]``, e.g. ``explainer:corrupt_number@goal#1``.
Target is ``*`` (default), a label (``goal``, ``siege``, ``recap``, ``half_time`` ...) optionally with ``.n`` for
the n-th model job of that label (``goal.2``). ``#attempt`` limits the rule to one attempt of the step (default 1;
``#*`` = every attempt). ``xN`` fires up to N times per job (default 1). ``demo`` is a ready-made plan.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from agent_framework import ChatContext, ChatMiddleware, ChatResponse, Message
from agent_framework.exceptions import ChatClientException

from ..llm.fake import openai_error
from ..llm.guard import INJECTED, current_call_context
from .contracts import Job

TRANSPORT = ("rate_limit", "timeout", "connection", "server_error")
OUTPUT = ("malformed", "truncated")
CORRUPTIONS = ("corrupt_number", "corrupt_team", "corrupt_no_evidence", "corrupt_unknown_ref",
               "corrupt_unlisted_number", "corrupt_window", "corrupt_score", "overclaim")
KINDS = (*TRANSPORT, *OUTPUT, *CORRUPTIONS, "corrupt_random", "corrupt_all", "semantic_reject", "corrupt_persona")
ROLES = ("interpreter", "explainer", "verifier", "personalizer")
_SPEC = re.compile(r"^([a-z]+):([a-z_]+)(?:@([a-z_*]+(?:\.\d+)?))?(?:#(\d+|\*))?(?:x(\d+))?$")


@dataclass(frozen=True)
class FaultRule:
    role: str
    kind: str
    target: str = "*"
    attempt: Optional[int] = 1  # attempt of the pipeline step; None = any attempt
    times: int = 1  # per job
    rate: Optional[float] = None  # fire on this share of matching jobs (chosen by a hash of the job id)

    def describe(self) -> str:
        attempt = "every attempt" if self.attempt is None else f"attempt {self.attempt}"
        share = f", {self.rate:.0%} of jobs" if self.rate is not None else ""
        return f"{self.role}: {self.kind} on {self.target} ({attempt}, up to {self.times}x per job{share})"


@dataclass(frozen=True)
class FaultPlan:
    name: str
    rules: tuple[FaultRule, ...]

    def describe(self) -> list[str]:
        return [r.describe() for r in self.rules]


def demo_plan() -> FaultPlan:
    """One of each recovery path, on early, model-eligible moments most matches have."""
    return FaultPlan("demo", (
        FaultRule("interpreter", "malformed", "goal.1"),  # re-asked once, recovers
        FaultRule("explainer", "rate_limit", "goal.1"),  # the guard retries, recovers
        FaultRule("explainer", "corrupt_number", "goal.2"),  # checker rejects, the retry fixes it
        FaultRule("explainer", "corrupt_team", "big_chance.1"),  # checker rejects, the retry fixes it
        FaultRule("verifier", "timeout", "big_chance.2", times=4),  # semantic check skipped, flagged
        FaultRule("personalizer", "server_error", "big_chance.3", times=4),  # template personas
        FaultRule("explainer", "corrupt_all", "big_chance.4", attempt=None, times=9),  # template fallback
        FaultRule("verifier", "semantic_reject", "half_time"),  # semantic rejection, the retry passes
    ))


def bad_claims_plan(rate: float) -> FaultPlan:
    """Corrupt the first explainer draft of ``rate`` of the jobs, to measure what the verifier catches."""
    if not 0 < rate <= 1:
        raise ValueError("--inject-bad must be between 0 and 1")
    return FaultPlan(f"bad-claims-{rate:g}", (FaultRule("explainer", "corrupt_random", "*", rate=rate),))


def parse_faults(spec: str) -> FaultPlan:
    spec = spec.strip()
    if spec == "demo":
        return demo_plan()
    rules = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        m = _SPEC.match(part)
        if not m or m.group(1) not in ROLES or m.group(2) not in KINDS:
            raise ValueError(f"bad fault spec {part!r}: expected role:kind[@target][#attempt][xN] with role in "
                             f"{', '.join(ROLES)} and kind in {', '.join(KINDS)}")
        attempt = None if m.group(4) == "*" else int(m.group(4) or 1)
        rules.append(FaultRule(m.group(1), m.group(2), m.group(3) or "*", attempt, int(m.group(5) or 1)))
    if not rules:
        raise ValueError("empty fault spec")
    return FaultPlan("custom", tuple(rules))


def combine(*plans: Optional[FaultPlan]) -> Optional[FaultPlan]:
    real = [p for p in plans if p is not None]
    if not real:
        return None
    return FaultPlan("+".join(p.name for p in real), tuple(r for p in real for r in p.rules))


@dataclass(frozen=True)
class FiredFault:
    role: str
    kind: str
    job_id: str
    step: Optional[str]
    attempt: Optional[int]


@dataclass
class FaultInjector:
    """Shared state for one run: which jobs are which (label and order), and which faults fired."""

    plan: FaultPlan
    _labels: dict[str, tuple[str, int]] = field(default_factory=dict)
    _jobs: dict[str, Job] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _fired_per_job: dict[tuple[int, str], int] = field(default_factory=dict)
    fired: list[FiredFault] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, job: Job) -> None:
        """Called in match order for every job sent to the agents, so ``goal.2`` means the second such goal."""
        with self._lock:
            if job.job_id not in self._labels:
                self._counts[job.label] = self._counts.get(job.label, 0) + 1
                self._labels[job.job_id] = (job.label, self._counts[job.label])
                self._jobs[job.job_id] = job

    def middleware(self, role: str) -> "RoleFaults":
        return RoleFaults(self, role)

    def _targets(self, rule: FaultRule, job_id: str) -> bool:
        label, n = self._labels.get(job_id, ("", 0))
        target = rule.target
        if target != "*":
            name, _, number = target.partition(".")
            recap = name == "recap" and label in ("half_time", "full_time")
            if not (recap or name == label) or (number and int(number) != n):
                return False
        if rule.rate is not None:
            bucket = int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % 10_000
            return bucket < rule.rate * 10_000
        return True

    def pick(self, role: str) -> Optional[tuple[FaultRule, str, Optional[Job]]]:
        ctx = current_call_context()
        job_id = ctx.finding_id
        if job_id is None:
            return None
        with self._lock:
            for index, rule in enumerate(self.plan.rules):
                if rule.role != role or not self._targets(rule, job_id):
                    continue
                if rule.attempt is not None and ctx.attempt not in (None, rule.attempt):
                    continue
                key = (index, job_id)
                if self._fired_per_job.get(key, 0) >= rule.times:
                    continue
                self._fired_per_job[key] = self._fired_per_job.get(key, 0) + 1
                kind = rule.kind
                if kind == "corrupt_random":
                    deterministic = [k for k in CORRUPTIONS if k != "overclaim"]
                    kind = deterministic[int(hashlib.sha256(f"k{job_id}".encode()).hexdigest()[:8], 16)
                                         % len(deterministic)]
                self.fired.append(FiredFault(role, kind, job_id, ctx.step, ctx.attempt))
                return rule, kind, self._jobs.get(job_id)
        return None


class RoleFaults(ChatMiddleware):
    def __init__(self, injector: FaultInjector, role: str) -> None:
        self.injector = injector
        self.role = role

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        picked = self.injector.pick(self.role)
        if picked is None:
            await call_next()
            return
        _, kind, job = picked
        if kind in TRANSPORT:
            error = openai_error(kind, retry_after_s=0.0 if kind == "rate_limit" else None)
            wrapped = ChatClientException(f"injected {kind}", inner_exception=error)
            setattr(wrapped, INJECTED, kind)
            raise wrapped from error
        if kind == "malformed":
            context.result = _response("Here is my answer in prose rather than JSON.", kind)
            return
        if kind == "truncated":
            context.result = _response('{"claims": [{"text": "cut off', kind, finish_reason="length")
            return
        await call_next()
        result = context.result
        text = result.text if result is not None else ""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return  # nothing sensible to corrupt; the reply goes through as it is
        data = corrupt(kind, data, job)
        context.result = _response(json.dumps(data), kind, like=result)


def _response(text: str, kind: str, *, finish_reason: str = "stop", like: Optional[ChatResponse] = None) -> ChatResponse:
    return ChatResponse(
        messages=[Message(role="assistant", contents=[text])],
        model=getattr(like, "model", None) or "injected",
        usage_details=getattr(like, "usage_details", None),
        finish_reason=getattr(like, "finish_reason", None) or finish_reason if like else finish_reason,
        additional_properties={INJECTED: kind},
    )


# --------------------------------------------------------------------------- #
# Corruptions (applied to a parsed JSON reply)
# --------------------------------------------------------------------------- #


def _first_number(text: str) -> Optional[re.Match[str]]:
    return re.search(r"(?<![\w.])\d+(?:\.\d+)?", text)


def corrupt(kind: str, data: dict[str, Any], job: Optional[Job] = None) -> dict[str, Any]:
    if kind == "semantic_reject":
        for verdict in data.get("verdicts", []):
            verdict.update(supported=False, reason="Injected rejection: says more than the evidence shows.")
        return data
    if kind == "corrupt_persona":
        for entry in data.get("texts", [])[:1]:
            entry["text"] = entry["text"] + " They had 99 corners."
        return data
    claims = data.get("claims") or []
    if not claims:
        return data
    if kind == "corrupt_all":
        for claim in claims:
            claim["refs"], claim["numbers"] = [], []
        return data
    claim = next((c for c in claims if c.get("numbers")), claims[0])
    text = claim.get("text", "")
    if kind == "corrupt_number" and claim.get("numbers"):
        m = _first_number(text)
        number = claim["numbers"][0]
        if m:
            decimals = len(m.group(0).split(".")[1]) if "." in m.group(0) else 0
            wrong = round(float(m.group(0)) * 1.5 + 2, decimals)
            shown = f"{wrong:.{decimals}f}"
            claim["text"] = text[:m.start()] + shown + text[m.end():]
            for n in claim["numbers"]:
                if abs(n["value"] - float(m.group(0))) < 1e-6:
                    n["value"] = float(shown)
                    break
            else:
                number["value"] = float(shown)
    elif kind == "corrupt_team":
        # Give the claim (and its numbers) to the other club: swap the team id and the club names in the text.
        if job is not None and job.facts:
            home, away = job.facts.home, job.facts.away
            mine = job.facts.team(claim.get("team_id")) or job.facts.team(job.team_id) or home
            other = away if mine.team_id == home.team_id else home
            swapped = text.replace(mine.name, "§TEAM§").replace(other.name, mine.name).replace("§TEAM§", other.name)
            claim["text"] = swapped if swapped != text else f"{other.name}: {text}"
            claim["team_id"] = other.team_id
    elif kind == "corrupt_no_evidence":
        claim["refs"], claim["numbers"] = [], []
    elif kind == "corrupt_unknown_ref":
        claim["refs"] = ["e99", *claim.get("refs", [])[1:]]
        for n in claim.get("numbers", [])[:1]:
            n["ref"] = "e99"
    elif kind == "corrupt_unlisted_number":
        claim["text"] = text.rstrip(".") + ", and they won 9 corners."
    elif kind == "corrupt_window":
        # A minute after the moment was detected: outside every evidence window, whatever the job.
        minute = (job.minute if job is not None else 90) + 15
        claim["text"] = text.rstrip(".") + f", in minute {minute}."
    elif kind == "corrupt_score":
        claim["text"] = text.rstrip(".") + ", making it 7-7."
    elif kind == "overclaim":
        claim["text"] = text.rstrip(".") + ", and they will certainly win."
    return data
