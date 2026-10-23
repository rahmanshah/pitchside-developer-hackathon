"""The demo shot list (docs/DEMO.md): its URLs work on the published runs in web/data, and the video plan fits."""

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

PLAN_S = 105  # 1:45, which leaves slack under the 2:00 limit
WORDS = (215, 245)  # "about 230 words" on every reading path
MAX_PACE = 2.5  # words per second in any one segment
# Which variant lines each reading path reads; unmarked lines are read on every path.
PATHS = {"live and deployed": {"LIVE", "Deployed"}, "offline and not deployed": {"OFFLINE", "Not deployed"}}
VARIANT = re.compile(r"^(LIVE|OFFLINE|Deployed|Not deployed)\b.*:$")


def clock_of(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2})", value)
    assert match, f"t={value}: the shot list uses match-clock times (mm:ss)"
    return int(match.group(1)) * 60 + int(match.group(2))


def seconds(span: str) -> tuple[int, int]:
    start, end = (int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in span.split("-"))
    return start, end


def shot_spans() -> list[tuple[int, int]]:
    return [seconds(m) for m in re.findall(r"^\| \d+ \| (\d:\d\d-\d:\d\d) \|", DEMO, flags=re.M)]


def narration() -> list[tuple[tuple[int, int], str]]:
    section = DEMO.split("## Narration", 1)[1].split("\n## ", 1)[0]
    parts = re.split(r"^\*\*(\d:\d\d-\d:\d\d) \(shots? [^)]*\)(?::\*\*|\*\*)", section, flags=re.M)
    return [(seconds(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]


def words_read(body: str, path: set[str]) -> int:
    count, variant = 0, None
    for line in body.splitlines():
        if match := VARIANT.match(line):
            variant = match.group(1)
        elif line.startswith(">") and (variant is None or variant in path):
            count += len(line[1:].split())
    return count


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


class VideoPlanTests(unittest.TestCase):
    def test_the_shots_run_back_to_back_and_fit_the_plan(self) -> None:
        spans = shot_spans()
        self.assertGreaterEqual(len(spans), 6)
        self.assertEqual(spans[0][0], 0)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertEqual(end, start, "shots must be contiguous")
        self.assertLessEqual(spans[-1][1], PLAN_S)
        self.assertLess(spans[-1][1], 120)

    def test_narration_segments_line_up_with_the_shots(self) -> None:
        boundaries = {t for span in shot_spans() for t in span}
        segments = narration()
        self.assertGreaterEqual(len(segments), 6)
        for (start, end), _ in segments:
            self.assertIn(start, boundaries)
            self.assertIn(end, boundaries)
        self.assertEqual([s for (s, _), _ in segments][1:], [e for (_, e), _ in segments][:-1])
        self.assertEqual(segments[-1][0][1], shot_spans()[-1][1])

    def test_every_reading_path_is_about_230_words_at_a_steady_pace(self) -> None:
        for name, path in PATHS.items():
            with self.subTest(path=name):
                total = 0
                for (start, end), body in narration():
                    words = words_read(body, path)
                    self.assertLessEqual(words / (end - start), MAX_PACE, f"{name}, {start}-{end} s: {words} words")
                    total += words
                self.assertGreaterEqual(total, WORDS[0])
                self.assertLessEqual(total, WORDS[1])


if __name__ == "__main__":
    unittest.main()
