"""The demo shot list (docs/DEMO.md) only uses URLs that work on the published runs in web/data."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
DEMO = (ROOT / "docs" / "DEMO.md").read_text(encoding="utf-8")
DATA = ROOT / "web" / "data"
URLS = re.findall(r"`(\?run=[^`\s]+)`", DEMO)
KNOWN = {"run", "view", "t", "speed", "autoplay", "clean", "persona", "lang", "drawer", "motion", "holds"}


def clock_of(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2})", value)
    assert match, f"t={value}: the shot list uses match-clock times (mm:ss)"
    return int(match.group(1)) * 60 + int(match.group(2))


class DemoDocTests(unittest.TestCase):
    def setUp(self) -> None:
        index = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
        self.runs = {run["slug"]: run for run in index["runs"]}

    def overlay(self, slug: str) -> dict:
        return json.loads((DATA / slug / "overlay.json").read_text(encoding="utf-8"))

    def test_the_shot_list_has_urls(self) -> None:
        self.assertGreaterEqual(len(URLS), 6)

    def test_every_url_resolves_on_the_published_runs(self) -> None:
        for url in URLS:
            with self.subTest(url=url):
                query = {k: v[0] for k, v in parse_qs(url[1:], strict_parsing=True).items()}
                self.assertLessEqual(set(query), KNOWN)
                self.assertIn(query["run"], self.runs)
                doc = self.overlay(query["run"])
                self.assertIn(query.get("view", "broadcast"), {"broadcast", "analyst"})
                self.assertIn(query.get("lang", "en"), doc["languages"])
                personas = {p["persona"] for p in doc["personas"]} | {"fav", "fav:home", "fav:away"}
                self.assertIn(query.get("persona", "casual"), personas)
                if "speed" in query:
                    self.assertGreater(float(query["speed"]), 0)
                if "drawer" in query:
                    overlays = {o["overlay_id"]: o for o in doc["overlays"]}
                    self.assertIn(query["drawer"], overlays)
                    # The take seeks to the moment it opens, not somewhere else in the match.
                    self.assertLess(abs(overlays[query["drawer"]]["clock_s"] - clock_of(query["t"])), 60)

    def test_the_recovery_shots_use_a_run_with_injected_faults(self) -> None:
        recovery = [url for url in URLS if parse_qs(url[1:])["run"][0] == "faults-demo"]
        self.assertTrue(recovery)
        self.assertTrue(self.runs["faults-demo"]["faults_injected"])


if __name__ == "__main__":
    unittest.main()
