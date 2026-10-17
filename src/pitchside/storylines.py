"""Scripted match storylines.

A storyline plants known patterns into a simulated match: tactical *phases*
(a pressing surge, a low block, a chaotic spell) and *scripted goals*. Because
we know what was planted, the same definition doubles as an answer key: later
we can score whether the interpreter and explainer agents actually found the
momentum shift they were supposed to find.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .schema import Side

PhaseTeam = Literal["home", "away", "both"]


@dataclass(frozen=True)
class Tactics:
    """Effective tactical dials for one team at one moment."""

    attack_intent: float = 0.50  # 0 = keep the ball safe, 1 = go forward at all costs
    press: float = 0.35  # 0 = sit off, 1 = press everywhere
    tempo: float = 0.0  # additive change to the speed of play
    pass_accuracy: float = 0.0  # additive change to completion probability
    shot_propensity: float = 1.0  # multiplier on shot frequency


@dataclass(frozen=True)
class Phase:
    """A time window in which a team's tactics are shifted."""

    label: str  # machine-readable key
    kind: str  # ground-truth category an interpreter should recognise
    description: str
    start_min: float
    end_min: float
    team: PhaseTeam
    attack_intent: float = 0.0
    press: float = 0.0
    tempo: float = 0.0
    pass_accuracy: float = 0.0
    shot_propensity: float = 0.0  # added to the base multiplier of 1.0

    def applies_to(self, side: Side) -> bool:
        return self.team == "both" or self.team == side.value

    def active_at(self, minute: float) -> bool:
        return self.start_min <= minute < self.end_min


@dataclass(frozen=True)
class ScriptedGoal:
    """The first good chance this side creates after `minute` is a goal."""

    side: Side
    minute: float
    description: str


@dataclass(frozen=True)
class Storyline:
    name: str
    description: str
    phases: tuple[Phase, ...] = field(default_factory=tuple)
    goals: tuple[ScriptedGoal, ...] = field(default_factory=tuple)
    # When True the scripted goals are the *only* goals; every other shot is
    # saved, blocked or off target. This keeps the final score true to the story.
    scripted_scoring: bool = False


def tactics_at(storyline: Storyline, side: Side, minute: float) -> Tactics:
    """Base tactics plus every phase that is active for `side` at `minute`."""
    tactics = Tactics()
    for phase in storyline.phases:
        if phase.applies_to(side) and phase.active_at(minute):
            tactics = replace(
                tactics,
                attack_intent=tactics.attack_intent + phase.attack_intent,
                press=tactics.press + phase.press,
                tempo=tactics.tempo + phase.tempo,
                pass_accuracy=tactics.pass_accuracy + phase.pass_accuracy,
                shot_propensity=tactics.shot_propensity + phase.shot_propensity,
            )
    return replace(
        tactics,
        attack_intent=min(max(tactics.attack_intent, 0.05), 0.98),
        press=min(max(tactics.press, 0.05), 0.95),
        shot_propensity=max(tactics.shot_propensity, 0.2),
    )


BALANCED = Storyline(
    name="balanced",
    description="An even contest with no planted narrative. Useful as a control.",
)

COMEBACK = Storyline(
    name="comeback",
    description=(
        "Away side scores first and then sits deep. Home turn up the pressure after "
        "the hour, turn it around with two late goals, and the finish gets frantic."
    ),
    phases=(
        Phase(
            label="away_low_block",
            kind="low_block",
            description="Away side protect their lead by dropping deep and ceding the ball.",
            start_min=40, end_min=90, team="away",
            attack_intent=-0.15, press=-0.18, tempo=-0.05,
        ),
        Phase(
            label="home_press_surge",
            kind="momentum_shift",
            description="Home raise their line and press high; territory and shots swing their way.",
            start_min=55, end_min=90, team="home",
            attack_intent=0.28, press=0.30, tempo=0.15, shot_propensity=0.5,
        ),
        Phase(
            label="frantic_finish",
            kind="chaos",
            description="Both sides trade blows in a stretched, error-prone finish.",
            start_min=78, end_min=90, team="both",
            tempo=0.25, pass_accuracy=-0.10,
        ),
    ),
    goals=(
        ScriptedGoal(Side.AWAY, 23, "Away take an early lead against the run of play."),
        ScriptedGoal(Side.HOME, 71, "Home equalise after sustained pressure."),
        ScriptedGoal(Side.HOME, 86, "Home complete the comeback."),
    ),
    scripted_scoring=True,
)

SIEGE = Storyline(
    name="siege",
    description=(
        "Home besiege a low block, finally break through, then get hit by a counter-attack "
        "against the run of play before settling it late."
    ),
    phases=(
        Phase(
            label="home_siege",
            kind="siege",
            description="Home camp in the away half; away defend in a compact low block.",
            start_min=18, end_min=38, team="home",
            attack_intent=0.35, press=0.20, tempo=0.10, shot_propensity=0.5,
        ),
        Phase(
            label="away_low_block",
            kind="low_block",
            description="Away drop off and defend their box.",
            start_min=18, end_min=38, team="away",
            attack_intent=-0.20, press=-0.20,
        ),
        Phase(
            label="away_counter_burst",
            kind="counter_attack",
            description="Away break quickly after winning the ball, at pace and with directness.",
            start_min=48, end_min=54, team="away",
            attack_intent=0.40, tempo=0.35, shot_propensity=0.5,
        ),
    ),
    goals=(
        ScriptedGoal(Side.HOME, 36, "Home finally break the low block."),
        ScriptedGoal(Side.AWAY, 52, "Away score on the counter, against the run of play."),
        ScriptedGoal(Side.HOME, 79, "Home restore the lead."),
    ),
    scripted_scoring=True,
)

END_TO_END = Storyline(
    name="end_to_end",
    description="An open, high-tempo game where control gives way to chaos and goals trade.",
    phases=(
        Phase(
            label="open_game",
            kind="chaos",
            description="Both teams push high; passing accuracy drops and turnovers pile up.",
            start_min=30, end_min=60, team="both",
            attack_intent=0.20, press=0.15, tempo=0.30, pass_accuracy=-0.12,
            shot_propensity=0.3,
        ),
    ),
    goals=(
        ScriptedGoal(Side.HOME, 34, "Home open the scoring in a stretched game."),
        ScriptedGoal(Side.AWAY, 41, "Away hit back before half-time."),
        ScriptedGoal(Side.HOME, 58, "Home retake the lead."),
        ScriptedGoal(Side.AWAY, 83, "Away snatch a late equaliser."),
    ),
    scripted_scoring=True,
)

STORYLINES: dict[str, Storyline] = {
    s.name: s for s in (BALANCED, COMEBACK, SIEGE, END_TO_END)
}
