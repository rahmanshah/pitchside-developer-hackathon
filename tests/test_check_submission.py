"""scripts/check_submission.py on small fixture repositories: a clean one passes, each kind of problem fails its check."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from pitchside.agents.bundle import LIVE_LABEL, OFFLINE_LABEL
from pitchside.teams import build_team

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_submission", ROOT / "scripts" / "check_submission.py")
cs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cs  # dataclasses look the module up while the script runs
_spec.loader.exec_module(cs)

README = """# Demo

The public demo replays runs recorded by the live pipeline on GPT-6 Luna.
The MCP server is deployed on Azure Container Apps.

See [the pitch](docs/PITCH.md), ![a shot](docs/img/shot.png), [a section](#demo) and [a site](https://example.org).
Application Insights is planned, not deployed.

```
[not a link](nowhere.md) {{ in code is still a placeholder }}
```
"""
PITCH = "# Pitch\n\nAurora Rovers v Ironvale Athletic, invented clubs. Back to the [README](../README.md).\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root: Path, *, mode: str = "foundry") -> Path:
    """A repository with one published run that passes every check (for mode="foundry")."""
    live = mode == "foundry"
    label = LIVE_LABEL if live else OFFLINE_LABEL
    write(root / "README.md", README.replace("{{ in code is still a placeholder }}", ""))
    write(root / "docs" / "PITCH.md", PITCH)
    (root / "docs" / "img").mkdir(parents=True)
    (root / "docs" / "img" / "shot.png").write_bytes(b"\x89PNG")
    write(root / "web" / "index.html", "<title>Pitchside</title><p>Synthetic match intelligence</p>")
    write(root / "web" / "js" / "i18n.js", 'export const T = { broadcast: "Broadcast", spurs: "spurs on" };\n')
    home, away = build_team("aur", "home"), build_team("irv", "away")
    team = lambda t: {"team_id": t.team_id, "name": t.name, "short_name": t.short_name}  # noqa: E731
    entry = {"slug": "comeback", "title": "Comeback", "description": "A comeback.", "home": team(home),
             "away": team(away), "llm_mode": mode, "label": label, "faults_injected": False, "faults_fired": 0}
    write(root / "web" / "data" / "index.json", json.dumps({"runs": [entry]}))
    manifest = {"label": label, "home": {"name": home.name}, "away": {"name": away.name},
                "featured_players": [{"player_id": "aur_09", "name": home.players[9].name}],
                "pipeline": {"llm_mode": mode, "interpreter_tools": "mcp" if live else "local",
                             "mcp_server": "https://mcp.example.test/mcp" if live else None},
                "faults": {"injected": False, "fired": []}}
    run = root / "web" / "data" / "comeback"
    write(run / "manifest.json", json.dumps(manifest))
    write(run / "overlay.json", json.dumps({"source": {"llm_mode": mode, "label": label, "faults_injected": False},
                                            "overlays": [{"texts": {"casual": {"en": {"text": "What a goal."}}}}]}))
    start = {"type": "match_start", "home": home.model_dump(mode="json"), "away": away.model_dump(mode="json")}
    write(run / "events.jsonl", json.dumps(start) + "\n")
    return root


def edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


class CheckSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self._tmp.name) / "repo")
        self.run = self.root / "web" / "data" / "comeback"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def results(self) -> dict[str, "cs.Result"]:
        return {r.name: r for r in cs.run_checks(self.root)}

    def assertOnlyFails(self, *names: str) -> dict:
        results = self.results()
        self.assertEqual(sorted(n for n, r in results.items() if not r.ok), sorted(names),
                         {n: r.details for n, r in results.items()})
        return results

    def test_a_clean_repository_passes_every_check_and_exits_0(self) -> None:
        self.assertOnlyFails()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cs.main(["--root", str(self.root)]), 0)
        self.assertEqual(out.getvalue().count("PASS  "), len(cs.CHECKS))
        self.assertIn("ready to tag submission-v1", out.getvalue())

    def test_a_failure_exits_1_and_says_why(self) -> None:
        write(self.root / "docs" / "PITCH.md", PITCH + "Video: {{video_url}}\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cs.main(["--root", str(self.root)]), 1)
        self.assertIn("FAIL  placeholders", out.getvalue())
        self.assertIn("docs/PITCH.md:4: Video: {{video_url}}", out.getvalue())
        self.assertIn("1 of 6 checks failed", out.getvalue())

    def test_placeholders_and_pending_markers_fail_anywhere_in_the_two_files(self) -> None:
        write(self.root / "README.md", README)  # {{ inside a code block counts too
        self.assertOnlyFails("placeholders")
        write(self.root / "README.md", "# Demo\n\n{{PENDING deploy: on Azure | until then: soon}}\n")
        self.assertOnlyFails("placeholders")

    def test_offline_published_runs_fail_and_so_do_the_readme_claims_of_live_runs(self) -> None:
        offline = make_repo(Path(self._tmp.name) / "offline", mode="fake")
        results = {r.name: r for r in cs.run_checks(offline)}
        self.assertEqual(sorted(n for n, r in results.items() if not r.ok), ["README claims", "published runs"])
        self.assertIn("comeback: llm_mode 'fake'", results["published runs"].details)
        self.assertTrue(any("README.md:3:" in d for d in results["README claims"].details))

    def test_an_mcp_deployment_claim_needs_runs_recorded_through_a_deployed_server(self) -> None:
        edit_json(self.run / "manifest.json", lambda m: m["pipeline"].update(mcp_server=None, interpreter_tools="local"))
        results = self.assertOnlyFails("README claims")
        self.assertIn("README.md:4:", results["README claims"].details[0])
        self.assertIn("MCP server", results["README claims"].details[0])

    def test_naming_the_working_model_is_a_live_claim(self) -> None:
        offline = make_repo(Path(self._tmp.name) / "offline", mode="fake")
        write(offline / "README.md", "# Demo\n\nThe recorded runs were written by gpt-5-mini.\n")
        results = {r.name: r for r in cs.run_checks(offline)}
        self.assertFalse(results["README claims"].ok)
        self.assertIn("README.md:3:", results["README claims"].details[0])

    def test_negated_wrapped_and_quoted_mentions_are_not_claims(self) -> None:
        offline = make_repo(Path(self._tmp.name) / "offline", mode="fake")
        write(offline / "README.md", "\n".join([
            "# Demo", "",
            "The MCP server on Azure Container Apps is",
            "not deployed yet.",
            'Badges say "Recorded run generated by the live pipeline" for a recorded run.',
            "| Deploy | Static Web Apps | **Planned** |",
            "## Live runs on GPT-6 Luna", "",
        ]))
        results = {r.name: r for r in cs.run_checks(offline)}
        self.assertTrue(results["README claims"].ok, results["README claims"].details)

    def fresh(self, name: str) -> None:
        self.root = make_repo(Path(self._tmp.name) / name)
        self.run = self.root / "web" / "data" / "comeback"

    def test_run_labels_must_agree_between_index_manifest_and_overlay(self) -> None:
        edit_json(self.run / "manifest.json", lambda m: m["faults"].update(injected=True, fired=[{"kind": "timeout"}]))
        results = self.assertOnlyFails("run labels")
        self.assertTrue(any("faults were injected but the index does not say so" in d
                            for d in results["run labels"].details))

        self.fresh("offline-label")
        edit_json(self.run / "manifest.json", lambda m: m.update(label=OFFLINE_LABEL))
        results = self.assertOnlyFails("run labels")
        self.assertTrue(any("says offline" in d for d in results["run labels"].details))

        self.fresh("overlay")
        edit_json(self.run / "overlay.json", lambda o: o["source"].update(faults_injected=True))
        results = self.assertOnlyFails("run labels")
        self.assertTrue(any("overlay.json" in d for d in results["run labels"].details))

    def test_an_unlisted_run_directory_fails(self) -> None:
        (self.root / "web" / "data" / "stray").mkdir()
        results = self.assertOnlyFails("run labels")
        self.assertIn("stray: a run directory the index does not list", results["run labels"].details)

    def test_relative_links_must_resolve_inside_the_repository(self) -> None:
        write(self.root / "README.md", README.replace("{{ in code is still a placeholder }}", "")
              + "\n[gone](docs/missing.md) and [outside](../../outside.md) and [anchored](docs/PITCH.md#pitch)\n")
        results = self.assertOnlyFails("links")
        self.assertEqual(len(results["links"].details), 2)
        self.assertTrue(results["links"].details[0].endswith("docs/missing.md"))

    def test_league_and_real_names_fail_in_the_pitch_and_ui_strings(self) -> None:
        write(self.root / "docs" / "PITCH.md", PITCH + "Built for the Premier League.\n")
        write(self.root / "web" / "js" / "i18n.js", 'export const T = { team: "Arsenal" };\n')
        edit_json(self.run / "overlay.json",
                  lambda o: o["overlays"][0]["texts"]["casual"]["en"].update(text="A finish like Timo Werner."))
        results = self.assertOnlyFails("names")
        details = "\n".join(results["names"].details)
        self.assertIn('docs/PITCH.md:4: "Premier League"', details)
        self.assertIn("web/js/i18n.js:1: real name 'Arsenal'", details)
        self.assertIn("real name 'Timo Werner'", details)

    def test_every_published_name_must_be_an_invented_one(self) -> None:
        edit_json(self.run / "manifest.json", lambda m: m["featured_players"][0].update(name="John Smith"))
        results = self.assertOnlyFails("names")
        self.assertIn("comeback: 'John Smith' is not an invented club or player", results["names"].details)

    def test_the_invented_squads_contain_no_listed_real_name(self) -> None:
        hits = sorted(n for n in cs._invented_names() if cs.REAL_NAMES.search(n))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
