"""The verifier's first layer: deterministic checks of claims and persona texts against the evidence ledger.

A claim passes only if

* it cites evidence (``no_evidence``) that exists in the ledger (``unknown_evidence``);
* every number in its text is listed in its ``numbers`` (``unlisted_number``), cites one of its refs
  (``number_not_cited``) and matches the ledger value to the precision written (``number_mismatch``): "71%" may stand
  for 71.4, "0.4" for 0.39, "6 minutes" for 360 seconds, but "0" may not stand for 0.39;
* each number is attributed to the right club: the club (or player) named nearest to it in its sentence must be
  the club the number belongs to (``team_mismatch``), and the claim's ``team_id`` must be one the evidence is about;
* every score written ("2-1") is a score the match actually had by then (``score_mismatch``);
* every match minute written ("71st minute", "71'") lies in the time window of its evidence or of the job
  (``window_mismatch``);
* every player named is a player in the ledger that the claim cites (``unreferenced_player``);
* no number is joined to the word before it, as in "it is0-1" or "a0.31 xG" (``glued_number``): the number scan
  skips digits that follow a letter, so such a number would otherwise go unchecked. Models do drop spaces
  (gpt-5-mini wrote "it is0-1" in the first live smoke test). Ledger ids such as "e1" are not numbers.

Persona texts get the same checks in their own language, against the numbers the shown claims cite. Numbers are
always digits; a few number words ("three", "tres") are recognised so they cannot slip past.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ..schema import Score
from .contracts import Claim, ClaimCheck, GoalAt, Job, Ledger, LedgerItem, MatchFacts, Reason
from .languages import ENGLISH, Language
from .ledger import UNIT_DECIMALS

_SENTENCE_END = re.compile(r"(?<!\d)[.!?;](?!\d)|[!?;]")
_SCORE = re.compile(r"(?<![\d.,])(\d{1,2})\s*[-–]\s*(\d{1,2})(?!\d|[.,]\d|%)")
_STOPPAGE = re.compile(r"\b(\d{2,3})\s*\+\s*(\d{1,2})'")
_ORDINAL_SUFFIX = re.compile(r"^(?:st|nd|rd|th|º|ª|\.º)\b")
_GLUED = re.compile(r"(?<![\w])([A-Za-zÀ-ÿ]+)(\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+)?)")
_WINDOW_SLACK_S = 60.0


@dataclass(frozen=True)
class CheckContext:
    """What the checks compare against: the job's ledger, clubs, score history and time window."""

    ledger: Ledger
    facts: MatchFacts
    score: Score
    goals: Sequence[GoalAt]
    window_start_s: float
    window_end_s: float
    extra_players: frozenset[str] = frozenset()  # player ids persona texts may name (featured players)

    @classmethod
    def for_job(cls, job: Job) -> "CheckContext":
        return cls(ledger=job.ledger, facts=job.facts, score=job.score, goals=job.goals,
                   window_start_s=job.window_start_s, window_end_s=job.window_end_s,
                   extra_players=frozenset(job.featured))

    def scores(self) -> set[tuple[int, int]]:
        seen = {(0, 0), (self.score.home, self.score.away)}
        seen.update((g.score.home, g.score.away) for g in self.goals)
        return seen


@dataclass
class _Token:
    value: float
    decimals: int
    start: int
    end: int
    minutes: bool  # followed by a minute word: a duration in minutes


@dataclass
class _Scan:
    numbers: list[_Token] = field(default_factory=list)
    scores: list[tuple[int, int, int]] = field(default_factory=list)  # (a, b, position)
    minutes: list[tuple[int, int]] = field(default_factory=list)  # (minute, position)


# --------------------------------------------------------------------------- #
# Scanning text
# --------------------------------------------------------------------------- #


def _number_pattern(lang: Language) -> re.Pattern[str]:
    decimal = r"[.,]" if lang.decimal_sep == "," else r"\."
    return re.compile(rf"(?<![\w.,])([-+−]?)(\d+(?:{decimal}\d+)?)(?![\d])")


