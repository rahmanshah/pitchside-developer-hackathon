"""Ledger labels ("say") are pasted verbatim by the models, so each must read right and pass the checker on its own.

Decision 23: live runs pasted "goals 2, including this one", "restart or a save" and "X completing 73% of their
passes" into claims. Every label is now a clause or a phrase that survives being pasted as a sentence, names its
window once in words (never a duration the checker would read as a number) and says "across both teams" for
match-wide figures.
"""

from __future__ import annotations

import unittest

from pitchside.agents.checker import CheckContext, check_claim, scan
from pitchside.agents.contracts import Claim, ClaimNumber
from pitchside.agents.jobs import FindingPoint, RecapPoint, walk_match
from pitchside.agents.languages import ENGLISH, LANGUAGES, SPANISH
from pitchside.agents.ledger import goal_margin_say, player_say, window_say
from pitchside.schema import Score

from _agents import match

MATCHES = ((1, "comeback"), (2, "siege"), (3, "end_to_end"), (4, "balanced"), (5, "comeback"), (6, "siege"))
WINDOW_WORDS = ("in this spell", "in the last few minutes", "in that spell", "in the match")


def jobs():
    for seed, story in MATCHES:
        for point in walk_match(match(seed, story).events):
            if isinstance(point, FindingPoint):
                yield point.job
            elif isinstance(point, RecapPoint):
                yield point.make([])


def pasted(item, ledger) -> Claim:
    """The label pasted as a sentence of its own, citing its id (and its player), its numbers listed."""
    refs = [item.ref] + [p.ref for p in ledger.players if item.player_id and p.player_id == item.player_id]
    return Claim(claim_id="k1", text=item.say[:1].upper() + item.say[1:] + ".", team_id=item.team_id, refs=refs,
                 numbers=[ClaimNumber(value=t.value, ref=item.ref) for t in scan(item.say).numbers])


class PastedLabelTests(unittest.TestCase):
    def test_every_label_passes_the_checker_pasted_as_a_sentence(self) -> None:
        checked = set()
        for job in jobs():
            ctx = CheckContext.for_job(job)
            for item in job.ledger.items:
                check = check_claim(pasted(item, job.ledger), ctx)
                with self.subTest(job=job.job_id, ref=item.ref):
                    self.assertTrue(check.ok, f"{item.say!r}: {check.reasons}")
                checked.add(item.metric)
        # every kind of label the matches produce was pasted, including the rewritten ones
        for metric in ("goal_margin", "player_goals", "possession_won_in_open_play", "seconds_since_turnover",
                       "opponent_pass_completion", "turnovers_per_min", "mean_spell", "shots", "xg"):
            self.assertIn(metric, checked)

    def test_a_label_names_at_most_one_window(self) -> None:
        for job in jobs():
            for item in job.ledger.items:
                self.assertLessEqual(sum(item.say.count(w) for w in WINDOW_WORDS), 1, item.say)

    def test_window_words_hold_no_number_and_no_duration(self) -> None:
        # "over the last 5 minutes" would be an unlisted number; "between the two teams" a number word.
        for lang in LANGUAGES.values():
            phrases = [lang.phrases[k] for k in lang.phrases if k.startswith(("window.", "combined."))]
            for phrase in phrases:
                self.assertEqual(scan(phrase.replace("{v}", "").replace("{window}", ""), lang).numbers, [], phrase)


class RewrittenLabelTests(unittest.TestCase):
    def test_the_scorers_tally_is_a_clause(self) -> None:
        self.assertEqual(player_say("player_goals", 2, "Rafa Yaldiran", scored_this=True),
                         "Rafa Yaldiran has 2 goals in the match, including this one")
        self.assertEqual(player_say("player_goals", 1, "Rafa Yaldiran", scored_this=True),
                         "this is Rafa Yaldiran's first goal of the match")
        self.assertEqual(player_say("player_goals", 1, "Rafa Yaldiran"), "Rafa Yaldiran has 1 goal in the match")
        self.assertEqual(player_say("player_shots", 5, "Rafa Yaldiran"), "Rafa Yaldiran has 5 shots in the match")
        self.assertEqual(player_say("player_xg", 1.524, "Rafa Yaldiran"), "Rafa Yaldiran has 1.52 xG in the match")
        self.assertEqual(player_say("player_goals", 2, "Rafa Yaldiran", SPANISH, scored_this=True),
                         "Rafa Yaldiran suma 2 goles en el partido, contando este")

    def test_the_score_comes_first_and_no_club_is_named_after_it(self) -> None:
        self.assertEqual(goal_margin_say(0, "Aurora Rovers", Score(home=1, away=1)), "it is 1-1: Aurora Rovers equalise")
        self.assertEqual(goal_margin_say(-1, "Ironvale Athletic", Score(home=2, away=1)),
                         "it is 2-1: Ironvale Athletic pull one back")

    def test_match_wide_numbers_are_combined_figures_with_their_window(self) -> None:
        self.assertEqual(window_say("turnovers_per_min", 3.0, "per_min", "spell"),
                         "3.0 turnovers per minute across both teams in this spell")
        self.assertEqual(window_say("pass_completion", 72.3, "pct", "spell"),
                         "72% pass completion for both teams combined in this spell")
        self.assertEqual(window_say("mean_spell", 19.2, "s", "spell"),
                         "possessions lasting 19 s on average across both teams in this spell")
        self.assertEqual(window_say("chaos_index", 0.86, "index", "spell"), "chaos index +0.86 in this spell")
        self.assertEqual(window_say("turnovers_per_min", 3.0, "per_min", "spell", SPANISH),
                         "3,0 pérdidas por minuto entre ambos equipos en esta fase")

    def test_one_clubs_window_counts_name_their_window_once(self) -> None:
        self.assertEqual(window_say("shots", 3, "count", "spell"), "3 shots in this spell")
        self.assertEqual(window_say("shots", 1, "count", "recent"), "1 shot in the last few minutes")
        self.assertEqual(window_say("xg", 0.849, "xg", "earlier"), "0.85 xG in that spell")

    def test_counts_of_one_are_singular(self) -> None:
        from pitchside.agents.ledger import say
        self.assertEqual(say("passes_in_spell", 1, "count"), "1 pass in the move")
        self.assertEqual(say("match_shots", 1, "count"), "1 shot in the match")
        self.assertEqual(say("shots_total", 1, "count"), "1 shot")
        self.assertEqual(say("passes_in_spell", 4, "count"), "4 passes in the move")
        self.assertEqual(say("tackles_won", 1, "count", SPANISH), "1 entrada ganada")
        self.assertEqual(say("opponent_pass_completion", 73.3, "pct", ENGLISH, "Aurora Rovers"),
                         "Aurora Rovers are completing 73% of their passes")


if __name__ == "__main__":
    unittest.main()
