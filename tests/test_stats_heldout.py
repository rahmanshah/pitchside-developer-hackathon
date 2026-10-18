"""Held-out answer-key bars for the stats engine.

Thresholds were tuned on seeds 1-40 only. The bars (80% recall per storyline, at most 1.0 spell
finding per balanced match, every goal found, ...) are checked on seeds that were not tuned on:

* 101-120: first held-out run (re-run after later retunes, so no longer pristine).
* 201-220: fresh when first run after the counter-attack retune (re-run after the chaos retune).
* 301-320: fresh, run once after the chaos retune and not tuned on afterwards.

Recall is tested one storyline at a time. The known shortfalls are real and are marked
``xfail(strict=True)`` with the measured numbers as the reason, so a bar that starts passing makes the
run fail until the marker is removed. Every other bar (goals found exactly, balanced spell rate, spells
and counters outside planted windows) is a plain assertion that passes on all three ranges.
Matching rules, tolerances and the 80% bar were not loosened.
"""

from __future__ import annotations

import pytest

from pitchside.stats.evaluate import MIN_RECALL, check_bars, run_evaluation, summarise

RUNS = {
    "101-120": range(101, 121),
    "201-220": range(201, 221),
    "301-320": range(301, 321),
}

# (run, storyline) -> measured recall and where it comes from
KNOWN_RECALL_SHORTFALLS = {
    ("101-120", "comeback"): "78%: chaos 65%, momentum shift 90%",
    ("101-120", "end_to_end"): "70%: chaos 70%",
    ("101-120", "siege"): "62%: siege 100%, counter-attack 25%",
    ("201-220", "comeback"): "78%: chaos 70%, momentum shift 85%",
    ("201-220", "end_to_end"): "75%: chaos 75%",
    ("201-220", "siege"): "75%: siege 95%, counter-attack 55%",
    ("301-320", "comeback"): "78%: chaos 60%, momentum shift 95%",
    ("301-320", "siege"): "65%: siege 100%, counter-attack 30%",
}

_SUMMARIES: dict[str, dict] = {}


def _summaries(run: str) -> dict:
    if run not in _SUMMARIES:
        _SUMMARIES[run] = summarise(run_evaluation(RUNS[run]))
    return _SUMMARIES[run]


def _recall_params():
    for run in RUNS:
        for storyline in ("comeback", "end_to_end", "siege"):
            known = KNOWN_RECALL_SHORTFALLS.get((run, storyline))
            marks = []
            if known:
                marks.append(pytest.mark.xfail(strict=True, reason=f"recall below 80% on seeds {run}: {known}"))
            yield pytest.param(run, storyline, marks=marks, id=f"{run}-{storyline}")


@pytest.mark.parametrize(("run", "storyline"), list(_recall_params()))
def test_scored_phase_recall_meets_the_bar(run: str, storyline: str) -> None:
    recall = _summaries(run)[storyline].scored.recall
    assert recall is not None and recall >= MIN_RECALL, f"{storyline} recall {recall:.0%} on seeds {run}"


@pytest.mark.parametrize("run", sorted(RUNS))
def test_every_other_bar_passes(run: str) -> None:
    """Goals exact, balanced spell rate, spells and counters outside planted windows: no known failures."""
    failed = [text for text, ok in check_bars(_summaries(run)) if not ok and "recall" not in text]
    assert not failed, "; ".join(failed)
