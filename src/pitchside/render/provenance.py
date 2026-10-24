"""The evidence chain behind an overlay: shown text -> claims -> ledger numbers -> metric and window -> events.

Two relations between a number and the event stream are kept apart, so the UI never presents "events in the
window" as "the events that caused this":

* ``triggering``: the events the stats engine recorded as triggering the finding (``Finding.event_ids``), attached
  to the finding's own evidence (``e*``).
* ``in_window``: the events a metric is computed from, found with a deterministic filter (event types for the
  metric, team or player, and the number's time window). The filter is stored next to the ids. Context (``c*``),
  player (``p*``) and recap (``claim``) numbers only have this relation, and so do findings without event ids.

The numbers in each persona text are also located, so the UI can link every verified figure to its evidence.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from typing import Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..agents.contracts import Claim, ClaimCheck, Ledger, LedgerItem, PlayerFact
from ..schema import Event
from ..stats.findings import Finding

MAX_EVENT_IDS = 200
PLAY = ("pass", "shot", "tackle", "pressure", "possession_change")
SHOT = ("shot",)
PASS = ("pass",)
POSSESSION = ("possession_change",)

# Which events each ledger metric is computed from. A test checks every metric in the sample bundles is listed.
METRIC_EVENTS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("shot_xg", "shot_distance", "xg", "match_xg", "xg_total", "shots", "match_shots",
                     "shots_total", "ended_with_shot", "finish_distance_to_goal", "seconds_to_finish",
                     "player_goals", "player_shots", "player_xg", "goal_margin", "shot_outcome",
                     "recap_score"), SHOT),
    **dict.fromkeys(("pass_completion", "pass_completion_total", "opponent_pass_completion", "field_tilt",
                     "opponent_final_third_passes", "passes_in_move", "metres_gained"), PASS),
    **dict.fromkeys(("possession", "possession_total", "passes_in_spell", "mean_spell", "seconds_since_turnover",
                     "possession_won_in_open_play", "regain_distance_from_own_goal", "turnovers_per_min"),
                    POSSESSION),
    "pressures_per_min": ("pressure",),
    "tackles_won": ("tackle",),
    "chaos_index": ("pass", "possession_change"),
    "momentum": ("pass", "shot", "pressure"),
    "spell_duration": PLAY,
}
INVOLVEMENT_EVENTS: dict[str, tuple[str, ...]] = {
    "scorer": SHOT, "shooter": SHOT, "top_xg": SHOT, "ball_winner": ("tackle", "possession_change"),
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Relation = Literal["triggering", "in_window", "player"]


class EventFilter(_Frozen):
    types: list[str]
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    from_s: float = Field(description="Match clock, inclusive.")
    to_s: float = Field(description="Match clock, inclusive.")


class EventLink(_Frozen):
    relation: Relation
    event_ids: list[int] = Field(description=f"At most {MAX_EVENT_IDS}, in event order.")
    count: int = Field(description="All matching events, even if event_ids was capped.")
    filter: Optional[EventFilter] = Field(None, description="How in_window and player events were selected.")


class EvidenceLink(_Frozen):
    ref: str
    kind: Literal["number", "player"]
    metric: Optional[str] = None
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    name: Optional[str] = Field(None, description="Player name, for player refs.")
    involvement: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    say: Optional[str] = None
    window_start_s: Optional[float] = Field(None, description="Match clock.")
    window_end_s: Optional[float] = Field(None, description="Match clock.")
    baseline: Optional[float] = None
    source: Optional[str] = Field(None, description="finding, context, player or claim (a recap's earlier claim).")
    origin: Optional[str] = Field(None, description="Job whose verified claim this number came from (recaps).")
    events: list[EventLink] = Field(default_factory=list)
    known: bool = Field(True, description="False if the ref is not in the ledger (should never happen for shown "
                                          "claims: the checker rejects them).")


class ClaimLink(_Frozen):
    claim_id: str
    text: str = Field(description="As checked (English).")
    team_id: Optional[str] = None
    deterministic: Literal["passed"] = Field("passed", description="Shown claims always passed the checker.")
    semantic: Literal["passed", "skipped", "not_run"]
    evidence: list[EvidenceLink]


class NumberSpan(_Frozen):
    start: int
    end: int
    refs: list[str] = Field(description="Ledger ids of the claim numbers with this value.")


class EventIndex:
    """Events sorted by clock, for window queries."""

    def __init__(self, events: Sequence[Event]) -> None:
        self.events = sorted(events, key=lambda e: (e.clock_s, e.event_id))
        self.clocks = [e.clock_s for e in self.events]
        self.by_id = {e.event_id: e for e in self.events}

    def query(self, flt: EventFilter) -> list[int]:
        lo, hi = bisect_left(self.clocks, flt.from_s), bisect_right(self.clocks, flt.to_s)
        return [e.event_id for e in self.events[lo:hi]
                if e.type in flt.types
                and (flt.team_id is None or getattr(e, "team_id", None) == flt.team_id)
                and (flt.player_id is None or getattr(e, "player_id", None) == flt.player_id)]

    def link(self, flt: EventFilter, relation: Relation) -> EventLink:
        ids = self.query(flt)
        return EventLink(relation=relation, event_ids=ids[:MAX_EVENT_IDS], count=len(ids), filter=flt)


def _number_link(item: LedgerItem, finding: Optional[Finding], index: EventIndex) -> EvidenceLink:
    links: list[EventLink] = []
    if item.source == "finding" and finding is not None and finding.event_ids:
        links.append(EventLink(relation="triggering", event_ids=list(finding.event_ids), count=len(finding.event_ids)))
    point = item.window_end_s <= item.window_start_s
    if not (links and point):
        types = METRIC_EVENTS.get(item.metric, PLAY)
        flt = EventFilter(types=list(types), team_id=item.team_id, player_id=item.player_id,
                          from_s=item.window_start_s, to_s=item.window_end_s)
        links.append(index.link(flt, "in_window"))
    return EvidenceLink(ref=item.ref, kind="number", metric=item.metric, team_id=item.team_id,
                        player_id=item.player_id, value=item.value, unit=item.unit, say=item.say,
                        window_start_s=item.window_start_s, window_end_s=item.window_end_s, baseline=item.baseline,
                        source=item.source, origin=item.origin, events=links)


def _player_link(fact: PlayerFact, clock_s: float, index: EventIndex) -> EvidenceLink:
    types = INVOLVEMENT_EVENTS.get(fact.involvement, PLAY)
    at = fact.clock_s
    flt = EventFilter(types=list(types), player_id=fact.player_id, from_s=at if at is not None else 0.0,
                      to_s=at if at is not None else clock_s)
    return EvidenceLink(ref=fact.ref, kind="player", team_id=fact.team_id, player_id=fact.player_id, name=fact.name,
                        involvement=fact.involvement, source="player", window_start_s=flt.from_s,
                        window_end_s=flt.to_s, events=[index.link(flt, "player")])


def evidence_link(ref: str, ledger: Ledger, finding: Optional[Finding], clock_s: float,
                  index: EventIndex) -> EvidenceLink:
    item = ledger.item(ref)
    if item is not None:
        return _number_link(item, finding, index)
    fact = ledger.player(ref)
    if fact is not None:
        return _player_link(fact, clock_s, index)
    return EvidenceLink(ref=ref, kind="number", known=False)


def semantic_status(claim_id: str, checks: Sequence[ClaimCheck], semantic_check: str) -> str:
    if semantic_check.startswith("skipped"):
        return "skipped"
    if any(c.claim_id == claim_id and c.layer == "semantic" and c.ok for c in checks):
        return "passed"
    return "not_run"


def claim_links(claims: Sequence[Claim], checks: Sequence[ClaimCheck], semantic_check: str, ledger: Ledger,
                finding: Optional[Finding], clock_s: float, index: EventIndex) -> list[ClaimLink]:
    return [ClaimLink(claim_id=c.claim_id, text=c.text, team_id=c.team_id,
                      semantic=semantic_status(c.claim_id, checks, semantic_check),
                      evidence=[evidence_link(ref, ledger, finding, clock_s, index)
                                for ref in dict.fromkeys([*c.refs, *(n.ref for n in c.numbers)])])
            for c in claims]


_SCORE = re.compile(r"\d+\s*[-–]\s*\d+")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w'’])")


def number_spans(text: str, claims: Sequence[Claim]) -> list[NumberSpan]:
    """Every figure in a shown text that equals a cited claim number (scores and minutes are left alone).

    Numbers are written as digits in every language (decision 14), with ``.`` or ``,`` as the decimal mark."""
    values: dict[float, list[str]] = {}
    for claim in claims:
        for number in claim.numbers:
            refs = values.setdefault(round(number.value, 6), [])
            if number.ref not in refs:
                refs.append(number.ref)
    scores = [m.span() for m in _SCORE.finditer(text)]
    spans: list[NumberSpan] = []
    for match in _NUMBER.finditer(text):
        if any(a <= match.start() < b for a, b in scores):
            continue
        refs = values.get(round(float(match.group().replace(",", ".")), 6))
        if refs:
            spans.append(NumberSpan(start=match.start(), end=match.end(), refs=list(refs)))
    return spans
