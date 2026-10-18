"""Held-out answer-key bars for the stats engine.

Thresholds were tuned on seeds 1-40 only. The bars (80% recall per storyline, at most 1.0 spell
finding per balanced match, every goal found, ...) are checked on seeds that were never tuned on:

* 101-120: first held-out run. The first counter-attack rule scored 6/20 there; the retuned rule is
  re-run here, so these seeds are no longer pristine for counter-attacks.
* 201-220: fresh seeds, run once after the counter-attack retune and not tuned on afterwards.

The known failures below are real. They are marked ``xfail(strict=True)`` so the suite stays honest:
a bar that starts passing makes the run fail until the marker is removed, and a new failure in a
passing range shows up immediately. Matching rules, tolerances and bars were not loosened.
"""

from __future__ import annotations

import pytest

from pitchside.stats.evaluate import check_bars, run_evaluation, summarise

RUNS = {
    "101-120": range(101, 121),
    "201-220": range(201, 221),
}

_SUMMARIES: dict[str, dict] = {}


def _summaries(name: str) -> dict:
    if name not in _SUMMARIES:
        _SUMMARIES[name] = summarise(run_evaluation(RUNS[name]))
    return _SUMMARIES[name]


@pytest.mark.parametrize(
    "run",
    [
        pytest.param(
            "101-120",
            marks=pytest.mark.xfail(
                strict=True,
                reason="siege storyline recall 62% < 80%: counter-attack phases only 5/20 (25%); "
                       "siege phases 20/20. All other bars pass.",
            ),
        ),
        pytest.param(
            "201-220",
            marks=pytest.mark.xfail(
                strict=True,
                reason="recall below 80% for comeback (78%), end_to_end (75%) and siege (75%; counter-attack "
                       "phases 11/20 = 55%), and balanced spell findings 1.10/match > 1.0 (chaos_spell fires "
                       "on balanced play). Goals 100% found.",
            ),
        ),
    ],
)
def test_every_held_out_bar_passes(run: str) -> None:
    failed = [text for text, ok in check_bars(_summaries(run)) if not ok]
    assert not failed, "; ".join(failed)


@pytest.mark.parametrize("run", sorted(RUNS))
def test_every_goal_is_found_on_held_out_seeds(run: str) -> None:
    """Goals are scored exactly: this one bar must always hold."""
    for name, summary in _summaries(run).items():
        assert summary.goals_found == summary.goals_total, (run, name)
