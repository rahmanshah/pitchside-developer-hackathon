"""Build the evidence ledger for a job, and format numbers the way the claims must quote them.

Every number a model may use gets an id here. Finding evidence becomes ``e*`` items, match totals and player
numbers ``c*`` items, involved players ``p*`` facts. A recap's ledger is built from the claims already verified
earlier in the match, plus the totals and player numbers at the whistle.
"""

from __future__ import annotations

from typing import Iterable, Literal, Optional, Sequence

from ..schema import (
    Event, MatchStartEvent, PassEvent, PossessionChangeEvent, PressureEvent, Score, ShotEvent, TackleEvent,
)
from ..stats.detector import DetectorConfig
from ..stats.engine import Snapshot
from ..stats.findings import SPELL_KINDS, Finding, FindingKind
from .contracts import EarlierClaim, JobRecord, Ledger, MatchFacts, PlayerRef, PlayerStats, TeamRef
from .languages import ENGLISH, Language

UNIT_DECIMALS = {"pct": 0, "xg": 2, "index": 2, "per_min": 1, "s": 0, "m": 0, "count": 0}
MAX_RECAP_CLAIMS = 12


UNSIGNED_INDEX = frozenset({"pass_difficulty"})  # 0..1 ratings, not -1..+1 swings: no "+" sign
# Yes/no facts stored as 1/0: quoted in words, never as a digit ("(1)" read as a count in live output).
FLAG_METRICS = frozenset({"possession_won_in_open_play", "ended_with_shot"})
# How a shot ended, stored as the index into SHOT_OUTCOMES (decision 24). Live text invented outcomes the evidence
# did not hold ("rattles the bar" for a blocked shot), so goals and big chances carry the real one.
SHOT_OUTCOMES = ("off_target", "blocked", "saved", "goal")
# Facts stated in words: no number in a text can match them (the checker never accepts a digit for one).
WORD_METRICS = FLAG_METRICS | {"shot_outcome"}
# Match-wide numbers over the chaos window: without it named, "79% pass completion" reads as the whole match, and
# "both teams averaged" as each club's figure, so they say "across both teams in this spell" (decisions 21, 23).
SPELL_WINDOW_METRICS = frozenset({"chaos_index", "turnovers_per_min", "pass_completion", "mean_spell"})
# One club's counts and sums over a rolling window: "3 shots" alone reads as a match total (decision 23).
WINDOW_COUNT_METRICS = frozenset({"shots", "xg", "tackles_won", "opponent_final_third_passes", "final_third_passes",
                                  "passes", "pressures"})
# Possessions that begin this way were won in open play; the others began with a restart or a save.
OPEN_PLAY_REGAINS = frozenset(DetectorConfig().counter_regains)
Window = Literal["spell", "recent", "earlier"]


def number_text(value: float, unit: str, lang: Language = ENGLISH, metric: str = "") -> str:
    """The number as a claim should write it: digits, unit-specific rounding, unit symbol where natural."""
    if unit == "pct":
        return f"{lang.number(value, 0)}%"
    if unit == "index":
        if metric in UNSIGNED_INDEX:
            return lang.number(value, 2)
        sign = "+" if value > 0.005 else "-" if value < -0.005 else ""
        return sign + lang.number(abs(value), 2)
    if unit == "s":
        return f"{lang.number(value / 60, 0)} min" if value >= 90 else f"{lang.number(value, 0)} s"
    if unit == "m":
        return f"{lang.number(value, 0)} m"
    return lang.number(value, UNIT_DECIMALS.get(unit, 2))


def _phrase(lang: Language, key: str, value: float, unit: str) -> Optional[str]:
    """The phrase for ``key``, in the singular when the count is 1 ("1 pass in the move")."""
    if unit == "count" and value == 1 and f"{key}.one" in lang.phrases:
        return lang.phrases[f"{key}.one"]
    return lang.phrases.get(key)


