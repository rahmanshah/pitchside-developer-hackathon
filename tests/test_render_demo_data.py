"""web/data: the published sample runs are honest, current and consistent with their index."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from pitchside.agents.bundle import FILES, BundleManifest, load_bundle
from pitchside.render.demo_data import FAULTS_NOTE, INDEX_FILE, publish, read_index
from pitchside.render.overlay import OVERLAY_FILE, dump_overlay, overlay_from_dir

from _render import bundle_dir

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "data"
STALE = "run `uv run pitchside demo-data` to regenerate web/data"


class PublishedDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = read_index(DATA)

    def manifest(self, slug: str) -> BundleManifest:
        return BundleManifest.model_validate_json((DATA / slug / FILES["manifest"]).read_text(encoding="utf-8"))

    def test_the_index_lists_exactly_the_run_directories(self) -> None:
        dirs = sorted(p.name for p in DATA.iterdir() if p.is_dir())
        self.assertEqual(sorted(r.slug for r in self.index.runs), dirs)
        self.assertGreaterEqual(len(dirs), 3)

    def test_index_entries_say_what_their_manifests_say(self) -> None:
        for entry in self.index.runs:
            m = self.manifest(entry.slug)
            self.assertEqual((entry.llm_mode, entry.label, entry.faults_injected, entry.faults_fired),
                             (m.pipeline.llm_mode, m.label, m.faults.injected, len(m.faults.fired)), entry.slug)
            self.assertEqual((entry.match_id, entry.run_id, entry.final_score),
                             (m.match_id, m.run_id, m.final_score))

    def test_offline_samples_are_labelled_as_offline(self) -> None:
        for entry in self.index.runs:
            if entry.llm_mode == "fake":
                self.assertTrue(entry.label.startswith("Offline run (fake model client)"), entry.slug)
                self.assertNotIn("live pipeline", entry.label)

    def test_exactly_one_run_has_injected_faults_and_the_index_says_so(self) -> None:
        faulted = [e for e in self.index.runs if e.faults_injected]
        self.assertEqual(len(faulted), 1, [e.slug for e in faulted])
        self.assertIn(FAULTS_NOTE, faulted[0].description)
        self.assertIn("faults were deliberately injected", faulted[0].label)
        self.assertGreater(faulted[0].faults_fired, 0)

    def test_every_run_loads_and_its_overlay_is_current_and_valid(self) -> None:
        schema = json.loads((ROOT / "docs" / "overlay.schema.json").read_text(encoding="utf-8"))
        for entry in self.index.runs:
            path = DATA / entry.slug
            load_bundle(path)
            on_disk = (path / OVERLAY_FILE).read_text(encoding="utf-8")
            self.assertEqual(on_disk, dump_overlay(overlay_from_dir(path)), f"{entry.slug}: {STALE}")
            jsonschema.validate(json.loads(on_disk), schema)


class PublishTests(unittest.TestCase):
    def test_publish_copies_the_bundle_and_upserts_the_index_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            publish(bundle_dir(), data, "one", "One")
            publish(bundle_dir(faults=True), data, "two", "Two", "A demo.")
            publish(bundle_dir(), data, "one", "One again")
            index = read_index(data)
            self.assertEqual([(r.slug, r.title) for r in index.runs], [("one", "One again"), ("two", "Two")])
            self.assertIn(FAULTS_NOTE, index.runs[1].description)
            self.assertTrue((data / "one" / OVERLAY_FILE).exists())
            self.assertTrue((data / INDEX_FILE).exists())
            load_bundle(data / "two")

    def test_bad_slugs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for slug in ("", "../x", "a/b", "a b"):
                with self.assertRaises(ValueError):
                    publish(bundle_dir(), Path(tmp), slug, "x")


if __name__ == "__main__":
    unittest.main()
