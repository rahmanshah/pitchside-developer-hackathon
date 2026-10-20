"""Build the evidence ledger for a job, and format numbers the way the claims must quote them.

Every number a model may use gets an id here. Finding evidence becomes ``e*`` items, match totals and player
numbers ``c*`` items, involved players ``p*`` facts. A recap's ledger is built from the claims already verified
earlier in the match, plus the totals and player numbers at the whistle.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from ..schema import Event, MatchStartEvent, PassEvent, PossessionChangeEvent, PressureEvent, ShotEvent, TackleEvent
from ..stats.engine import Snapshot
from ..stats.findings import Finding, FindingKind
from .contracts import EarlierClaim, JobRecord, Ledger, MatchFacts, PlayerRef, PlayerStats, TeamRef
from .languages import ENGLISH, Language

UNIT_DECIMALS = {"pct": 0, "xg": 2, "index": 2, "per_min": 1, "s": 0, "m": 0, "count": 0}
MAX_RECAP_CLAIMS = 12


def number_text(value: float, unit: str, lang: Language = ENGLISH) -> str:
    """The number as a claim should write it: digits, unit-specific rounding, unit symbol where natural."""
    if unit == "pct":
        return f"{lang.number(value, 0)}%"
    if unit == "index":
        sign = "+" if value > 0.005 else "-" if value < -0.005 else ""
        return sign + lang.number(abs(value), 2)
    if unit == "s":
        return f"{lang.number(value / 60, 0)} min" if value >= 90 else f"{lang.number(value, 0)} s"
    if unit == "m":
        return f"{lang.number(value, 0)} m"
    return lang.number(value, UNIT_DECIMALS.get(unit, 2))


def say(metric: str, value: float, unit: str, lang: Language = ENGLISH, team_name: str = "") -> str:
    """A short phrase quoting the number, e.g. "71% field tilt" or "Ironvale completing 64% of their passes"."""
    phrase = lang.phrases.get(f"fact.{metric}", "{v} " + metric.replace("_", " "))
    return phrase.format(v=number_text(value, unit, lang), opponent=team_name)


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


def _add_player_numbers(ledger: Ledger, stats: PlayerStats, clock_s: float, facts: MatchFacts) -> None:
    name = (facts.player(stats.player_id) or PlayerRef(player_id=stats.player_id, name=stats.player_id,
                                                       team_id=stats.team_id, position="CM")).name
    for metric, value, unit in (("player_goals", stats.goals, "count"), ("player_shots", stats.shots, "count"),
                                ("player_xg", stats.xg, "xg")):
        ledger.add_item("c", metric=metric, team_id=stats.team_id, player_id=stats.player_id, value=float(value),
                        unit=unit, window_start_s=0.0, window_end_s=clock_s,
                        say=f"{name}: {say(metric, value, unit)}", source="player")


# --------------------------------------------------------------------------- #
# Ledgers
# --------------------------------------------------------------------------- #


def finding_ledger(finding: Finding, facts: MatchFacts, events_by_id: dict[int, Event],
                   tally: Optional[PlayerTally] = None) -> Ledger:
    ledger = Ledger()
    for ev in finding.evidence:
        team = facts.team(ev.team_id)
        ledger.add_item("e", metric=ev.metric, team_id=ev.team_id, value=ev.value, unit=ev.unit,
                        window_start_s=ev.window_start_s, window_end_s=ev.window_end_s, baseline=ev.baseline,
                        say=say(ev.metric, ev.value, ev.unit, ENGLISH, team.name if team else ""), source="finding")
    for event_id in finding.event_ids:
        event = events_by_id.get(event_id)
        if isinstance(event, ShotEvent):
            involvement = "scorer" if event.outcome == "goal" else "shooter"
            _involve(ledger, facts, event.player_id, involvement, event.clock_s, tally, finding.detected_at_s)
        elif isinstance(event, PossessionChangeEvent) and event.player_id and finding.kind is FindingKind.COUNTER_ATTACK:
            _involve(ledger, facts, event.player_id, "ball_winner", event.clock_s, tally, finding.detected_at_s)
    return ledger


def _involve(ledger: Ledger, facts: MatchFacts, player_id: str, involvement: str, clock_s: float,
             tally: Optional[PlayerTally], now_s: float) -> None:
    player = facts.player(player_id)
    if player is None:
        return
    ledger.add_player(player_id=player_id, name=player.name, team_id=player.team_id, involvement=involvement,
                      clock_s=clock_s)
    if tally is not None:
        _add_player_numbers(ledger, tally.get(player_id, player.team_id), now_s, facts)


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