def scan(text: str, lang: Language = ENGLISH) -> _Scan:
    result = _Scan()
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b in taken)

    for m in _STOPPAGE.finditer(text):
        result.minutes.append((int(m.group(1)) + int(m.group(2)), m.start()))
        taken.append(m.span())
    for pattern in lang.compiled_clock_patterns():
        for m in pattern.finditer(text):
            if free(*m.span()):
                result.minutes.append((int(m.group(1)), m.start()))
                taken.append(m.span())
    for m in _SCORE.finditer(text):
        if free(*m.span()):
            result.scores.append((int(m.group(1)), int(m.group(2)), m.start()))
            taken.append(m.span())
    minute_words = tuple(w.lower() for w in lang.minute_words)
    for m in _number_pattern(lang).finditer(text):
        if not free(*m.span()):
            continue
        rest = text[m.end():]
        if _ORDINAL_SUFFIX.match(rest):
            continue  # "2nd goal": an ordinal, not a quantity
        sign = -1.0 if m.group(1) in ("-", "−") else 1.0
        digits = m.group(2).replace(",", ".")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        next_word = re.match(r"\s*([A-Za-zÀ-ÿ]+)", rest)
        is_minutes = bool(next_word and next_word.group(1).lower() in minute_words)
        result.numbers.append(_Token(sign * float(digits), decimals, m.start(), m.end(), is_minutes))
    for m in re.finditer(r"[A-Za-zÀ-ÿ]+", text):
        word = m.group(0).lower()
        if word in lang.number_words and free(*m.span()):
            rest = text[m.end():]
            next_word = re.match(r"\s*([A-Za-zÀ-ÿ]+)", rest)
            is_minutes = bool(next_word and next_word.group(1).lower() in minute_words)
            result.numbers.append(_Token(float(lang.number_words[word]), 0, m.start(), m.end(), is_minutes))
    result.numbers.sort(key=lambda t: t.start)
    return result


@dataclass(frozen=True)
class _Mention:
    team_id: str
    start: int
    player_id: Optional[str] = None


def _aliases(facts: MatchFacts, lang: Language) -> list[tuple[re.Pattern[str], str, Optional[str]]]:
    """(pattern, team_id, player_id) for every way the text may name a club or player."""
    out: list[tuple[re.Pattern[str], str, Optional[str]]] = []
    for team in facts.teams:
        words = [team.name, team.name.split()[0]]
        words += list(lang.home_words if team.side == "home" else lang.away_words)
        for word in dict.fromkeys(words):
            out.append((re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE), team.team_id, None))
        out.append((re.compile(rf"\b{re.escape(team.short_name)}\b"), team.team_id, None))
    surnames: dict[str, int] = {}
    for p in facts.roster:
        surname = p.name.split()[-1]
        surnames[surname] = surnames.get(surname, 0) + 1
    for p in facts.roster:
        names = [p.name]
        surname = p.name.split()[-1]
        if surnames[surname] == 1 and len(surname) >= 4:
            names.append(surname)
        for name in names:
            out.append((re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE), p.team_id, p.player_id))
    return out


def mentions(text: str, facts: MatchFacts, lang: Language = ENGLISH) -> list[_Mention]:
    found: list[_Mention] = []
    covered: list[tuple[int, int]] = []
    # Longest aliases first, so "Aurora Rovers" is one mention, not also "Aurora".
    for pattern, team_id, player_id in sorted(_aliases(facts, lang), key=lambda a: -len(a[0].pattern)):
        for m in pattern.finditer(text):
            if any(not (m.end() <= a or m.start() >= b) for a, b in covered):
                continue
            covered.append(m.span())
            found.append(_Mention(team_id, m.start(), player_id))
    return sorted(found, key=lambda x: x.start)


def _sentence_bounds(text: str, position: int) -> tuple[int, int]:
    start = 0
    for m in _SENTENCE_END.finditer(text):
        if m.end() <= position:
            start = m.end()
        else:
            return start, m.start()
    return start, len(text)


def _attributed_team(text: str, token: _Token, found: list[_Mention], default: Optional[str]) -> Optional[str]:
    """The club the text gives this number to: nearest club/player named before it in its sentence, else after."""
    lo, hi = _sentence_bounds(text, token.start)
    before = [m for m in found if lo <= m.start < token.start]
    if before:
        return before[-1].team_id
    after = [m for m in found if token.end <= m.start < hi]
    if after:
        return after[0].team_id
    return default


