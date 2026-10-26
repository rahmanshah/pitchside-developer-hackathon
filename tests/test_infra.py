"""infra/: the Bicep template stays in step with the code (matches, CLI flags) and with the agreed limits.

If the Bicep CLI is installed, the template is also compiled and linted with zero warnings allowed (CI installs
it in its own job; locally `winget install -e --id Microsoft.Bicep`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pitchside.cli import build_parser
from pitchside.mcp_server import load_store

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
BICEP = (INFRA / "main.bicep").read_text(encoding="utf-8")


def array_param(name: str) -> list[str]:
    block = re.search(rf"param {name} array = \[(.*?)\n\]", BICEP, re.S)
    assert block, f"param {name} not found"
    return re.findall(r"'([^']+)'", block.group(1))


def container_args() -> list[str]:
    """The literal argument list of the MCP container, with Bicep expressions replaced by sample values."""
    block = re.search(r"args: concat\(\s*\[(.*?)\]\s*,\s*generateArgs", BICEP, re.S)
    assert block, "container args not found"
    values = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        literal = re.fullmatch(r"'([^'$]*)'", line)
        values.append(literal.group(1) if literal else "SAMPLE" if not line.startswith("string(") else "8000")
    return values


class TemplateTests(unittest.TestCase):
    def test_the_deployed_server_holds_every_published_run(self) -> None:
        served = set(load_store(generate=array_param("mcpMatches")).ids())
        published = {json.loads(p.read_text(encoding="utf-8"))["match_id"]
                     for p in (ROOT / "web" / "data").glob("*/manifest.json")}
        self.assertTrue(published)
        self.assertLessEqual(published, served, "add the missing matches to mcpMatches in infra/main.bicep")

    def test_the_container_arguments_are_valid_pitchside_cli_arguments(self) -> None:
        args = container_args() + [x for m in array_param("mcpMatches") for x in ("--generate", m)]
        self.assertEqual(args[0], "mcp")
        parsed = build_parser().parse_args(args)
        self.assertEqual((parsed.transport, parsed.stateless, parsed.json_response, parsed.trust_forwarded),
                         ("streamable-http", True, True, True))
        self.assertFalse(parsed.no_rate_limit)
        self.assertEqual(parsed.host, "0.0.0.0")
        self.assertEqual(len(parsed.allowed_host), 1)

    def test_sizing_scaling_and_ingress_are_as_agreed(self) -> None:
        self.assertIn("cpu: json('0.25')", BICEP)
        self.assertIn("memory: '0.5Gi'", BICEP)
        self.assertRegex(BICEP, r"minReplicas: 0\b")
        self.assertRegex(BICEP, r"maxReplicas: 1\b")
        self.assertIn("ipSecurityRestrictions", BICEP)
        self.assertIn("action: 'Allow'", BICEP)
        self.assertIn("allowInsecure: false", BICEP)
        self.assertIn("path: '/healthz'", BICEP)
        self.assertIn("name: 'Free'", BICEP)  # Static Web Apps Free plan
        self.assertIn("Microsoft.Consumption/budgets", BICEP)
        self.assertIn("expires: '2026-11-10'", BICEP)

    def test_no_managed_identity_or_role_assignment_is_deployed(self) -> None:
        """Decided: nothing here calls a model, so no identity and no Foundry role (docs/AZURE.md, planned)."""
        self.assertNotIn("Microsoft.ManagedIdentity", BICEP)
        self.assertNotIn("roleAssignments", BICEP)
        self.assertNotRegex(BICEP, r"\n\s*identity:")

    def test_personal_values_come_from_the_environment_not_from_git(self) -> None:
        params = (INFRA / "main.bicepparam").read_text(encoding="utf-8")
        for name in ("mcpImage", "allowedIpRanges", "budgetContactEmails"):
            self.assertRegex(params, rf"param {name} = .*readEnvironmentVariable\(")
        self.assertNotRegex(params, r"@[A-Za-z0-9-]+\.[a-z]{2,}")  # no e-mail address
        self.assertNotRegex(params, r"\b\d{1,3}(\.\d{1,3}){3}\b")  # no IP address

    def test_the_bicep_cli_compiles_and_lints_without_warnings(self) -> None:
        bicep = shutil.which("bicep")
        if bicep is None:
            self.skipTest("Bicep CLI not installed (CI runs this in its own job)")
        for args in (["build", str(INFRA / "main.bicep"), "--stdout"], ["lint", str(INFRA / "main.bicep")]):
            out = subprocess.run([bicep, *args], capture_output=True, text=True, timeout=180)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotRegex(out.stderr, r"(Warning|Error) BCP|Warning [a-z-]+:", out.stderr)


class ScriptTests(unittest.TestCase):
    def test_teardown_refuses_to_run_before_the_judging_period_ends(self) -> None:
        script = (INFRA / "teardown.ps1").read_text(encoding="utf-8")
        # An absolute instant with its offset, compared against UtcNow, never local midnight.
        m = re.search(r"\$TeardownFrom = \[DateTimeOffset\]::new\((\d+), (\d+), (\d+), (\d+), (\d+), (\d+), "
                      r"\[TimeSpan\]::FromHours\((-?\d+)\)\)", script)
        self.assertIsNotNone(m, "the guard must be a DateTimeOffset with an explicit offset")
        *parts, hours = map(int, m.groups())
        guard = datetime(*parts, tzinfo=timezone(timedelta(hours=hours)))
        judging_ends = datetime(2026, 11, 10, 23, 59, tzinfo=timezone(timedelta(hours=-8)))  # PST
        self.assertEqual(guard, datetime(2026, 11, 11, 12, 0, tzinfo=timezone(timedelta(hours=2))))
        self.assertGreaterEqual(guard - judging_ends, timedelta(hours=2))
        self.assertIn("[DateTimeOffset]::UtcNow -lt $TeardownFrom -and -not $Force", script)
        self.assertNotIn("[datetime]", script)
        self.assertNotIn("Get-Date", script)

    def test_deploy_is_staged_and_resumable(self) -> None:
        script = (INFRA / "deploy.ps1").read_text(encoding="utf-8")
        for stage in ("preflight", "providers", "group", "deploy", "verify"):
            self.assertIn(f'Test-Stage "{stage}"', script)
        self.assertIn("-FromStage", script)
        self.assertIn('"what-if"', script)
        self.assertNotIn("group delete", script)


if __name__ == "__main__":
    unittest.main()
