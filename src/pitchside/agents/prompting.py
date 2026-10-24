"""Prompts: the system instructions (``prompts/*.md``, versioned by hash) and compact per-job user prompts.

User prompts are short, line-oriented blocks: the moment, the clubs, the evidence (one line per ledger id, with how
to quote it), the players, and for retries the checker's feedback. The same format is parsed by the offline fake
agents, so offline runs exercise these builders too.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import resources
from typing import Iterable, Optional, Sequence

from .contracts import ANALYST, CASUAL, FAV_PREFIX, Claim, ClaimCheck, ExplainerReply, InterpreterReply, Job, Ledger
from .languages import ENGLISH, Language
from .ledger import word_value

ROLES = ("interpreter", "explainer", "verifier", "personalizer")
BOTH_CLUBS = "both clubs"  # the club column of a match-wide number ("match" was copied as "the whole match")


@lru_cache(maxsize=None)
def instructions(role: str) -> str:
    return resources.files("pitchside.agents").joinpath(f"prompts/{role}.md").read_text(encoding="utf-8")


def prompt_version(role: str) -> str:
    return digest(instructions(role))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #


def score_clause(job: Job, lang: Language = ENGLISH) -> str:
    """The score as a clause a text can quote: "it is 0-0", "it is 1-0 to Aurora Rovers" (home goals first).
    "score Aurora Rovers 0-0 Ironvale Athletic" was pasted into persona texts (decision 24)."""
    s = job.score
    score = f"{s.home}-{s.away}"
    if s.home == s.away:
        return lang.phrases["score.clause"].format(score=score)
    leader = job.facts.home if s.home > s.away else job.facts.away
    return lang.phrases["score.clause_lead"].format(score=score, team=leader.name)


def moment_line(job: Job) -> str:
    if job.kind == "recap":
        return f"MOMENT: recap {job.label} | minute {job.minute} | score: {score_clause(job)}"
    finding = job.finding
    label = job.label if finding is None or finding.status == "moment" else f"{job.label} {finding.status}"
    team = job.facts.team(job.team_id)
    who = f"team {team.team_id} ({team.name}, {team.side})" if team else "team both"
    # How the shot ended, for every role: the personalizer sees only this line and the claims (decision 24).
    outcome = next((f" | outcome: {word_value(i.metric, i.value)}" for i in job.ledger.items
                    if i.metric == "shot_outcome" and i.source == "finding"), "")
    return f"MOMENT: {label} | {who} | minute {job.minute}{outcome} | score: {score_clause(job)}"


def clubs_line(job: Job) -> str:
    return "CLUBS: " + "; ".join(f"{t.team_id} = {t.name} ({t.side})" for t in job.facts.teams)


def scores_line(job: Job) -> str:
    parts = ["0-0"] + [f"{g.score.home}-{g.score.away} ({int(g.clock_s // 60) + 1}')" for g in job.goals]
    return "SCORES SO FAR: " + "; ".join(parts)


def evidence_lines(ledger: Ledger, refs: Optional[Iterable[str]] = None, windows: bool = False) -> list[str]:
    wanted = None if refs is None else set(refs)
    lines = []
    for item in ledger.items:
        if wanted is not None and item.ref not in wanted:
            continue
        window = f" | {clock(item.window_start_s)}-{clock(item.window_end_s)}" if windows else ""
        value = word_value(item.metric, item.value) or f"{item.value:g} {item.unit}"
        lines.append(f"{item.ref} | {item.metric} | {item.team_id or BOTH_CLUBS} | {value}{window} "
                     f"| say \"{item.say}\"")
    return lines


def player_lines(ledger: Ledger) -> list[str]:
    return [f"{p.ref} | {p.name} | {p.team_id} | {p.involvement}" for p in ledger.players]


def _block(title: str, lines: Sequence[str]) -> list[str]:
    return [f"{title}:", *lines] if lines else []


def rule_brief(job: Job) -> InterpreterReply:
    """The deterministic brief used when the interpreter is unavailable: players, then the first evidence."""
    players = [p.ref for p in job.ledger.players][:1]
    if job.kind == "recap":
        numbers = [i.ref for i in job.ledger.items if i.source == "claim"][:2] + [
            i.ref for i in job.ledger.items if i.metric in ("shots_total", "xg_total")][:2]
    else:
        numbers = [i.ref for i in job.ledger.items if i.source == "finding"][:3]
    return InterpreterReply(angle=f"{job.label.replace('_', ' ')} at minute {job.minute}",
                            focus_refs=players + numbers, note="")


# --------------------------------------------------------------------------- #
# Per role
# --------------------------------------------------------------------------- #


def interpreter_prompt(job: Job, tools: Sequence[str] = ()) -> str:
    lines = ["TASK: interpret this moment.", moment_line(job), clubs_line(job), scores_line(job)]
    if job.recent:
        lines.append("RECENT: " + "; ".join(f"{m.minute}' {m.kind} {m.status} {m.team_id or 'both'}"
                                            for m in job.recent))
    lines += _block("EARLIER (verified claims)", [f"{c.minute}' [{c.label}] {c.text} (ids {', '.join(c.refs)})"
                                                  for c in job.earlier])
    lines += _block("EVIDENCE", evidence_lines(job.ledger))
    lines += _block("PLAYERS", player_lines(job.ledger))
    if tools:
        lines.append("TOOLS: " + ", ".join(tools) + ". They return context numbers with ids you can cite, already "
                     "limited to this moment of the match; do not pass times or match ids.")
    return "\n".join(lines)


def explainer_prompt(job: Job, brief: InterpreterReply, feedback: Sequence[str], attempt: int) -> str:
    lines = ["TASK: write the claims.", moment_line(job), clubs_line(job), scores_line(job),
             f"BRIEF: {brief.angle} | focus {', '.join(brief.focus_refs) or 'none'} | {brief.note}".rstrip(" |")]
    lines += _block("EARLIER (verified claims)", [f"{c.minute}' [{c.label}] {c.text} (ids {', '.join(c.refs)})"
                                                  for c in job.earlier])
    lines += _block("EVIDENCE", evidence_lines(job.ledger))
    lines += _block("PLAYERS", player_lines(job.ledger))
    lines += _block(f"FEEDBACK (attempt {attempt})", list(feedback))
    return "\n".join(lines)


def verifier_prompt(job: Job, claims: Sequence[Claim]) -> str:
    refs = {r for c in claims for r in c.refs}
    lines = ["TASK: check each claim.", moment_line(job), clubs_line(job)]
    lines += _block("EVIDENCE", evidence_lines(job.ledger, refs, windows=True))
    lines += _block("PLAYERS", [p for p in player_lines(job.ledger) if p.split(" | ")[0] in refs])
    lines += _block("CLAIMS", [f"{n}. [{', '.join(c.refs)}] {c.text}" for n, c in enumerate(claims, 1)])
    return "\n".join(lines)


def persona_line(job: Job, personas: Sequence[str]) -> str:
    parts = []
    for persona in personas:
        if persona.startswith(FAV_PREFIX):
            player = job.facts.player(persona[len(FAV_PREFIX):])
            fact = next((p for p in job.ledger.players if p.player_id == player.player_id), None)
            involved = f"involved: {fact.involvement}" if fact else "not involved"
            parts.append(f"{persona} ({player.name}, {job.facts.team(player.team_id).name}, {involved})")
        else:
            parts.append(persona)
    return "PERSONAS: " + " | ".join(parts)


def personalizer_prompt(job: Job, claims: Sequence[Claim], personas: Sequence[str],
                        languages: Sequence[Language]) -> str:
    lines = ["TASK: write every persona in every language.", moment_line(job), clubs_line(job)]
    lines += _block("CLAIMS (verified)", [f"{n}. {c.text}" for n, c in enumerate(claims, 1)])
    lines.append(persona_line(job, personas))
    lines.append("LANGUAGES: " + ", ".join(l.code for l in languages))
    return "\n".join(lines)


def feedback_lines(claims: Sequence[Claim], checks: Sequence[ClaimCheck]) -> list[str]:
    index = {c.claim_id: n for n, c in enumerate(claims, 1)}
    lines = []
    for check in checks:
        if check.ok:
            continue
        for reason in check.reasons:
            lines.append(f"claim {index.get(check.claim_id, '?')}: {reason.code}: {reason.detail}")
    return lines


def to_claims(reply: ExplainerReply, attempt: int) -> list[Claim]:
    return [Claim(claim_id=f"a{attempt}c{n}", text=c.text, team_id=c.team_id, refs=list(c.refs),
                  numbers=list(c.numbers)) for n, c in enumerate(reply.claims, 1)]


def personas_for(job: Job) -> list[str]:
    return [ANALYST, CASUAL, *(f"{FAV_PREFIX}{p}" for p in job.featured)]
