"""Typed findings emitted by the stats engine.

A finding is a claim about the match ("home are laying siege") together with
the numbers that support it. Downstream agents may only narrate what is in a
finding's ``evidence``; the verifier rejects anything else. That is why a
finding without evidence is invalid.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..schema import Score


class FindingKind(str, Enum):
    GOAL = "goal"
    BIG_CHANCE = "big_chance"
    MOMENTUM_SHIFT = "momentum_shift"
    PRESSURE_SURGE = "pressure_surge"
    SIEGE = "siege"
    CHAOS_SPELL = "chaos_spell"
    COUNTER_ATTACK = "counter_attack"


# Kinds that describe a spell of play (they have a start and an end) rather
# than a single moment.
SPELL_KINDS = frozenset(
    {FindingKind.MOMENTUM_SHIFT, FindingKind.PRESSURE_SURGE, FindingKind.SIEGE, FindingKind.CHAOS_SPELL}
)

Unit = Literal["pct", "per_min", "xg", "index", "s", "count", "m"]
FindingStatus = Literal["moment", "start", "end"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Evidence(_Model):
    """One computed number backing a finding."""

    metric: str = Field(description='Metric name, e.g. "field_tilt" or "xg".')
    team_id: Optional[str] = Field(default=None, description="Team the number is about; None for match-wide.")
    value: float
    unit: Unit
    window_start_s: float = Field(ge=0)
    window_end_s: float = Field(ge=0)
    baseline: Optional[float] = Field(
        default=None, description="Reference value the number is compared against, if any."
    )


class Finding(_Model):
    finding_id: str = Field(description="Deterministic: <match_id>:<kind>:<sequence>.")
    match_id: str
    kind: FindingKind
    status: FindingStatus = Field(description="'moment' for point events, 'start'/'end' for spells.")
    team_id: Optional[str] = Field(default=None, description="Team the finding is about; None for both.")
    start_s: float = Field(ge=0, description="Start of the time window the claim covers.")
    end_s: float = Field(ge=0, description="End of the time window the claim covers.")
    detected_at_s: float = Field(ge=0, description="Match clock when the engine emitted the finding.")
    event_ids: list[int] = Field(default_factory=list, description="Events that triggered the finding.")
    severity: float = Field(ge=0, le=1, description="How notable the moment is, 0 to 1.")
    score: Score = Field(description="Score at detection time.")
    evidence: list[Evidence] = Field(min_length=1)


FINDINGS_ADAPTER: TypeAdapter[list[Finding]] = TypeAdapter(list[Finding])


def findings_json_schema() -> dict:
    """JSON Schema of a list of findings, for docs and downstream tools."""
    return FINDINGS_ADAPTER.json_schema()
