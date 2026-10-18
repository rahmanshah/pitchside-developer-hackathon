"""Tests for the Finding contract."""

from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from pitchside.schema import Score
from pitchside.stats import Evidence, Finding, FindingKind, findings_json_schema


def _finding(**overrides) -> Finding:
    fields = dict(
        finding_id="m:siege:0",
        match_id="m",
        kind=FindingKind.SIEGE,
        status="start",
        team_id="aur",
        start_s=1080.0,
        end_s=1380.0,
        detected_at_s=1380.0,
        severity=0.7,
        score=Score(home=0, away=0),
        evidence=[
            Evidence(
                metric="field_tilt", team_id="aur", value=0.81, unit="pct",
                window_start_s=1080.0, window_end_s=1380.0, baseline=0.5,
            )
        ],
    )
    fields.update(overrides)
    return Finding(**fields)


class FindingModelTests(unittest.TestCase):
    def test_round_trips_through_json(self) -> None:
        finding = _finding()
        self.assertEqual(Finding.model_validate_json(finding.model_dump_json()), finding)

    def test_requires_evidence(self) -> None:
        with self.assertRaises(ValidationError):
            _finding(evidence=[])

    def test_rejects_unknown_kind_and_extra_fields(self) -> None:
        with self.assertRaises(ValidationError):
            _finding(kind="worldie")
        payload = json.loads(_finding().model_dump_json())
        payload["vibe"] = "great"
        with self.assertRaises(ValidationError):
            Finding.model_validate(payload)

    def test_severity_is_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            _finding(severity=1.5)

    def test_json_schema_lists_every_kind(self) -> None:
        text = json.dumps(findings_json_schema())
        for kind in FindingKind:
            self.assertIn(f'"{kind.value}"', text)


if __name__ == "__main__":
    unittest.main()
