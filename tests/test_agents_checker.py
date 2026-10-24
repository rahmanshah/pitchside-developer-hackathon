"""Tests for the deterministic checker: good claims pass, each kind of bad claim is rejected with its reason."""

from __future__ import annotations

import unittest

from pitchside.agents.checker import CheckContext, check_claim, check_text, missing_goal_score, scan
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

    def test_a_club_claim_backed_only_by_match_wide_evidence(self) -> None:
        from pitchside.agents.contracts import Ledger
        ledger = Ledger()
        ledger.add_item("e", metric="chaos_index", team_id=None, value=0.8, unit="index", window_start_s=1000,
                        window_end_s=1400, say="chaos index +0.80", source="finding")
        ctx = CheckContext(**{**CTX.__dict__, "ledger": ledger, "window_start_s": 1000, "window_end_s": 1400})
        bad = Claim(claim_id="k1", text="Ironvale Athletic: chaos index +0.80.", team_id="irv", refs=["e1"],
                    numbers=[ClaimNumber(value=0.8, ref="e1")])
        self.assertEqual([r.code for r in check_claim(bad, ctx).reasons], ["team_mismatch"])
        good = bad.model_copy(update={"team_id": None, "text": "Chaos index +0.80 across both sides."})
        self.assertTrue(check_claim(good, ctx).ok)

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

    def test_a_score_joined_to_the_word_before_it(self) -> None:
        # What gpt-5-mini wrote in the first live smoke test: the right score, but no space before it.
        reasons = check_claim(claim("Ironvale Athletic scored, and it is0-1."), CTX).reasons
        self.assertEqual([r.code for r in reasons], ["glued_number"])
        self.assertIn('"is0-1" joins a word and a number; write "is 0-1" with a space', reasons[0].detail)
        self.assertEqual(codes(claim("Ironvale Athletic scored, and it is 0-1.")), [])

    def test_a_joined_number_does_not_slip_past_the_number_checks(self) -> None:
        # The number scan skips digits that follow a letter, so without this check "a0.48" went unchecked.
        self.assertEqual(codes(claim("Ironvale Athletic scored from a0.48 xG chance.")), ["glued_number"])

    def test_ledger_ids_are_not_joined_numbers(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic scored (e1).")), [])


class AttributionTests(unittest.TestCase):
    """The prompts ask for the club once per sentence (decision 21); the check itself must stay as strict."""

    def reasons(self, text: str, numbers=((0.05, "e1"), (21, "e2")), refs=("e1", "e2"), team_id="irv"):
        return check_claim(claim(text, refs=refs, numbers=numbers, team_id=team_id), CTX).reasons

    def test_a_club_named_once_covers_the_numbers_after_it(self) -> None:
        self.assertEqual(self.reasons("Ironvale Athletic scored with a 0.05 xG chance from 21 m."), [])

    def test_a_named_player_carries_their_club(self) -> None:
        text = f"{SCORER.name} scored with a 0.05 xG chance from 21 m."
        self.assertEqual(self.reasons(text, refs=("e1", "e2", SCORER.ref)), [])

    def test_with_no_club_named_the_claims_team_id_decides(self) -> None:
        self.assertEqual(self.reasons("A 0.05 xG chance from 21 m was enough."), [])
        reasons = self.reasons("A 0.05 xG chance from 21 m was enough.", team_id="aur")
        self.assertEqual([r.code for r in reasons], ["team_mismatch"] * 3)  # the claim, then both numbers
        self.assertIn("no club is named in its sentence, and the claim's team_id is Aurora Rovers", reasons[1].detail)

    def test_the_other_club_named_nearer_still_fails(self) -> None:
        reasons = self.reasons("Ironvale Athletic scored after Aurora Rovers allowed a 0.05 xG chance from 21 m.")
        self.assertEqual([r.code for r in reasons], ["team_mismatch", "team_mismatch"])
        self.assertEqual(reasons[0].detail,
                         '"0.05" (e1) belongs to Ironvale Athletic, but the club named last before it in this sentence '
                         'is Aurora Rovers; name Ironvale Athletic before this number (once per sentence)')

    def test_the_other_club_named_only_after_the_number_still_fails(self) -> None:
        reasons = self.reasons("A 0.05 xG chance from 21 m went to Aurora Rovers.")
        self.assertEqual([r.code for r in reasons], ["team_mismatch", "team_mismatch"])
        self.assertIn("no club is named before it, and the club named after it is Aurora Rovers", reasons[0].detail)

    def test_persona_texts_follow_the_same_rule(self) -> None:
        refs = ["e1", "e2"]
        self.assertEqual(check_text("For Aurora Rovers, a blow: Ironvale Athletic scored from 21 m.", CTX, refs), [])
        self.assertEqual([r.code for r in check_text("For Aurora Rovers, a blow from 21 m.", CTX, refs)],
                         ["team_mismatch"])


class FlagClaimTests(unittest.TestCase):
    def test_a_flag_is_cited_without_a_number(self) -> None:
        flag = next(i for i in LEDGER.items if i.metric == "possession_won_in_open_play")
        self.assertNotRegex(flag.say, r"\d")
        self.assertEqual(codes(claim(f"Ironvale Athletic: {flag.say}.", refs=(flag.ref,))), [])
        self.assertEqual(codes(claim(f"Ironvale Athletic: {flag.say}, and scored 29 s later.", refs=(flag.ref, "e6"),
                                     numbers=((29, "e6"),))), [])

    def test_a_shots_outcome_is_cited_in_words(self) -> None:
        outcome = next(i for i in LEDGER.items if i.metric == "shot_outcome")
        self.assertEqual(outcome.say, "the shot went in")
        self.assertEqual(codes(claim(f"{SCORER.name} scored from 21 m, and {outcome.say}.",
                                     refs=(outcome.ref, "e2", SCORER.ref), numbers=((21, "e2"),))), [])

    def test_no_digit_stands_for_a_word_fact(self) -> None:
        # A code ("3" for a goal, "1" for yes) is never a number a text may quote (decision 24).
        outcome = next(i for i in LEDGER.items if i.metric == "shot_outcome")
        flag = next(i for i in LEDGER.items if i.metric == "possession_won_in_open_play")
        self.assertEqual(codes(claim("Ironvale Athletic: outcome 3.", refs=(outcome.ref,),
                                     numbers=((3, outcome.ref),))), ["number_mismatch"])
        self.assertEqual(codes(claim("Ironvale Athletic won the ball in open play (1).", refs=(flag.ref,),
                                     numbers=((1, flag.ref),))), ["number_mismatch"])
        persona = check_text("Ironvale Athletic: outcome 3.", CTX, [outcome.ref])
        self.assertEqual([r.code for r in persona], ["unlisted_number"])


class ScorelineTests(unittest.TestCase):
    MARGIN = next(i for i in LEDGER.items if i.metric == "goal_margin")

    def test_a_goal_claim_cites_the_scoreline_with_no_number(self) -> None:
        text = "Ironvale Athletic open the scoring, and it is 0-1."
        self.assertEqual(codes(claim(text, refs=(self.MARGIN.ref,))), [])
        self.assertEqual(codes(claim(f"{SCORER.name} scored his 1st goal: Aurora Rovers 0-1 Ironvale Athletic.",
                                     refs=(self.MARGIN.ref, SCORER.ref))), [])

    def test_a_wrong_score_is_still_rejected_even_when_the_scoreline_is_cited(self) -> None:
        self.assertEqual(codes(claim("Ironvale Athletic go ahead: it is 2-0.", refs=(self.MARGIN.ref,))),
                         ["score_mismatch"])


def _brace_job():
    """Seed 1 comeback goal:2: Aurora Rovers (home) go 2-1 up, the scorer's second goal."""
    from test_agents_fakes_faults import model_jobs
    return next(j for j in model_jobs() if j.job_id.endswith(":goal:2"))


class PastedScorelineTests(unittest.TestCase):
    """Two live failures (seeds 206-210, decision 23), and that the checks they hit are as strict as before."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.job = _brace_job()
        cls.ctx = CheckContext.for_job(cls.job)
        led = cls.job.ledger
        cls.scorer = led.players[0]
        cls.margin = next(i for i in led.items if i.metric == "goal_margin")
        cls.tally = next(i for i in led.items if i.metric == "player_goals" and i.player_id == cls.scorer.player_id)
        cls.clock = led.item("e6")  # seconds_since_turnover

    def codes(self, text: str, refs, numbers) -> list[str]:
        c = Claim(claim_id="k1", text=text, team_id="aur", refs=list(refs),
                  numbers=[ClaimNumber(value=v, ref=r) for v, r in numbers])
        return [r.code for r in check_claim(c, self.ctx).reasons]

    def test_the_numbers_after_a_pasted_scoreline_belong_to_the_scorer(self) -> None:
        # balanced_0206 pasted "Aurora Rovers 1-0 Ironvale Athletic (goals 1, ...)": the away club was named last.
        self.assertEqual((self.margin.say, self.tally.say),
                         ("it is 2-1: Aurora Rovers go ahead",
                          f"{self.scorer.name} has 2 goals in this match, including this one"))
        seconds = round(self.clock.value)
        new = f"{self.scorer.name} scored; {self.margin.say}, {seconds} s after winning the ball; {self.tally.say}."
        self.assertEqual(self.codes(new, [self.scorer.ref, self.margin.ref, "e6", self.tally.ref],
                                    [(seconds, "e6"), (2, self.tally.ref)]), [])
        old = f"{self.scorer.name} scored; Aurora Rovers 2-1 Ironvale Athletic, {seconds} s after winning the ball."
        self.assertEqual(self.codes(old, [self.scorer.ref, "e6"], [(seconds, "e6")]), ["team_mismatch"])  # unchanged

    def test_a_score_digit_listed_beside_the_tally_does_not_take_its_place(self) -> None:
        # comeback_0208 listed the score's 2 and 1 under goal_margin, so the tally's "2" was matched to goal_margin.
        text = f"{self.scorer.name} put Aurora Rovers 2-1 ahead; {self.tally.say}."
        refs = [self.scorer.ref, self.margin.ref, self.tally.ref]
        self.assertEqual(self.codes(text, refs, [(2, self.margin.ref), (1, self.margin.ref), (2, self.tally.ref)]), [])
        wrong = text.replace("has 2 goals", "has 3 goals")
        self.assertEqual(self.codes(wrong, refs, [(3, self.tally.ref)]), ["number_mismatch"])
        other_club = f"Ironvale Athletic have 2 goals in the match; {self.scorer.name} scored."
        self.assertIn("team_mismatch", self.codes(other_club, refs, [(2, self.tally.ref)]))


class GoalScoreRuleTests(unittest.TestCase):
    def test_a_goal_needs_a_claim_that_cites_goal_margin_and_states_the_score(self) -> None:
        job = _brace_job()
        margin = next(i.ref for i in job.ledger.items if i.metric == "goal_margin")
        say_it = Claim(claim_id="a", text="It is 2-1: Aurora Rovers go ahead.", team_id="aur", refs=[margin])
        no_cite = Claim(claim_id="b", text="Aurora Rovers lead 2-1.", team_id="aur", refs=["e1"])
        no_score = Claim(claim_id="c", text="Aurora Rovers go ahead.", team_id="aur", refs=[margin])
        wrong_score = Claim(claim_id="d", text="Aurora Rovers go ahead at 1-0.", team_id="aur", refs=[margin])
        self.assertIsNone(missing_goal_score(job, [no_score, say_it]))
        for claims in ([no_cite], [no_score], [wrong_score], [no_cite, no_score]):
            problem = missing_goal_score(job, claims)
            self.assertTrue(problem.startswith("state the score:"), problem)
            self.assertIn('"it is 2-1"', problem)
            self.assertIn(margin, problem)

    def test_other_jobs_have_no_score_rule(self) -> None:
        from test_agents_fakes_faults import model_jobs
        other = next(j for j in model_jobs() if j.label != "goal")
        self.assertIsNone(missing_goal_score(other, []))


class MinuteTests(unittest.TestCase):
    CTX24 = CheckContext(**{**CTX.__dict__, "minute": 24})  # GOAL is detected at 23:11, minute 24

    def reasons(self, text: str, ctx=None) -> list[str]:
        return [r.code for r in check_claim(claim(text), ctx or self.CTX24).reasons]

    def test_the_jobs_own_minute_in_natural_forms(self) -> None:
        for text in ("Ironvale Athletic scored in the 24th minute.", "Ironvale Athletic scored in minute 24.",
                     "Ironvale Athletic scored at 24'.", "Ironvale Athletic scored after 24 minutes.",
                     "Ironvale Athletic scored on 24 mins."):
            self.assertEqual(self.reasons(text), [], text)
        self.assertEqual(check_text("Ironvale Athletic marca a los 24 minutos.", self.CTX24, ["e1"], SPANISH), [])

    def test_other_minutes_written_as_durations_are_still_numbers(self) -> None:
        self.assertEqual(self.reasons("Ironvale Athletic scored after 30 minutes."), ["unlisted_number"])
        self.assertEqual(self.reasons("Ironvale Athletic scored after 24 minutes.", CTX), ["unlisted_number"])  # no minute
        self.assertEqual(check_text("Ironvale Athletic marca a los 30 minutos.", self.CTX24, ["e1"], SPANISH)[0].code,
                         "unlisted_number")

    def test_a_clock_range_is_a_window_not_numbers_or_a_score(self) -> None:
        s = scan("During 81:01–89:01 the match had a chaos index +0.86.")
        self.assertEqual(([(a, b) for a, b, _ in s.scores], sorted(m for m, _ in s.minutes)), ([], [82, 90]))
        self.assertEqual([t.value for t in s.numbers], [0.86])
        self.assertEqual(self.reasons("Ironvale Athletic scored at 23:11."), [])
        self.assertEqual(self.reasons("Ironvale Athletic pressed during 81:01–89:01."), ["window_mismatch"] * 2)


class PersonaTextTests(unittest.TestCase):
    def test_spanish_text_with_verified_numbers_passes(self) -> None:
        text = f"¡{SCORER.name} marca para Ironvale Athletic con una ocasión de 0,05 xG! Aurora Rovers 0-1 Ironvale Athletic."
        self.assertEqual(check_text(text, CTX, ["e1"], SPANISH), [])

    def test_persona_text_with_a_new_number_is_rejected(self) -> None:
        reasons = check_text("Ironvale Athletic score from 0.30 xG.", CTX, ["e1"])
        self.assertEqual([r.code for r in reasons], ["unlisted_number"])

    def test_persona_text_with_a_joined_score_is_rejected_in_any_language(self) -> None:
        self.assertEqual([r.code for r in check_text("¡GOL! Marca Ironvale Athletic. Va0-1.", CTX, ["e1"], SPANISH)],
                         ["glued_number"])

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
