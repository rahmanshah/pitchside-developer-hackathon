"""Bundles for the render tests: each (seed, storyline, faults) is run offline once per session and cached."""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path

from pitchside.agents.bundle import write_bundle
from pitchside.agents.faults import demo_plan

from test_agents_pipeline import run

_TMP = tempfile.TemporaryDirectory()  # removed at exit


@functools.lru_cache(maxsize=None)
def bundle_dir(seed: int = 7, storyline: str = "comeback", faults: bool = False) -> Path:
    result = run(seed=seed, storyline=storyline, faults=demo_plan() if faults else None,
                 run_id=f"test-{seed}-{storyline}-{'faults' if faults else 'clean'}")
    return write_bundle(result, Path(_TMP.name) / f"{seed}-{storyline}-{faults}")
