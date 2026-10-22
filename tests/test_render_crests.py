"""Generated club identities: readable, distinct, deterministic, and clear of the evidence layer's amber."""

from __future__ import annotations

import itertools
import unittest

from pitchside.render import crests
from pitchside.teams import CLUBS


class CrestTests(unittest.TestCase):
    def pairs(self):
        return itertools.permutations(CLUBS, 2)

    def test_text_on_club_colours_is_readable(self) -> None:
        for home, away in self.pairs():
            for team, hue in zip((home, away), crests.club_hues(home, away)):
                colours = crests.colours_for(team, hue)
                self.assertGreaterEqual(crests.contrast(colours.text, colours.primary), crests.TEXT_CONTRAST, team)
                self.assertGreaterEqual(crests.contrast(colours.accent, crests.PITCH_BG), crests.GRAPHIC_CONTRAST,
                                        team)

    def test_the_two_clubs_never_look_alike(self) -> None:
        for home, away in self.pairs():
            a, b = crests.club_hues(home, away)
            self.assertGreaterEqual(crests.hue_gap(a, b), crests.MIN_HUE_GAP, (home, away))

    def test_no_club_takes_the_evidence_amber(self) -> None:
        for home, away in self.pairs():
            for hue in crests.club_hues(home, away):
                self.assertFalse(crests.AMBER_BAND[0] <= hue < crests.AMBER_BAND[1], (home, away, hue))

    def test_a_clash_rotates_the_away_side(self) -> None:
        clash = next(((a, b) for a, b in itertools.permutations([f"t{i}" for i in range(40)], 2)
                      if crests.hue_gap(crests.base_hue(a), crests.base_hue(b)) < crests.MIN_HUE_GAP), None)
        self.assertIsNotNone(clash)
        home, away = crests.club_hues(*clash)
        self.assertEqual(home, crests.base_hue(clash[0]))
        self.assertGreaterEqual(crests.hue_gap(home, away), crests.MIN_HUE_GAP)

    def test_identities_are_deterministic_and_varied(self) -> None:
        self.assertEqual(crests.colours_for("aur", 140.0), crests.colours_for("aur", 140.0))
        self.assertEqual(len({crests.base_hue(t) for t in CLUBS}), len(CLUBS))
        self.assertEqual(len({crests.crest_for(t, t).pattern for t in CLUBS}), len(CLUBS))

    def test_contrast_matches_the_wcag_reference_values(self) -> None:
        self.assertAlmostEqual(crests.contrast("#ffffff", "#000000"), 21.0, places=2)
        self.assertAlmostEqual(crests.contrast("#777777", "#ffffff"), 4.48, places=2)


if __name__ == "__main__":
    unittest.main()
