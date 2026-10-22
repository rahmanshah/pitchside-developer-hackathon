"""Generated club colours and crest patterns: invented clubs get invented identities, never real ones.

Colours come from a hash of the team id, so a club looks the same in every run. Every colour pair the UI draws is
checked for contrast (WCAG 2.x relative luminance): text on the club colour at least 4.5:1, and the club's on-pitch
accent against the pitch at least 3:1 (non-text graphics). Hues near the UI's amber are avoided, because amber is
reserved for the evidence layer, and home and away are kept at least 60 degrees apart so the clubs never look alike.
"""

from __future__ import annotations

import colorsys
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

PITCH_BG = "#0f2a24"  # must match --pitch in web/css/tokens.css (a test checks it)
WHITE = "#ffffff"
PATTERNS = ("stripes", "hoops", "chevron", "sash", "quarters", "plain")
Pattern = Literal["stripes", "hoops", "chevron", "sash", "quarters", "plain"]
MIN_HUE_GAP = 60.0
AMBER_BAND = (20.0, 60.0)  # hues kept free for the evidence layer
TEXT_CONTRAST = 4.5
GRAPHIC_CONTRAST = 3.0


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClubColours(_Frozen):
    primary: str
    secondary: str
    text: str
    accent: str


class Crest(_Frozen):
    pattern: Pattern
    initials: str


SALT = "crest:45:"  # chosen so the four built-in clubs get four different hues and patterns


def _hash(team_id: str) -> int:
    return int(hashlib.sha256(f"{SALT}{team_id}".encode("utf-8")).hexdigest()[:12], 16)


def _hex(hue: float, lightness: float, saturation: float) -> str:
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360, lightness, saturation)
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


def luminance(colour: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hue_gap(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


HUES = (0.0, 75.0, 140.0, 190.0, 215.0, 255.0, 285.0, 325.0)  # spread round the wheel, none in the amber band


def base_hue(team_id: str) -> float:
    return HUES[_hash(team_id) % len(HUES)]


def colours_for(team_id: str, hue: float) -> ClubColours:
    lightness = 0.42
    primary = _hex(hue, lightness, 0.62)
    while contrast(WHITE, primary) < TEXT_CONTRAST:
        lightness -= 0.02
        primary = _hex(hue, lightness, 0.62)
    accent_l = 0.58
    accent = _hex(hue, accent_l, 0.78)
    while contrast(accent, PITCH_BG) < GRAPHIC_CONTRAST:
        accent_l += 0.02
        accent = _hex(hue, accent_l, 0.78)
    shift = ((_hash(team_id) >> 20) % 3 - 1) * 30
    return ClubColours(primary=primary, secondary=_hex(hue + shift, 0.88, 0.55), text=WHITE, accent=accent)


def crest_for(team_id: str, short_name: str) -> Crest:
    return Crest(pattern=PATTERNS[(_hash(team_id) // len(HUES)) % len(PATTERNS)], initials=short_name[:3].upper())


def club_hues(home_id: str, away_id: str) -> tuple[float, float]:
    """Home keeps its hue; away is rotated 120 degrees if the two would look alike (and off the amber band)."""
    home, away = base_hue(home_id), base_hue(away_id)
    if hue_gap(home, away) < MIN_HUE_GAP:
        away = (away + 120) % 360
        if AMBER_BAND[0] <= away < AMBER_BAND[1]:
            away = (away + 45) % 360
    return home, away