# --------------------------------------------------------------------------- #
# Number matching
# --------------------------------------------------------------------------- #


def _comparable(item: LedgerItem, token: _Token) -> float:
    if item.unit == "s" and token.minutes:
        return item.value / 60.0
    return item.value


def number_matches(item: LedgerItem, token: _Token) -> bool:
    """True if the written number is the ledger value rounded to the precision written.

    The precision may be at most one decimal coarser than the unit's display precision: "0.4 xG" may stand for
    0.39, but "0 xG" may not stand for 0.048 ("2 m" for 1.7 m and "71%" for 71.4 are fine).
    """
    if not token.minutes and token.decimals < UNIT_DECIMALS.get(item.unit, 0) - 1:
        return False
    return abs(token.value - _comparable(item, token)) <= 0.5 * 10 ** (-token.decimals) + 1e-9


def _same_value(a: float, token: _Token) -> bool:
    return abs(a - token.value) <= 0.5 * 10 ** (-token.decimals) + 1e-9


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _minute_ok(minute: int, windows: Iterable[tuple[float, float]]) -> bool:
    lo_s, hi_s = (minute - 1) * 60.0, minute * 60.0
    return any(lo_s <= end + _WINDOW_SLACK_S and hi_s >= start - _WINDOW_SLACK_S for start, end in windows)


def _common_checks(text: str, scanned: _Scan, ctx: CheckContext, lang: Language,
                   windows: list[tuple[float, float]], allowed_players: set[str]) -> list[Reason]:
    reasons: list[Reason] = []
    for m in _GLUED.finditer(text):
        if not ctx.ledger.has(m.group(0)):
            reasons.append(Reason(code="glued_number",
                                  detail=f'"{m.group(0)}" joins a word and a number; write '
                                         f'"{m.group(1)} {m.group(2)}" with a space'))
    valid_scores = ctx.scores()
    for a, b, _ in scanned.scores:
        if (a, b) not in valid_scores and (b, a) not in valid_scores:
            reasons.append(Reason(code="score_mismatch", detail=f"{a}-{b} is not a score this match has had"))
    for minute, _ in scanned.minutes:
        if not _minute_ok(minute, windows):
            span = ", ".join(f"{int(s // 60) + 1}'-{int(math.ceil(e / 60))}'" for s, e in windows)
            reasons.append(Reason(code="window_mismatch",
                                  detail=f"minute {minute} is outside the evidence window ({span})"))
    for mention in mentions(text, ctx.facts, lang):
        if mention.player_id and mention.player_id not in allowed_players:
            name = ctx.facts.player(mention.player_id).name
            reasons.append(Reason(code="unreferenced_player",
                                  detail=f"{name} is named but is not a player in the cited evidence"))
    return reasons