def say(metric: str, value: float, unit: str, lang: Language = ENGLISH, team_name: str = "",
        cause: Optional[str] = None) -> str:
    """A short phrase quoting the number, e.g. "71% field tilt" or "Ironvale are completing 64% of their passes".

    Since-kick-off metrics end in ``_total``; those without their own phrase reuse the base metric's phrase. Flags
    are clauses with no number ("Aurora Rovers won the ball in open play"). ``cause`` is how the possession began
    (a ``PossessionChangeEvent`` reason): a restart or a save is named as such, and its clock reads "after gaining
    possession", not "after winning the ball".
    """
    if metric == "shot_outcome":
        return lang.phrases[f"shot_outcome.{word_value(metric, value)}"]
    if metric in FLAG_METRICS:
        key = f"fact.{metric}" if value >= 0.5 else f"fact.{metric}.no"
        if value < 0.5 and metric == "possession_won_in_open_play" and f"possession_from.{cause}" in lang.phrases:
            key = f"possession_from.{cause}"
        return lang.phrases[key].format(team=team_name).strip()
    v = number_text(value, unit, lang, metric)
    if metric == "seconds_since_turnover" and cause is not None and cause not in OPEN_PLAY_REGAINS:
        metric = "seconds_since_possession"
    phrase = _phrase(lang, f"fact.{metric}", value, unit)
    if phrase is None and metric.endswith("_total") and f"fact.{metric[:-6]}" in lang.phrases:
        base = _phrase(lang, f"fact.{metric[:-6]}", value, unit).format(v=v, opponent=team_name)
        return lang.phrases["fact.since_kickoff"].format(phrase=base)
    return (phrase or "{v} " + metric.replace("_", " ")).format(v=v, opponent=team_name)


def word_value(metric: str, value: float) -> Optional[str]:
    """The word a word fact is shown as in prompts ("yes", "saved"); None for a number."""
    if metric == "shot_outcome":
        return SHOT_OUTCOMES[int(round(value))]
    if metric in FLAG_METRICS:
        return "yes" if value >= 0.5 else "no"
    return None


def window_say(metric: str, value: float, unit: str, window: Window, lang: Language = ENGLISH,
               team_name: str = "") -> str:
    """A rolling number with its window named once, in words: "3 shots in this spell", or for the match-wide chaos
    numbers "3.0 turnovers per minute across both teams in this spell"."""
    where = lang.phrases[f"window.{window}"]
    if metric in SPELL_WINDOW_METRICS:
        return lang.phrases[f"combined.{metric}"].format(v=number_text(value, unit, lang, metric), window=where)
    return f"{say(metric, value, unit, lang, team_name)} {where}"


def player_say(metric: str, value: float, player: str, lang: Language = ENGLISH, scored_this: bool = False) -> str:
    """A player's number since kick-off as a clause: "Rafa Yaldiran has 2 goals in the match, including this one"."""
    unit = "xg" if metric == "player_xg" else "count"
    key = {"player_goals": "player.goals_this" if scored_this else "player.goals", "player_shots": "player.shots",
           "player_xg": "player.xg"}[metric]
    return _phrase(lang, key, value, unit).format(player=player, v=number_text(value, unit, lang))


def goal_effect(margin_after: float, goals_in_match: int) -> str:
    """What a goal did, from the scoring club's margin after it: opener, equaliser, ahead, extend or back."""
    if margin_after == 1 and goals_in_match == 1:
        return "opener"
    if margin_after == 0:
        return "equaliser"
    if margin_after == 1:
        return "ahead"
    return "extend" if margin_after > 1 else "back"


def goal_margin_say(margin_after: float, team_name: str, score: Score, lang: Language = ENGLISH) -> str:
    """The score after a goal, then what it did: "it is 1-1: Aurora Rovers equalise". The scoring club is the last
    club named, so a number pasted after it is read as that club's (with "Home 1-1 Away" it was the away club's)."""
    effect = lang.phrases[f"goal_effect.{goal_effect(margin_after, score.home + score.away)}"].format(team=team_name)
    return lang.phrases["goal_margin.line"].format(score=f"{score.home}-{score.away}", effect=effect)


# --------------------------------------------------------------------------- #
# Match facts and player numbers
# --------------------------------------------------------------------------- #


def match_facts(start: MatchStartEvent) -> MatchFacts:
    def team(info) -> TeamRef:
        return TeamRef(team_id=info.team_id, name=info.name, short_name=info.short_name, side=info.side.value)

    roster = [PlayerRef(player_id=p.player_id, name=p.name, team_id=t.team_id, position=p.position)
              for t in (start.home, start.away) for p in t.players]
    return MatchFacts(match_id=start.match_id, home=team(start.home), away=team(start.away), roster=roster)


def default_featured(facts: MatchFacts) -> list[str]:
    """One featured player per club for the favourite-player persona: the centre forward (ST), else the first."""
    featured = []
    for team in facts.teams:
        squad = [p for p in facts.roster if p.team_id == team.team_id]
        striker = next((p for p in squad if p.position == "ST"), squad[0])
        featured.append(striker.player_id)
    return featured


