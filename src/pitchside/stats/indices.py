"""Composite indices built from windowed numbers.

* **Momentum** (-1 to +1, positive = home on top): a weighted blend of who
  owns the attacking third, who shoots, who creates xG and who presses. Each
  part is a smoothed share, so a quiet window drifts to 0 instead of jumping
  to an extreme on one shot.
* **Chaos** (-1 to +1, positive = chaotic, negative = controlled): how far
  turnovers, pass completion and possession-spell length are from typical
  settled play.

The reference values below are typical levels in the ``balanced`` control
storyline (tuning seeds 1-6), not real-world league figures.
"""

from __future__ import annotations

import math

from .metrics import MatchNumbers, TeamNumbers

# Sums use math.fsum: built-in sum() over floats changed in Python 3.12, and a replay must give the same rounded
# numbers on every supported Python version (requires-python >= 3.11).

MOMENTUM_WEIGHTS = {"field_tilt": 0.35, "shots": 0.25, "xg": 0.20, "pressing": 0.20}

TYPICAL_TURNOVERS_PER_MIN = 1.8
TYPICAL_COMPLETION_PCT = 84.0
TYPICAL_SPELL_S = 32.0
# Distance from typical that counts as fully chaotic (or fully controlled).
TURNOVER_SCALE = 1.6
COMPLETION_SCALE = 15.0
SPELL_SCALE = 16.0


def _signed_share(mine: float, theirs: float, prior: float) -> float:
    """Smoothed share of `mine` mapped to [-1, 1]."""
    return 2 * (mine + prior) / (mine + theirs + 2 * prior) - 1


def _clip(value: float) -> float:
    return max(-1.0, min(1.0, value))


def momentum_parts(home: TeamNumbers, away: TeamNumbers) -> dict[str, float]:
    """Each momentum component in [-1, 1], positive = home."""
    return {
        "field_tilt": _signed_share(home.final_third_passes, away.final_third_passes, 2.0),
        "shots": _signed_share(home.shots, away.shots, 1.0),
        "xg": _signed_share(home.xg, away.xg, 0.1),
        "pressing": _signed_share(home.pressures_per_min, away.pressures_per_min, 0.5),
    }


def momentum(home: TeamNumbers, away: TeamNumbers) -> float:
    parts = momentum_parts(home, away)
    return round(math.fsum(MOMENTUM_WEIGHTS[k] * v for k, v in parts.items()), 3)


def chaos_parts(match: MatchNumbers) -> dict[str, float]:
    """Each chaos component in [-1, 1], positive = chaotic. Missing data counts as typical."""
    completion = match.pass_completion_pct
    spell = match.mean_spell_s
    return {
        "turnovers": _clip((match.turnovers_per_min - TYPICAL_TURNOVERS_PER_MIN) / TURNOVER_SCALE),
        "completion": _clip((TYPICAL_COMPLETION_PCT - completion) / COMPLETION_SCALE) if completion is not None else 0.0,
        "spell_length": _clip((TYPICAL_SPELL_S - spell) / SPELL_SCALE) if spell is not None else 0.0,
    }


def chaos(match: MatchNumbers) -> float:
    parts = chaos_parts(match)
    return round(math.fsum(parts.values()) / len(parts), 3)
