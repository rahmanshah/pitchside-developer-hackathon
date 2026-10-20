"""Tests for the deterministic checker: good claims pass, each kind of bad claim is rejected with its reason."""

from __future__ import annotations

import unittest

from pitchside.agents.checker import CheckContext, check_claim, check_text, scan
from pitchside.agents.contracts import Claim, ClaimNumber, GoalAt
from pitchside.agents.languages import SPANISH
from pitchside.agents.ledger import finding_ledger

from _agents import events_by_id, facts, first, tally_until

GOAL = first("goal")  # seed 1 comeback: Ironvale Athletic score at 23:11 to make it 0-1
F = facts()
LEDGER = finding_ledger(GOAL, F, events_by_id(), tally_until(1, "comeback", GOAL.detected_at_s))
CTX = CheckContext(ledger=LEDGER, facts=F, score=GOAL.score,
                   goals=[GoalAt(clock_s=GOAL.detected_at_s, team_id=GOAL.team_id, player_id="irv_09",
                                 score=GOAL.score)],
                   window_start_s=GOAL.start_s, window_end_s=GOAL.end_s)
XG = LEDGER.item("e1")  # shot_xg 0.048
DIST = LEDGER.item("e2")  # shot_distance 20.7 m
SCORER = LEDGER.players[0]


def claim(text: str, refs=("e1",), numbers=(), team_id="irv") -> Claim:
    return Claim(claim_id="k1", text=text, team_id=team_id, refs=list(refs),
                 numbers=[ClaimNumber(value=v, ref=r) for v, r in numbers])


def codes(c: Claim) -> list[str]:
    return [r.code for r in check_claim(c, CTX).reasons]


class GoodClaimTests(unittest.TestCase):
    def test_a_grounded_claim_passes(self) -> None:
        good = claim(f"{SCORER.name} put Ironvale Athletic ahead with a 0.05 xG shot from 21 m, 0-1 in the "
                     "24th minute.", refs=("e1", "e2", SCORER.ref), numbers=((0.05, "e1"), (21, "e2")))
        self.assertEqual(codes(good), [])

    def test_seconds_may_be_written_as_minutes(self) -> None:
        spell = LEDGER.item("e6")  # seconds_since_turnover 29 s
        self.assertEqual(spell.metric, "seconds_since_turnover")
        self.assertEqual(codes(claim("Ironvale Athletic scored 29 s after winning the ball.", refs=("e6",),
                                     numbers=((29, "e6"),))), [])

    def test_ordinals_and_the_score_are_not_quantities(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic's 1st goal makes it 0-1.", refs=("e1",))), [])


class BadClaimTests(unittest.TestCase):
    def test_wrong_number(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic scored from 0.48 xG.", numbers=((0.48, "e1"),))),
                         ["number_mismatch"])

    def test_rounding_too_far_is_a_wrong_number(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic scored from 0 xG.", numbers=((0, "e1"),))),
                         ["number_mismatch"])

    def test_wrong_team(self) -> None:
        bad = claim("Aurora Rovers created a 0.05 xG chance.", numbers=((0.05, "e1"),), team_id="aur")
        self.assertEqual(sorted(set(codes(bad))), ["team_mismatch"])

    def test_number_given_to_the_other_club_in_the_text(self) -> None:
        bad = claim("Ironvale Athletic scored, but Aurora Rovers had the 0.05 xG chance.", numbers=((0.05, "e1"),))
        self.assertEqual(codes(bad), ["team_mismatch"])

    def test_no_evidence(self) -> None:
        self.assertIn("no_evidence", codes(claim("Ironvale Athletic were brilliant.", refs=())))

    def test_unknown_evidence(self) -> None:
        self.assertIn("unknown_evidence", codes(claim("Ironvale Athletic scored.", refs=("e99",))))

    def test_number_not_in_the_claims_numbers(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic had 7 shots.", refs=("e1",))), ["unlisted_number"])

    def test_number_citing_evidence_the_claim_does_not_cite(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic shot from 21 m.", refs=("e1",), numbers=((21, "e2"),))),
                         ["number_not_cited"])

    def test_minute_outside_the_evidence_window(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic scored in the 80th minute.")), ["window_mismatch"])

    def test_score_the_match_never_had(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic lead 2-0.")), ["score_mismatch"])

    def test_player_not_in_the_evidence(self) -> None:
        other = next(p for p in F.roster if p.team_id == "irv" and p.player_id != SCORER.player_id)
        self.assertEqual(codes(claim(f"{other.name} scored for Ironvale Athletic.")), ["unreferenced_player"])

    def test_number_words_cannot_slip_past(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic needed three passes.")), ["unlisted_number"])


class PersonaTextTests(unittest.TestCase):
    def test_spanish_text_with_verified_numbers_passes(self) -> None:
        text = f"¡{SCORER.name} marca para Ironvale Athletic con una ocasión de 0,05 xG! Aurora Rovers 0-1 Ironvale Athletic."
        self.assertEqual(check_text(text, CTX, ["e1"], SPANISH), [])

    def test_persona_text_with_a_new_number_is_rejected(self) -> None:
        reasons = check_text("Ironvale Athletic score from 0.30 xG.", CTX, ["e1"])
        self.assertEqual([r.code for r in reasons], ["unlisted_number"])

    def test_featured_players_may_be_named_in_persona_texts(self) -> None:
        striker = next(p for p in F.roster if p.team_id == "aur" and p.position == "ST")
        text = f"Ironvale Athletic score against {striker.name}'s side."
        self.assertEqual([r.code for r in check_text(text, CTX, ["e1"])], ["unreferenced_player"])
        ctx = CheckContext(**{**CTX.__dict__, "extra_players": frozenset({striker.player_id})})
        self.assertEqual(check_text(text, ctx, ["e1"]), [])


class ScanTests(unittest.TestCase):
    def test_scan_separates_numbers_scores_and_minutes(self) -> None:
        s = scan("Aurora Rovers 1-0 Ironvale Athletic in the 71st minute after 6 minutes of pressure at 71%, 90+2'.")
        self.assertEqual([(a, b) for a, b, _ in s.scores], [(1, 0)])
        self.assertEqual(sorted(m for m, _ in s.minutes), [71, 92])
        self.assertEqual([(t.value, t.minutes) for t in s.numbers], [(6.0, True), (71.0, False)])


if __name__ == "__main__":
    unittest.main()