class PlayerTally:
    """Per-player numbers since kick-off, updated event by event (deterministic, no LLM)."""

    def __init__(self) -> None:
        self._stats: dict[str, dict] = {}

    def _row(self, player_id: str, team_id: str) -> dict:
        return self._stats.setdefault(player_id, {"player_id": player_id, "team_id": team_id, "goals": 0,
                                                  "shots": 0, "xg": 0.0, "passes": 0, "passes_completed": 0,
                                                  "tackles_won": 0, "pressures": 0})

    def update(self, event: Event) -> None:
        if isinstance(event, PassEvent):
            row = self._row(event.player_id, event.team_id)
            row["passes"] += 1
            row["passes_completed"] += event.outcome == "complete"
        elif isinstance(event, ShotEvent):
            row = self._row(event.player_id, event.team_id)
            row["shots"] += 1
            row["xg"] = round(row["xg"] + event.xg, 3)
            row["goals"] += event.outcome == "goal"
        elif isinstance(event, TackleEvent) and event.outcome == "won":
            self._row(event.player_id, event.team_id)["tackles_won"] += 1
        elif isinstance(event, PressureEvent):
            self._row(event.player_id, event.team_id)["pressures"] += 1

    def get(self, player_id: str, team_id: str) -> PlayerStats:
        return PlayerStats(**self._stats.get(player_id, {"player_id": player_id, "team_id": team_id}))

    def all(self) -> list[PlayerStats]:
        return [PlayerStats(**row) for _, row in sorted(self._stats.items())]


def _add_player_numbers(ledger: Ledger, stats: PlayerStats, clock_s: float, facts: MatchFacts,
                        scored_this: bool = False) -> None:
    name = (facts.player(stats.player_id) or PlayerRef(player_id=stats.player_id, name=stats.player_id,
                                                       team_id=stats.team_id, position="CM")).name
    for metric, value, unit in (("player_goals", stats.goals, "count"), ("player_shots", stats.shots, "count"),
                                ("player_xg", stats.xg, "xg")):
        # The scorer's tally ends at this goal's second; it says so, or "his second goal" reads as unsupported.
        phrase = player_say(metric, value, name, scored_this=scored_this and metric == "player_goals")
        ledger.add_item("c", metric=metric, team_id=stats.team_id, player_id=stats.player_id, value=float(value),
                        unit=unit, window_start_s=0.0, window_end_s=clock_s, say=phrase, source="player")


# --------------------------------------------------------------------------- #
# Ledgers
# --------------------------------------------------------------------------- #


def finding_ledger(finding: Finding, facts: MatchFacts, events_by_id: dict[int, Event],
                   tally: Optional[PlayerTally] = None) -> Ledger:
    ledger = Ledger()
    spell = finding.kind in SPELL_KINDS
    for ev in finding.evidence:
        team = facts.team(ev.team_id)
        name = team.name if team else ""
        if ev.metric in SPELL_WINDOW_METRICS and ev.team_id is None:
            phrase = window_say(ev.metric, ev.value, ev.unit, "spell")
        elif spell and ev.metric in WINDOW_COUNT_METRICS and team is not None:
            phrase = window_say(ev.metric, ev.value, ev.unit, "spell", team_name=name)
        elif ev.metric == "goal_margin" and team is not None:
            phrase = goal_margin_say(ev.value, name, finding.score)
        else:
            cause = (_possession_cause(events_by_id, ev.team_id, ev.window_start_s)
                     if ev.metric in ("possession_won_in_open_play", "seconds_since_turnover") else None)
            phrase = say(ev.metric, ev.value, ev.unit, ENGLISH, name, cause=cause)
        ledger.add_item("e", metric=ev.metric, team_id=ev.team_id, value=ev.value, unit=ev.unit,
                        window_start_s=ev.window_start_s, window_end_s=ev.window_end_s, baseline=ev.baseline,
                        say=phrase, source="finding")
    shot = events_by_id.get(finding.event_ids[0]) if finding.event_ids else None
    if finding.kind in (FindingKind.GOAL, FindingKind.BIG_CHANCE) and isinstance(shot, ShotEvent):
        # The finding's own shot event, which emitted it: its outcome is known at detection, nothing later.
        value = float(SHOT_OUTCOMES.index(shot.outcome))
        ledger.add_item("e", metric="shot_outcome", team_id=shot.team_id, value=value, unit="count",
                        window_start_s=shot.clock_s, window_end_s=shot.clock_s,
                        say=say("shot_outcome", value, "count"), source="finding")
    for event_id in finding.event_ids:
        event = events_by_id.get(event_id)
        if isinstance(event, ShotEvent):
            involvement = "scorer" if event.outcome == "goal" else "shooter"
            _involve(ledger, facts, event.player_id, involvement, event.clock_s, tally, finding.detected_at_s)
        elif isinstance(event, PossessionChangeEvent) and event.player_id and finding.kind is FindingKind.COUNTER_ATTACK:
            _involve(ledger, facts, event.player_id, "ball_winner", event.clock_s, tally, finding.detected_at_s)
    return ledger


