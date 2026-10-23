"""Publish run bundles into ``web/data`` for the static demo, with an index of available runs.

``pitchside demo-data`` regenerates the offline sample runs (fake model client, so they cost nothing and say so).
``pitchside demo-data --bundle DIR --slug S --title T`` publishes one recorded bundle, e.g. a live run after the
Azure milestone. Index entries copy their honesty fields (model mode, the manifest label, injected faults) from the
bundle's manifest, so the index cannot describe a run differently from the run itself.

A published live run (``llm_mode: foundry``) cost money and cannot be re-recorded for free, so neither command
replaces one unless asked to with ``overwrite_live`` (``--overwrite-live``).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..agents.bundle import FILES, BundleManifest, write_bundle
from .overlay import OVERLAY_FILE, dump_overlay, overlay_from_dir

INDEX_FILE = "index.json"
INDEX_VERSION = "1.0"
COPIED = [name for key, name in FILES.items() if key != "overlay"]
FAULTS_NOTE = "Faults were injected on purpose (opt-in) to show how the pipeline recovers."


class LiveRunError(ValueError):
    """Publishing would replace a published live run, and overwriting live runs was not asked for."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TeamSummary(_Frozen):
    team_id: str
    name: str
    short_name: str


class IndexEntry(_Frozen):
    slug: str = Field(description="Directory under data/ and the ?run= URL parameter.")
    title: str
    description: str
    match_id: str
    run_id: str
    created_utc: str
    storyline: Optional[str] = None
    seed: Optional[int] = None
    home: TeamSummary
    away: TeamSummary
    final_score: dict[str, int]
    llm_mode: Literal["fake", "foundry"]
    label: str = Field(description="The manifest's label, verbatim.")
    faults_injected: bool
    faults_fired: int
    fault_plan: Optional[str] = None


class DemoIndex(_Frozen):
    kind: Literal["pitchside-demo-index"] = "pitchside-demo-index"
    index_version: str = INDEX_VERSION
    runs: list[IndexEntry]


@dataclass(frozen=True)
class Sample:
    slug: str
    title: str
    description: str
    seed: int
    storyline: str
    home: str
    away: str
    faults: Optional[str] = None


SAMPLES = (
    Sample("comeback", "Comeback: Aurora Rovers v Ironvale Athletic",
           "A goal down at half-time, the home side lays siege and turns it round.", 7, "comeback", "aur", "irv"),
    Sample("siege", "Siege: Harbourton City v Westmere Town",
           "Long spells of pressure, a pressing surge and the goals that follow.", 3, "siege", "har", "wst"),
    Sample("faults-demo", "Recovery demo: Westmere Town v Aurora Rovers",
           "An end-to-end match. " + FAULTS_NOTE, 4, "end_to_end", "wst", "aur", faults="demo"),
)


def entry_for(path: Path, slug: str, title: str, description: str) -> IndexEntry:
    m = BundleManifest.model_validate_json((path / FILES["manifest"]).read_text(encoding="utf-8"))
    if m.faults.injected and FAULTS_NOTE not in description:
        description = f"{description} {FAULTS_NOTE}".strip()
    team = lambda t: TeamSummary(team_id=t.team_id, name=t.name, short_name=t.short_name)  # noqa: E731
    return IndexEntry(slug=slug, title=title, description=description, match_id=m.match_id, run_id=m.run_id,
                      created_utc=m.created_utc, storyline=m.generator.get("storyline"), seed=m.generator.get("seed"),
                      home=team(m.home), away=team(m.away), final_score=dict(m.final_score),
                      llm_mode=m.pipeline.llm_mode, label=m.label, faults_injected=m.faults.injected,
                      faults_fired=len(m.faults.fired), fault_plan=m.faults.plan)


def read_index(data_dir: Path) -> DemoIndex:
    path = Path(data_dir) / INDEX_FILE
    if not path.exists():
        return DemoIndex(runs=[])
    return DemoIndex.model_validate_json(path.read_text(encoding="utf-8"))


