"""Tests for the schema, replay timing, JSONL round-trip and the CLI."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from pitchside.cli import main
from pitchside.generator import generate_match
from pitchside.jsonl import read_events, write_events
from pitchside.replay import replay
from pitchside.schema import PassEvent, json_schema, parse_event, to_json_line


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.events = generate_match(seed=3, storyline="siege").events

    def test_every_event_round_trips(self) -> None:
        for event in self.events:
            self.assertEqual(parse_event(to_json_line(event)), event)

    def test_rejects_unknown_event_type(self) -> None:
        with self.assertRaises(ValidationError):
            parse_event('{"type": "dive", "event_id": 1, "match_id": "x", "period": 1, "clock_s": 3}')

    def test_rejects_off_pitch_coordinates_and_extra_fields(self) -> None:
        pass_event = next(e for e in self.events if isinstance(e, PassEvent))
        payload = json.loads(to_json_line(pass_event))

        off_pitch = {**payload, "start": {"x": 200, "y": 10}}
        with self.assertRaises(ValidationError):
            parse_event(json.dumps(off_pitch))

        extra = {**payload, "mood": "angry"}
        with self.assertRaises(ValidationError):
            parse_event(json.dumps(extra))

    def test_json_schema_lists_all_event_types(self) -> None:
        text = json.dumps(json_schema())
        for name in ("match_start", "pass", "shot", "tackle", "possession_change", "pressure", "match_end"):
            self.assertIn(f'"{name}"', text)


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.events = generate_match(seed=5, storyline="balanced").events

    def test_replay_scales_time_by_speed(self) -> None:
        clock = {"t": 0.0}
        delivered = []
        count = replay(
            self.events,
            delivered.append,
            speed=60,
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            now=lambda: clock["t"],
        )
        duration = self.events[-1].clock_s - self.events[0].clock_s
        self.assertEqual(count, len(self.events))
        self.assertEqual(delivered, self.events)
        self.assertAlmostEqual(clock["t"], duration / 60, places=3)

    def test_no_delay_mode_never_sleeps(self) -> None:
        def fail(_: float) -> None:
            raise AssertionError("slept in no-delay mode")

        self.assertEqual(replay(self.events, lambda e: None, speed=None, sleep=fail), len(self.events))

    def test_rejects_non_positive_speed(self) -> None:
        with self.assertRaises(ValueError):
            replay(self.events, lambda e: None, speed=0)


class CliTests(unittest.TestCase):
    def _run(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(argv))
        return code, buffer.getvalue()

    def test_generate_summary_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._run("generate", "--seed", "4", "--storyline", "siege", "--out", tmp)
            self.assertEqual(code, 0, out)
            files = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(files, ["aur_irv_siege_0004.jsonl", "aur_irv_siege_0004.key.json"])

            path = Path(tmp) / "aur_irv_siege_0004.jsonl"
            key = json.loads((Path(tmp) / "aur_irv_siege_0004.key.json").read_text())
            self.assertEqual(key["storyline"], "siege")
            self.assertEqual(len(key["scripted_goals"]), 3)

            self.assertEqual(self._run("summary", str(path))[0], 0)

            code, out = self._run("replay", str(path), "--instant")
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), len(list(read_events(path))))

    def test_jsonl_round_trip_through_files(self) -> None:
        events = generate_match(seed=9, storyline="balanced").events
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            self.assertEqual(write_events(events, path), len(events))
            self.assertEqual(list(read_events(path)), events)

    def test_schema_and_storylines_commands(self) -> None:
        code, out = self._run("schema")
        self.assertEqual(code, 0)
        self.assertIn("oneOf", out)
        code, out = self._run("storylines")
        self.assertEqual(code, 0)
        self.assertIn("comeback", out)

    def test_findings_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._run("generate", "--seed", "4", "--storyline", "siege", "--out", tmp)
            path = str(Path(tmp) / "aur_irv_siege_0004.jsonl")
            code, out = self._run("findings", path)
            self.assertEqual(code, 0)
            self.assertEqual(out.count(" goal "), 3)
            code, out = self._run("findings", path, "--json")
            first = json.loads(out.splitlines()[0])
            self.assertEqual(first["match_id"], "aur_irv_siege_0004")

    def test_eval_stats_command(self) -> None:
        code, out = self._run("eval-stats", "--seeds", "1-2", "--storylines", "balanced", "end_to_end")
        self.assertIn(code, (0, 1))  # 1 = a bar failed; the table is printed either way
        self.assertIn("Stats engine vs answer keys (seeds 1-2)", out)
        self.assertIn("end_to_end", out)
        self.assertIn("Bars:", out)

    def test_bad_seed_range_is_a_clean_error(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["eval-stats", "--seeds", "9-3"]), 2)
        self.assertIn("bad seed range", err.getvalue())

    def test_missing_file_is_a_clean_error(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["summary", "/nonexistent/match.jsonl"]), 2)
        self.assertIn("error:", err.getvalue())

    def test_invalid_line_reports_its_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text('{"type": "pass"}\n', encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                list(read_events(path))
            self.assertIn("bad.jsonl:1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
