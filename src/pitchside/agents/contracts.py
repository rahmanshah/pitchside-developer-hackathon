"""Contracts shared by the agents, the workflow, the run bundle and the evaluation.

Three groups:

* **Inputs**: ``Job`` (one finding or recap to narrate) with its ``Ledger``: every number a model may use, each
  with an id (``e*`` finding evidence, ``c*`` context and player numbers, ``p*`` player facts).
* **Model replies** (``*Reply``): the JSON each agent must return. They have no defaults and forbid extra keys,
  so they also work as strict structured-output schemas.
* **Outputs**: ``Claim``, ``ClaimCheck``, ``PersonaText``, ``Narrative``, ``AuditEntry`` and ``JobRecord``.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Score
from ..stats.engine import Snapshot
from ..stats.findings import Finding

ANALYST = "analyst"
CASUAL = "casual"
FAV_PREFIX = "fav:"


def fav_persona(player_id: str) -> str:
    return f"{FAV_PREFIX}{player_id}"


Source = Literal["model", "template"]
JobKind = Literal["finding", "recap"]
RecapKind = Literal["half_time", "full_time"]
Layer = Literal["deterministic", "semantic"]
ReasonCode = Literal[
    "empty_text",
    "no_evidence",
    "unknown_evidence",
    "number_not_cited",
    "number_mismatch",
    "unlisted_number",
    "team_mismatch",
    "window_mismatch",
    "score_mismatch",
    "unreferenced_player",
    "unsupported",  # semantic layer
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class LedgerItem(_Frozen):
    """One number a model may cite."""

    ref: str = Field(description="Id the claims cite: e* finding evidence, c* context or player numbers.")
    metric: str
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    value: float
    unit: str
    window_start_s: float
    window_end_s: float
    baseline: Optional[float] = None
    say: str = Field(description='How to quote it in English, e.g. "71% field tilt".')
    source: Literal["finding", "context", "player", "claim"]
    origin: Optional[str] = Field(None, description="Job whose verified claim this number came from (recaps).")


class PlayerFact(_Frozen):
    """A player involved in the moment, or whose numbers a recap uses."""

    ref: str
    player_id: str
    name: str
    team_id: str
    involvement: str = Field(description='"scorer", "shooter", "ball_winner", "featured", "top_xg" ...')
    clock_s: Optional[float] = None


class Ledger(_Mutable):
    items: list[LedgerItem] = Field(default_factory=list)
    players: list[PlayerFact] = Field(default_factory=list)

    def item(self, ref: str) -> Optional[LedgerItem]:
        return next((i for i in self.items if i.ref == ref), None)

    def player(self, ref: str) -> Optional[PlayerFact]:
        return next((p for p in self.players if p.ref == ref), None)

    def has(self, ref: str) -> bool:
        return self.item(ref) is not None or self.player(ref) is not None

    def next_ref(self, prefix: str) -> str:
        used = [r.ref for r in self.items if r.ref.startswith(prefix)] + [
            p.ref for p in self.players if p.ref.startswith(prefix)]
        return f"{prefix}{len(used) + 1}"

    def add_item(self, prefix: str, **fields: Any) -> LedgerItem:
        item = LedgerItem(ref=self.next_ref(prefix), **fields)
        self.items.append(item)
        return item

    def add_player(self, **fields: Any) -> PlayerFact:
        existing = next((p for p in self.players if p.player_id == fields["player_id"]), None)
        if existing is not None:
            return existing
        fact = PlayerFact(ref=self.next_ref("p"), **fields)
        self.players.append(fact)
        return fact


class TeamRef(_Frozen):
    team_id: str
    name: str
    short_name: str
    side: Literal["home", "away"]


class PlayerRef(_Frozen):
    player_id: str
    name: str
    team_id: str
    position: str


class MatchFacts(_Frozen):
    """Who is playing: clubs and squads (all invented)."""

    match_id: str
    home: TeamRef
    away: TeamRef
    roster: list[PlayerRef]

    @property
    def teams(self) -> tuple[TeamRef, TeamRef]:
        return (self.home, self.away)

    def team(self, team_id: Optional[str]) -> Optional[TeamRef]:
        return next((t for t in self.teams if t.team_id == team_id), None)

    def opponent(self, team_id: str) -> TeamRef:
        return self.away if team_id == self.home.team_id else self.home

    def player(self, player_id: str) -> Optional[PlayerRef]:
        return next((p for p in self.roster if p.player_id == player_id), None)


class GoalAt(_Frozen):
    clock_s: float
    team_id: str
    player_id: str
    score: Score = Field(description="Score after the goal.")


class Moment(_Frozen):
    """An earlier finding, as context for the interpreter."""

    finding_id: str
    kind: str
    status: str
    team_id: Optional[str]
    minute: int


class PlayerStats(_Frozen):
    player_id: str
    team_id: str
    goals: int = 0
    shots: int = 0
    xg: float = 0.0
    passes: int = 0
    passes_completed: int = 0
    tackles_won: int = 0
    pressures: int = 0


class EarlierClaim(_Frozen):
    """A claim already verified and shown earlier in the match; recaps are built from these."""

    job_id: str
    minute: int
    label: str
    text: str
    refs: list[str] = Field(description="Its numbers' ids in the recap's own ledger.")


class Job(_Frozen):
    """One thing to narrate: a model-eligible finding, or a half-time / full-time recap."""

    job_id: str
    kind: JobKind
    label: str = Field(description="Finding kind (e.g. 'goal') or recap kind ('half_time', 'full_time').")
    match_id: str
    finding: Optional[Finding] = None
    team_id: Optional[str] = None
    clock_s: float
    window_start_s: float
    window_end_s: float
    score: Score
    goals: list[GoalAt] = Field(default_factory=list, description="Goals so far, for score checks and recaps.")
    recent: list[Moment] = Field(default_factory=list)
    ledger: Ledger
    snapshot: Optional[Snapshot] = Field(None, description="Stats at detection time; the tools read it.")
    player_stats: list[PlayerStats] = Field(default_factory=list)
    featured: list[str] = Field(default_factory=list, description="Favourite-player persona player ids.")
    earlier: list[EarlierClaim] = Field(default_factory=list, description="Recaps: verified claims so far.")
    facts: MatchFacts

    @property
    def minute(self) -> int:
        return int(self.clock_s // 60) + 1


# --------------------------------------------------------------------------- #
# Model replies (strict JSON shapes: no defaults, no extra keys)
# --------------------------------------------------------------------------- #


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterpreterReply(_Wire):
    angle: str = Field(description="One sentence: what this moment means for the match.")
    focus_refs: list[str] = Field(description="The 2-5 ledger ids that best support the angle, most important first.")
    note: str = Field(description="Short context for the explainer (earlier moments, score situation).")


class ClaimNumber(_Wire):
    value: float = Field(description="The number exactly as written in the claim text.")
    ref: str = Field(description="Ledger id of the number.")


class ClaimOut(_Wire):
    text: str
    team_id: Optional[str]
    refs: list[str]
    numbers: list[ClaimNumber]


class ExplainerReply(_Wire):
    headline: str
    claims: list[ClaimOut]


class VerdictOut(_Wire):
    claim: int = Field(description="1-based index of the claim.")
    supported: bool
    reason: str


class VerifierReply(_Wire):
    verdicts: list[VerdictOut]


class PersonaTextOut(_Wire):
    persona: str
    language: str
    text: str


class PersonalizerReply(_Wire):
    texts: list[PersonaTextOut]


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class Claim(_Frozen):
    claim_id: str
    text: str
    team_id: Optional[str] = None
    refs: list[str] = Field(default_factory=list)
    numbers: list[ClaimNumber] = Field(default_factory=list)


class Reason(_Frozen):
    code: ReasonCode
    detail: str


class ClaimCheck(_Frozen):
    claim_id: str
    attempt: int
    layer: Layer
    ok: bool
    reasons: list[Reason] = Field(default_factory=list)


class Draft(_Frozen):
    attempt: int
    headline: str
    claims: list[Claim]


class PersonaText(_Frozen):
    persona: str = Field(description='"analyst", "casual" or "fav:<player_id>".')
    language: str
    text: str
    source: Source
    reason: Optional[str] = Field(None, description="Why a template was used, if it was.")


class Narrative(_Frozen):
    source: Source = Field(description="'model' if verified model claims are shown, 'template' otherwise.")
    reason: Optional[str] = Field(None, description="Why the template was used.")
    headline: str
    claims: list[Claim] = Field(description="Claims shown: every one passed the deterministic check.")
    semantic_check: str = Field(description="'passed', 'skipped: <reason>' or 'not_run' (templates).")
    texts: list[PersonaText]

    def text(self, persona: str, language: str = "en") -> Optional[str]:
        return next((t.text for t in self.texts if t.persona == persona and t.language == language), None)


class AuditEntry(_Frozen):
    """One step of one job: who did what, with which input and output, and why the next step followed."""

    job_id: str
    seq: int
    step: str = Field(description="interpret, explain, check, verify, personalize, fallback, template, publish ...")
    actor: str = Field(description="Agent name, 'checker', 'director' or 'templates'.")
    attempt: Optional[int] = None
    outcome: str
    detail: str
    prompt_version: Optional[str] = None
    input_digest: Optional[str] = None
    next_step: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)


class JobRecord(_Frozen):
    """Everything about one narrated job: inputs (ledger), every draft and check, and what was shown."""

    job_id: str
    kind: JobKind
    label: str
    finding_id: Optional[str]
    team_id: Optional[str]
    clock_s: float
    route: Literal["model", "template"] = Field(description="Whether the job was sent to the agents at all.")
    ledger: Ledger
    drafts: list[Draft]
    checks: list[ClaimCheck]
    narrative: Narrative
    audit: list[AuditEntry]