def _possession_cause(events_by_id: dict[int, Event], team_id: Optional[str], start_s: float) -> Optional[str]:
    """How the possession that began at ``start_s`` started (its ``PossessionChangeEvent`` reason), from the events
    seen so far; None if it is not among them."""
    for event in reversed(list(events_by_id.values())):
        if isinstance(event, PossessionChangeEvent) and event.team_id == team_id and event.clock_s == start_s:
            return event.reason
    return None


def _involve(ledger: Ledger, facts: MatchFacts, player_id: str, involvement: str, clock_s: float,
             tally: Optional[PlayerTally], now_s: float) -> None:
    player = facts.player(player_id)
    if player is None:
        return
    ledger.add_player(player_id=player_id, name=player.name, team_id=player.team_id, involvement=involvement,
                      clock_s=clock_s)
    if tally is not None:
        _add_player_numbers(ledger, tally.get(player_id, player.team_id), now_s, facts,
                            scored_this=involvement == "scorer")


def totals_items(ledger: Ledger, snapshot: Snapshot, facts: MatchFacts) -> list[str]:
    """Since-kick-off totals for both clubs as ``c*`` items; returns their refs."""
    refs = []
    for team in facts.teams:
        totals = snapshot.totals[team.team_id]
        for metric, value, unit in (("possession_total", totals.possession_pct, "pct"),
                                    ("shots_total", float(totals.shots), "count"),
                                    ("xg_total", totals.xg, "xg"),
                                    ("pass_completion_total", totals.pass_completion_pct, "pct")):
            if value is None:
                continue
            item = ledger.add_item("c", metric=metric, team_id=team.team_id, value=round(value, 3), unit=unit,
                                   window_start_s=0.0, window_end_s=snapshot.clock_s,
                                   say=f"{team.name}: {say(metric, value, unit)}", source="context")
            refs.append(item.ref)
    return refs


def recap_ledger(snapshot: Snapshot, facts: MatchFacts, tally: PlayerTally, featured: Sequence[str],
                 goals_scorers: Iterable[str], records: Sequence[JobRecord]) -> tuple[Ledger, list[EarlierClaim]]:
    """Totals, player numbers (scorers, each club's top xG, featured players) and earlier verified claims."""
    ledger = Ledger()
    totals_items(ledger, snapshot, facts)
    stats = {s.player_id: s for s in tally.all()}
    chosen: list[tuple[str, str]] = [(pid, "scorer") for pid in dict.fromkeys(goals_scorers)]
    for team in facts.teams:
        shooters = [s for s in stats.values() if s.team_id == team.team_id and s.shots > 0]
        if shooters:
            top = max(shooters, key=lambda s: (s.xg, s.shots, s.player_id))
            chosen.append((top.player_id, "top_xg"))
    chosen += [(pid, "featured") for pid in featured]
    for player_id, involvement in chosen:
        player = facts.player(player_id)
        if player is None or ledger_has_player(ledger, player_id):
            continue
        ledger.add_player(player_id=player_id, name=player.name, team_id=player.team_id, involvement=involvement)
        _add_player_numbers(ledger, stats.get(player_id) or tally.get(player_id, player.team_id),
                            snapshot.clock_s, facts)

    earlier: list[EarlierClaim] = []
    shown = [r for r in records if r.narrative.source == "model" and r.kind == "finding"]
    # Goals first, then the most recent moments, up to MAX_RECAP_CLAIMS claims.
    shown.sort(key=lambda r: (r.label != "goal", -r.clock_s))
    for record in shown:
        for claim in record.narrative.claims:
            if len(earlier) >= MAX_RECAP_CLAIMS:
                break
            refs = []
            for ref in claim.refs:
                item = record.ledger.item(ref)
                if item is not None:
                    copy = ledger.add_item("c", **{**item.model_dump(exclude={"ref", "source", "origin"}),
                                                   "source": "claim", "origin": record.job_id})
                    refs.append(copy.ref)
                elif (fact := record.ledger.player(ref)) is not None:
                    refs.append(ledger.add_player(**fact.model_dump(exclude={"ref"})).ref)
            earlier.append(EarlierClaim(job_id=record.job_id, minute=int(record.clock_s // 60) + 1,
                                        label=record.label, text=claim.text, refs=refs))
    earlier.sort(key=lambda c: (c.minute, c.job_id))
    return ledger, earlier


def ledger_has_player(ledger: Ledger, player_id: str) -> bool:
    return any(p.player_id == player_id for p in ledger.players)
