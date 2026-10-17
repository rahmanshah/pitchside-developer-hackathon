"""Event schema for synthetic football matches.

Every message that flows through the pipeline (generator -> stats engine ->
agents -> overlays) is one of the event models below. They are plain pydantic
models, so the same definitions give us validation, JSON (de)serialisation and
a published JSON Schema (``pitchside schema``).

Coordinates are in metres on a 105 x 68 pitch. The home side always attacks
towards x = 105 and the away side towards x = 0. The clock is total match
seconds since kick-off (second half starts at 2700 s).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SCHEMA_VERSION = "1.0"
PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0
GOAL_WIDTH_M = 7.32
HALF_LENGTH_S = 45 * 60


class Side(str, Enum):
    HOME = "home"
    AWAY = "away"

    @property
    def opponent(self) -> "Side":
        return Side.AWAY if self is Side.HOME else Side.HOME

    @property
    def attack_sign(self) -> int:
        """+1 if this side attacks towards x = 105, -1 otherwise."""
        return 1 if self is Side.HOME else -1


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Point(_Model):
    x: float = Field(ge=0, le=PITCH_LENGTH_M)
    y: float = Field(ge=0, le=PITCH_WIDTH_M)


class Score(_Model):
    home: int = Field(ge=0)
    away: int = Field(ge=0)


Position = Literal["GK", "LB", "CB", "RB", "CM", "LW", "ST", "RW"]


class PlayerInfo(_Model):
    player_id: str
    name: str
    shirt_number: int = Field(ge=1, le=99)
    position: Position
    # Skill attributes in [0, 1]; they drive the simulation, not the stats.
    pace: float = Field(ge=0, le=1)
    passing: float = Field(ge=0, le=1)
    shooting: float = Field(ge=0, le=1)
    tackling: float = Field(ge=0, le=1)


class TeamInfo(_Model):
    team_id: str
    name: str
    short_name: str
    side: Side
    players: list[PlayerInfo]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class _EventBase(_Model):
    event_id: int = Field(ge=0, description="Monotonic per match, starts at 0.")
    match_id: str
    period: int = Field(ge=1, le=2)
    clock_s: float = Field(ge=0, description="Match seconds since kick-off.")


class MatchStartEvent(_EventBase):
    type: Literal["match_start"] = "match_start"
    schema_version: str = SCHEMA_VERSION
    home: TeamInfo
    away: TeamInfo
    venue: str
    competition: str
    weather: str
    kickoff_utc: datetime


class PeriodStartEvent(_EventBase):
    type: Literal["period_start"] = "period_start"


class PeriodEndEvent(_EventBase):
    type: Literal["period_end"] = "period_end"
    score: Score


class MatchEndEvent(_EventBase):
    type: Literal["match_end"] = "match_end"
    final_score: Score


class PassEvent(_EventBase):
    type: Literal["pass"] = "pass"
    team_id: str
    player_id: str
    receiver_id: str = Field(description="Intended receiver (also set if incomplete).")
    start: Point
    end: Point
    distance_m: float = Field(ge=0)
    ball_speed_kmh: float = Field(ge=0)
    outcome: Literal["complete", "incomplete"]
    pressure: float = Field(ge=0, le=1, description="Defensive pressure on the passer.")
    difficulty: float = Field(ge=0, le=1, description="Pass difficulty rating.")


class ShotEvent(_EventBase):
    type: Literal["shot"] = "shot"
    team_id: str
    player_id: str
    location: Point
    distance_to_goal_m: float = Field(ge=0)
    xg: float = Field(ge=0, le=1, description="Expected-goals value of the chance.")
    shot_speed_kmh: float = Field(ge=0)
    outcome: Literal["goal", "saved", "blocked", "off_target"]
    score_after: Optional[Score] = Field(default=None, description="Only set on goals.")


class TackleEvent(_EventBase):
    type: Literal["tackle"] = "tackle"
    team_id: str = Field(description="Team of the player making the tackle.")
    player_id: str = Field(description="The tackler.")
    tackled_player_id: str
    location: Point
    outcome: Literal["won", "lost"]


PossessionReason = Literal[
    "tackle",
    "interception",
    "out_of_play",
    "shot_saved",
    "shot_blocked",
    "goal_kick",
    "kickoff",
]


class PossessionChangeEvent(_EventBase):
    type: Literal["possession_change"] = "possession_change"
    team_id: str = Field(description="Team that gains possession.")
    from_team_id: str
    player_id: Optional[str] = Field(default=None, description="Player gaining the ball.")
    location: Point
    reason: PossessionReason
    previous_spell_s: float = Field(ge=0, description="Length of the spell just ended.")
    previous_spell_passes: int = Field(ge=0, description="Completed passes in that spell.")


class PressureEvent(_EventBase):
    type: Literal["pressure"] = "pressure"
    team_id: str = Field(description="Pressing team.")
    player_id: str = Field(description="The presser.")
    target_player_id: str
    location: Point
    intensity: float = Field(ge=0, le=1)


Event = Annotated[
    Union[
        MatchStartEvent,
        PeriodStartEvent,
        PeriodEndEvent,
        MatchEndEvent,
        PassEvent,
        ShotEvent,
        TackleEvent,
        PossessionChangeEvent,
        PressureEvent,
    ],
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def to_json_line(event: _EventBase) -> str:
    """Serialise one event as a compact JSON line (None fields omitted)."""
    return event.model_dump_json(exclude_none=True)


def parse_event(line: str) -> Event:
    """Parse and validate one JSON line into the matching event model."""
    return EVENT_ADAPTER.validate_json(line)


def json_schema() -> dict:
    """The JSON Schema of the full event union, for docs and downstream tools."""
    return EVENT_ADAPTER.json_schema()