def check_claim(claim: Claim, ctx: CheckContext, attempt: int = 1, lang: Language = ENGLISH) -> ClaimCheck:
    reasons: list[Reason] = []
    text = claim.text.strip()
    if not text:
        reasons.append(Reason(code="empty_text", detail="the claim has no text"))
    if not claim.refs:
        reasons.append(Reason(code="no_evidence", detail="the claim cites no evidence"))
    unknown = [r for r in claim.refs if not ctx.ledger.has(r)]
    for ref in unknown:
        reasons.append(Reason(code="unknown_evidence", detail=f"{ref} is not in the evidence list"))

    cited = [i for r in claim.refs if (i := ctx.ledger.item(r)) is not None]
    cited_players = {p.player_id for r in claim.refs if (p := ctx.ledger.player(r)) is not None}
    cited_players |= {i.player_id for i in cited if i.player_id}
    teams_cited = {i.team_id for i in cited if i.team_id} | {
        ctx.facts.player(p).team_id for p in cited_players if ctx.facts.player(p)}
    if claim.team_id is not None:
        if ctx.facts.team(claim.team_id) is None:
            reasons.append(Reason(code="team_mismatch", detail=f"{claim.team_id} is not a team in this match"))
        elif teams_cited and claim.team_id not in teams_cited:
            reasons.append(Reason(code="team_mismatch",
                                  detail=f"the claim is about {_name(ctx, claim.team_id)} but its evidence is "
                                         f"about {', '.join(sorted(_name(ctx, t) for t in teams_cited))}"))
        elif cited and not teams_cited:
            reasons.append(Reason(code="team_mismatch",
                                  detail=f"the claim is about {_name(ctx, claim.team_id)} but its evidence is "
                                         "match-wide (both clubs); set team_id to null"))

    scanned = scan(text, lang)
    found = mentions(text, ctx.facts, lang)
    for token in scanned.numbers:
        # Numbers should be listed exactly as written; a listed value that rounds to the text is the fallback.
        listed = [n for n in claim.numbers if abs(n.value - token.value) <= 1e-6] or [
            n for n in claim.numbers if _same_value(n.value, token)]
        if not listed:
            reasons.append(Reason(code="unlisted_number",
                                  detail=f"{_shown(text, token)} appears in the text but not in the claim's numbers"))
            continue
        given_to = _attributed_team(text, token, found, claim.team_id)
        # Equal values ("4 shots each"): use the number that belongs to the club the text names.
        listed.sort(key=lambda n: (getattr(ctx.ledger.item(n.ref), "team_id", None) != given_to))
        number = listed[0]
        if number.ref not in claim.refs:
            reasons.append(Reason(code="number_not_cited",
                                  detail=f"{_shown(text, token)} uses {number.ref}, which the claim does not cite"))
            continue
        item = ctx.ledger.item(number.ref)
        if item is None:
            if ctx.ledger.player(number.ref) is not None:
                reasons.append(Reason(code="number_mismatch",
                                      detail=f"{number.ref} is a player, not a number"))
            continue  # unknown refs are reported above
        if not number_matches(item, token):
            reasons.append(Reason(code="number_mismatch",
                                  detail=f"{_shown(text, token)} does not match {item.ref} ({item.metric} = "
                                         f"{item.value:g} {item.unit}; write \"{item.say}\")"))
            continue
        if item.team_id and given_to is not None and given_to != item.team_id:
            reasons.append(Reason(code="team_mismatch",
                                  detail=f"{_shown(text, token)} ({item.ref}) belongs to "
                                         f"{_name(ctx, item.team_id)}, but the text gives it to "
                                         f"{_name(ctx, given_to)}; name the club right before its number"))

    windows = [(i.window_start_s, i.window_end_s) for i in cited] + [(ctx.window_start_s, ctx.window_end_s)]
    reasons += _common_checks(text, scanned, ctx, lang, windows, cited_players)
    return ClaimCheck(claim_id=claim.claim_id, attempt=attempt, layer="deterministic", ok=not reasons,
                      reasons=reasons)


def check_text(text: str, ctx: CheckContext, refs: Iterable[str], lang: Language = ENGLISH) -> list[Reason]:
    """Check a persona text: its numbers must be numbers the shown claims cite (``refs``), correctly attributed."""
    items = [i for r in dict.fromkeys(refs) if (i := ctx.ledger.item(r)) is not None]
    reasons: list[Reason] = []
    if not text.strip():
        return [Reason(code="empty_text", detail="the text is empty")]
    scanned = scan(text, lang)
    found = mentions(text, ctx.facts, lang)
    for token in scanned.numbers:
        matching = [i for i in items if number_matches(i, token)]
        if not matching:
            reasons.append(Reason(code="unlisted_number",
                                  detail=f"{_shown(text, token)} is not one of the verified numbers"))
            continue
        given_to = _attributed_team(text, token, found, None)
        if given_to is not None and all(i.team_id is not None and i.team_id != given_to for i in matching):
            reasons.append(Reason(code="team_mismatch",
                                  detail=f"{_shown(text, token)} is given to {_name(ctx, given_to)}, but that "
                                         f"number belongs to {_name(ctx, matching[0].team_id)}"))
    allowed = {p.player_id for p in ctx.ledger.players} | set(ctx.extra_players)
    windows = [(i.window_start_s, i.window_end_s) for i in items] + [(ctx.window_start_s, ctx.window_end_s)]
    return reasons + _common_checks(text, scanned, ctx, lang, windows, allowed)


def _name(ctx: CheckContext, team_id: Optional[str]) -> str:
    team = ctx.facts.team(team_id)
    return team.name if team else str(team_id)


def _shown(text: str, token: _Token) -> str:
    return f'"{text[token.start:token.end]}"'
