"""Deterministic narratives: used for findings that do not go to the models, and as every fallback.

A template builds the same things the agents do: English claims that cite ledger ids (so the checker can verify
them, and a test does, for every finding of many matches) and persona texts in every configured language.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ..stats.findings import SPELL_KINDS, FindingKind
from .contracts import ANALYST, CASUAL, Claim, ClaimNumber, Job, LedgerItem, PersonaText, fav_persona
from .languages import ENGLISH, Language
from .ledger import number_text

MAX_SPELL_FACTS = 4


@dataclass(frozen=True)
class TemplateOutput:
    headline: str
    claims: list[Claim]
    texts: list[PersonaText]


def _written(item: LedgerItem) -> float:
    """The number as the English text writes it (e.g. 71 for "71%", 6 for "6 min")."""
    text = number_text(item.value, item.unit, ENGLISH)
    return float(text.split()[0].rstrip("%").lstrip("+"))


class _Builder:
    def __init__(self, job: Job, languages: Sequence[Language], reason: Optional[str]) -> None:
        self.job = job
        self.languages = languages
        self.reason = reason
        self.facts = job.facts
        self.texts: list[PersonaText] = []
        self.claims: list[Claim] = []

    # -- helpers --------------------------------------------------------- #
    def item(self, metric: str, team_id: Optional[str] = None, player_id: Optional[str] = None) -> Optional[LedgerItem]:
        for item in self.job.ledger.items:
            if item.metric == metric and (team_id is None or item.team_id == team_id) and (
                    player_id is None or item.player_id == player_id):
                return item
        return None

    def num(self, item: LedgerItem, lang: Language) -> str:
        return number_text(item.value, item.unit, lang)

    def score(self, lang: Language) -> str:
        s = self.job.score
        return lang.phrases["score"].format(home=self.facts.home.name, home_goals=s.home, away_goals=s.away,
                                            away=self.facts.away.name)

    def team_name(self, team_id: Optional[str]) -> str:
        team = self.facts.team(team_id)
        return team.name if team else ""

    def claim(self, text: str, items: Sequence[LedgerItem], players: Sequence[str] = (),
              team_id: Optional[str] = None) -> None:
        refs = [i.ref for i in items] + list(players)
        numbers = [ClaimNumber(value=_written(i), ref=i.ref) for i in items]
        self.claims.append(Claim(claim_id=f"t{len(self.claims) + 1}", text=text, team_id=team_id,
                                 refs=list(dict.fromkeys(refs)), numbers=numbers))

    def add(self, persona: str, lang: Language, text: str) -> None:
        self.texts.append(PersonaText(persona=persona, language=lang.code, text=text, source="template",
                                      reason=self.reason))

    def player_fact(self, involvement: Sequence[str]):
        return next((p for p in self.job.ledger.players if p.involvement in involvement), None)

    def fav_variant(self, player_id: str, involved_ids: set[str]) -> str:
        if player_id in involved_ids:
            return "fav_involved"
        player = self.facts.player(player_id)
        if self.job.team_id is None:
            return "fav_match"
        return "fav_team" if player and player.team_id == self.job.team_id else "fav_other"

    def output(self, headline: str) -> TemplateOutput:
        return TemplateOutput(headline=headline, claims=self.claims, texts=self.texts)


# --------------------------------------------------------------------------- #
# Per kind
# --------------------------------------------------------------------------- #


def _shot(b: _Builder, kind: str) -> TemplateOutput:
    team = b.job.team_id
    xg, dist = b.item("shot_xg", team), b.item("shot_distance", team)
    shots, mxg = b.item("match_shots", team), b.item("match_xg", team)
    shooter = b.player_fact(("scorer", "shooter"))
    involved = {shooter.player_id} if shooter else set()
    for lang in b.languages:
        values = dict(team=b.team_name(team), score=b.score(lang), shot_xg=b.num(xg, lang),
                      shot_distance=b.num(dist, lang), match_shots=b.num(shots, lang), match_xg=b.num(mxg, lang),
                      opponent=b.facts.opponent(team).name)
        analyst = lang.phrases[f"{kind}.analyst"].format(**values)
        b.add(ANALYST, lang, analyst)
        b.add(CASUAL, lang, lang.phrases[f"{kind}.casual"].format(**values))
        for player_id in b.job.featured:
            variant = b.fav_variant(player_id, involved)
            name = shooter.name if variant == "fav_involved" else b.facts.player(player_id).name
            phrase = lang.phrases.get(f"{kind}.{variant}") or lang.phrases[f"{kind}.casual"]
            b.add(fav_persona(player_id), lang, phrase.format(player=name, **values))
        if lang.code == "en":
            b.claim(analyst, [xg, dist, shots, mxg], team_id=team)
    return b.output(next(t.text for t in b.texts if t.persona == CASUAL))


def _spell_items(b: _Builder) -> list[LedgerItem]:
    team = b.job.team_id
    evidence = [i for i in b.job.ledger.items if i.source == "finding" and i.metric != "spell_duration"]
    own = [i for i in evidence if i.team_id in (team, None)]
    other = [i for i in evidence if i.team_id not in (team, None)]
    return (own + other)[:MAX_SPELL_FACTS] if len(own) >= MAX_SPELL_FACTS else own[:MAX_SPELL_FACTS - 1] + other[:1]


def _spell_start(b: _Builder, kind: str) -> TemplateOutput:
    team = b.job.team_id
    items = _spell_items(b)
    from .ledger import say

    for lang in b.languages:
        facts = ", ".join(say(i.metric, i.value, i.unit, lang, b.team_name(i.team_id)) for i in items)
        label = lang.phrases[f"{kind}.label"]
        values = dict(team=b.team_name(team), opponent=b.facts.opponent(team).name if team else "",
                      label=label, label_cap=label[:1].upper() + label[1:], facts=facts)
        key = "spell.analyst" if team else "spell.analyst_match"
        analyst = lang.phrases[key].format(**values)
        b.add(ANALYST, lang, analyst)
        b.add(CASUAL, lang, lang.phrases[f"{kind}.casual"].format(**values))
        for player_id in b.job.featured:
            variant = b.fav_variant(player_id, set())
            b.add(fav_persona(player_id), lang,
                  lang.phrases[f"spell.{variant}"].format(player=b.facts.player(player_id).name, **values))
        if lang.code == "en":
            b.claim(analyst, items, team_id=team)
    return b.output(next(t.text for t in b.texts if t.persona == CASUAL))


def _spell_end(b: _Builder, kind: str) -> TemplateOutput:
    team = b.job.team_id
    duration = b.item("spell_duration")
    for lang in b.languages:
        values = dict(team=b.team_name(team), label_end=lang.phrases[f"{kind}.label_end"],
                      duration=b.num(duration, lang))
        text = lang.phrases["spell_end" if team else "spell_end_match"].format(**values)
        for persona in [ANALYST, CASUAL, *map(fav_persona, b.job.featured)]:
            b.add(persona, lang, text)
        if lang.code == "en":
            b.claim(text, [duration], team_id=team)
    return b.output(next(t.text for t in b.texts if t.persona == CASUAL))


def _counter(b: _Builder) -> TemplateOutput:
    team = b.job.team_id
    gained, seconds = b.item("metres_gained", team), b.item("seconds_to_finish", team)
    for lang in b.languages:
        text = lang.phrases["counter_attack"].format(team=b.team_name(team), metres_gained=b.num(gained, lang),
                                                     seconds_to_finish=b.num(seconds, lang))
        for persona in [ANALYST, CASUAL, *map(fav_persona, b.job.featured)]:
            b.add(persona, lang, text)
        if lang.code == "en":
            b.claim(text, [gained, seconds], team_id=team)
    return b.output(next(t.text for t in b.texts if t.persona == CASUAL))


def _recap(b: _Builder) -> TemplateOutput:
    home, away = b.facts.home.team_id, b.facts.away.team_id
    totals = {(m, t): b.item(m, t) for m in ("shots_total", "xg_total", "possession_total") for t in (home, away)}
    scorers = []
    for goal in b.job.goals:
        fact = next((p for p in b.job.ledger.players if p.player_id == goal.player_id), None)
        if fact is not None and fact.ref not in [s.ref for s in scorers]:
            scorers.append(fact)
    for lang in b.languages:
        period = lang.phrases[f"recap.{b.job.label}"]
        values = dict(period=period, score=b.score(lang), home=b.facts.home.name, away=b.facts.away.name,
                      home_shots=b.num(totals["shots_total", home], lang),
                      away_shots=b.num(totals["shots_total", away], lang),
                      home_xg=b.num(totals["xg_total", home], lang), away_xg=b.num(totals["xg_total", away], lang),
                      home_possession=b.num(totals["possession_total", home], lang),
                      away_possession=b.num(totals["possession_total", away], lang))
        analyst = lang.phrases["recap.analyst"].format(**values)
        casual = lang.phrases["recap.casual"].format(**values)
        goals_text = ""
        if scorers:
            names = ", ".join(f"{p.name} ({b.team_name(p.team_id)})" for p in scorers)
            goals_text = " " + lang.phrases["recap.goals"].format(scorers=names)
        b.add(ANALYST, lang, analyst + goals_text)
        b.add(CASUAL, lang, casual + goals_text)
        for player_id in b.job.featured:
            player = b.facts.player(player_id)
            g, s, x = (b.item(m, player_id=player_id) for m in ("player_goals", "player_shots", "player_xg"))
            if s is not None and s.value > 0:
                fav = lang.phrases["recap.fav"].format(player=player.name, goals=b.num(g, lang),
                                                       shots=b.num(s, lang), xg=b.num(x, lang))
            else:
                fav = lang.phrases["recap.fav_quiet"].format(player=player.name)
            b.add(fav_persona(player_id), lang, f"{casual} {fav}")
            if lang.code == "en" and s is not None and s.value > 0:
                fact = next((p.ref for p in b.job.ledger.players if p.player_id == player_id), None)
                b.claim(fav, [g, s, x], players=[fact] if fact else [], team_id=player.team_id)
        if lang.code == "en":
            b.claim(analyst, list(totals.values()))
            if scorers:
                b.claim(goals_text.strip(), [], players=[p.ref for p in scorers])
    return b.output(next(t.text for t in b.texts if t.persona == CASUAL and t.language == "en"))


def template_narrative(job: Job, languages: Sequence[Language], reason: Optional[str] = None) -> TemplateOutput:
    """The deterministic narrative for any job: claims (English) and persona texts (every language)."""
    b = _Builder(job, languages, reason)
    if job.kind == "recap":
        return _recap(b)
    finding = job.finding
    assert finding is not None
    kind = finding.kind
    if kind in (FindingKind.GOAL, FindingKind.BIG_CHANCE):
        return _shot(b, kind.value)
    if kind is FindingKind.COUNTER_ATTACK:
        return _counter(b)
    if kind in SPELL_KINDS:
        return _spell_end(b, kind.value) if finding.status == "end" else _spell_start(b, kind.value)
    raise ValueError(f"no template for {kind}")
