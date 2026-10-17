"""Fictional clubs and squads.

All clubs and players are invented. Any resemblance to real people or clubs is
coincidental; no real match data or Premier League IP is used anywhere.
Squads are generated from a fixed per-club seed, so a club always has the same
players regardless of the match seed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .schema import PlayerInfo, Position, Side, TeamInfo


@dataclass(frozen=True)
class Slot:
    """One position in the formation. Coordinates are fractions of the pitch,
    measured from the team's own goal (x) and across the width (y)."""

    position: Position
    shirt: int
    x: float
    y: float


# 4-3-3. Order matters: players are created in this order everywhere.
FORMATION_4_3_3: tuple[Slot, ...] = (
    Slot("GK", 1, 0.04, 0.50),
    Slot("LB", 3, 0.26, 0.10),
    Slot("CB", 4, 0.22, 0.37),
    Slot("CB", 5, 0.22, 0.63),
    Slot("RB", 2, 0.26, 0.90),
    Slot("CM", 6, 0.46, 0.28),
    Slot("CM", 8, 0.42, 0.50),
    Slot("CM", 10, 0.46, 0.72),
    Slot("LW", 11, 0.72, 0.14),
    Slot("ST", 9, 0.80, 0.50),
    Slot("RW", 7, 0.72, 0.86),
)

# Typical attribute profile per position: (pace, passing, shooting, tackling).
_PROFILE: dict[str, tuple[float, float, float, float]] = {
    "GK": (0.45, 0.60, 0.20, 0.30),
    "LB": (0.74, 0.66, 0.40, 0.76),
    "RB": (0.74, 0.66, 0.40, 0.76),
    "CB": (0.60, 0.62, 0.35, 0.84),
    "CM": (0.66, 0.80, 0.62, 0.66),
    "LW": (0.84, 0.70, 0.74, 0.40),
    "RW": (0.84, 0.70, 0.74, 0.40),
    "ST": (0.76, 0.62, 0.86, 0.34),
}

_FIRST_NAMES = (
    "Alden", "Bram", "Caio", "Dario", "Elio", "Finn", "Gael", "Hugo", "Ilmari",
    "Jonas", "Kofi", "Leif", "Mateo", "Niko", "Otso", "Pavel", "Quinn", "Rafa",
    "Soren", "Tomas", "Ugo", "Viktor", "Wen", "Xavi", "Yusuf", "Zane", "Arman",
    "Benji", "Cem", "Dani", "Emil", "Farid", "Gio", "Hanno", "Idris", "Joaquin",
)
_SURNAMES = (
    "Aaltio", "Brandt", "Carvalho", "Dumont", "Eklund", "Fonseca", "Grau",
    "Halvorsen", "Ibarra", "Jovanic", "Kallio", "Lindqvist", "Moreau", "Nakamura",
    "Okafor", "Petrovic", "Quintero", "Rinne", "Salvi", "Tanaka", "Uusitalo",
    "Vidal", "Werner", "Yilmaz", "Zielinski", "Abara", "Bellamy", "Costa",
    "Devereux", "Esposito", "Falk", "Garrido", "Hasegawa", "Ivanov", "Kovacs",
)


@dataclass(frozen=True)
class Club:
    team_id: str
    name: str
    short_name: str
    venue: str
    # Shifts every attribute of the squad; 0.0 is an average side.
    quality: float = 0.0


CLUBS: dict[str, Club] = {
    "aur": Club("aur", "Aurora Rovers", "AUR", "Aurora Park", 0.02),
    "irv": Club("irv", "Ironvale Athletic", "IRV", "Ironworks Ground", 0.0),
    "har": Club("har", "Harbourton City", "HAR", "Quayside Stadium", 0.03),
    "wst": Club("wst", "Westmere Town", "WST", "Mere Lane", -0.02),
}

DEFAULT_HOME = "aur"
DEFAULT_AWAY = "irv"


def _clip(value: float, low: float = 0.30, high: float = 0.97) -> float:
    return max(low, min(high, value))


def build_team(club_key: str, side: Side) -> TeamInfo:
    """Create the (deterministic) squad of a club for the given side."""
    club = CLUBS[club_key]
    rng = random.Random(f"squad:{club.team_id}")
    first = rng.sample(_FIRST_NAMES, len(FORMATION_4_3_3))
    last = rng.sample(_SURNAMES, len(FORMATION_4_3_3))

    players: list[PlayerInfo] = []
    for slot, given, family in zip(FORMATION_4_3_3, first, last):
        pace, passing, shooting, tackling = (
            _clip(base + club.quality + rng.gauss(0, 0.05)) for base in _PROFILE[slot.position]
        )
        players.append(
            PlayerInfo(
                player_id=f"{club.team_id}_{slot.shirt:02d}",
                name=f"{given} {family}",
                shirt_number=slot.shirt,
                position=slot.position,
                pace=round(pace, 3),
                passing=round(passing, 3),
                shooting=round(shooting, 3),
                tackling=round(tackling, 3),
            )
        )
    return TeamInfo(
        team_id=club.team_id,
        name=club.name,
        short_name=club.short_name,
        side=side,
        players=players,
    )