def write_index(data_dir: Path, index: DemoIndex) -> Path:
    path = Path(data_dir) / INDEX_FILE
    path.write_text(json.dumps(index.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def live_run_at(target: Path) -> Optional[str]:
    """Why ``target`` must not be replaced without ``overwrite_live``, or None if it holds no live run.

    A manifest that exists but cannot be read counts as live: it cannot be shown to be safe to replace.
    """
    path = Path(target) / FILES["manifest"]
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        mode, run_id = raw["pipeline"]["llm_mode"], raw.get("run_id")
    except (ValueError, KeyError, TypeError, OSError) as error:
        return f"{path} cannot be read ({type(error).__name__}), so it may be a live run"
    return f"{target} holds a live run (llm_mode foundry, run {run_id})" if mode == "foundry" else None


def _refuse_live(targets: list[Path], overwrite_live: bool) -> None:
    if overwrite_live:
        return
    reasons = [r for r in map(live_run_at, targets) if r]
    if reasons:
        raise LiveRunError("; ".join(reasons) + ". Nothing was changed. Pass --overwrite-live to replace it.")


def publish(bundle: Path, data_dir: Path, slug: str, title: str, description: str = "", *,
            overwrite_live: bool = False) -> IndexEntry:
    """Copy one bundle into ``data_dir/slug`` (replacing it), rebuild its overlay and upsert the index entry.

    Raises ``LiveRunError`` instead of replacing a published live run, unless ``overwrite_live``.
    """
    if not slug or not all(c.isalnum() or c in "-_" for c in slug):
        raise ValueError(f"bad slug {slug!r}: use letters, digits, '-' and '_'")
    bundle, data_dir = Path(bundle), Path(data_dir)
    target = data_dir / slug
    _refuse_live([target], overwrite_live)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in COPIED:
        if (bundle / name).exists():
            shutil.copyfile(bundle / name, target / name)
    (target / OVERLAY_FILE).write_text(dump_overlay(overlay_from_dir(target)), encoding="utf-8", newline="\n")
    entry = entry_for(target, slug, title, description)
    runs = [r for r in read_index(data_dir).runs if r.slug != slug]
    existing = [r.slug for r in read_index(data_dir).runs]
    position = existing.index(slug) if slug in existing else len(runs)
    runs.insert(position, entry)
    write_index(data_dir, DemoIndex(runs=runs))
    return entry


def run_sample(sample: Sample, work: Path) -> Path:
    """Run one sample offline (fake model client) and write its bundle under ``work``; returns the bundle path."""
    from ..agents.faults import parse_faults
    from ..agents.pipeline import RunConfig, build_pipeline, run_match
    from ..generator import generate_match
    from ..llm import load_settings

    settings = load_settings({"PITCHSIDE_LLM": "fake", "PITCHSIDE_USAGE_LOG": str(work / f"{sample.slug}.usage.jsonl"),
                              "PITCHSIDE_MATCH_BUDGET_USD": "0.25"}, env_file=None)
    result = generate_match(seed=sample.seed, home=sample.home, away=sample.away, storyline=sample.storyline)
    pipeline = build_pipeline(settings, parse_faults(sample.faults) if sample.faults else None)

    async def go():
        try:
            return await run_match(result.events, pipeline, RunConfig.from_settings(settings),
                                   run_id=f"sample-{sample.slug}", answer_key=result.answer_key)
        finally:
            await pipeline.aclose()

    return write_bundle(asyncio.run(go()), work)


def regenerate_samples(data_dir: Path, *, overwrite_live: bool = False) -> list[IndexEntry]:
    """Run every offline sample and publish it; the index then lists exactly the samples.

    Checks every sample slug first and raises ``LiveRunError`` before changing anything if one holds a live run,
    unless ``overwrite_live``.
    """
    data_dir = Path(data_dir)
    _refuse_live([data_dir / sample.slug for sample in SAMPLES], overwrite_live)
    data_dir.mkdir(parents=True, exist_ok=True)
    write_index(data_dir, DemoIndex(runs=[r for r in read_index(data_dir).runs if r.llm_mode == "foundry"]))
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        for sample in SAMPLES:
            path = run_sample(sample, Path(tmp))
            entries.append(publish(path, data_dir, sample.slug, sample.title, sample.description,
                                   overwrite_live=overwrite_live))
    return entries
