"""Tests for model-client settings and the .env.example contract."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from pitchside.llm import ENV_VARS, ConfigError, Role, load_settings

ROOT = Path(__file__).resolve().parents[1]
LIVE = {
    "PITCHSIDE_LLM": "foundry",
    "FOUNDRY_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/pitchside",
    "MODEL_INTERPRETER": "luna-i",
    "MODEL_EXPLAINER": "luna-e",
    "MODEL_VERIFIER": "luna-v",
    "MODEL_PERSONALIZER": "luna-p",
}


class SettingsTests(unittest.TestCase):
    def test_defaults_are_offline_and_capped(self) -> None:
        s = load_settings({})
        self.assertEqual(s.mode, "fake")
        self.assertEqual((s.match_budget_usd, s.match_max_calls, s.total_budget_usd), (0.25, 200, 10.0))
        self.assertEqual(s.max_concurrency, 4)
        self.assertEqual((s.price_model, s.reasoning_effort), ("gpt-5-mini", "low"))

    def test_live_mode_reads_one_deployment_per_role(self) -> None:
        s = load_settings(LIVE)
        self.assertEqual(s.deployment(Role.VERIFIER), "luna-v")
        self.assertEqual({s.deployment(r) for r in Role}, {"luna-i", "luna-e", "luna-v", "luna-p"})

    def test_live_mode_lists_every_missing_variable(self) -> None:
        env = dict(LIVE, FOUNDRY_ENDPOINT="", MODEL_VERIFIER="  ")
        with self.assertRaises(ConfigError) as ctx:
            load_settings(env)
        self.assertIn("FOUNDRY_ENDPOINT", str(ctx.exception))
        self.assertIn("MODEL_VERIFIER", str(ctx.exception))

    def test_live_mode_wants_the_project_endpoint(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(dict(LIVE, FOUNDRY_ENDPOINT="https://acct.openai.azure.com/"))

    def test_bad_numbers_name_the_variable(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_settings({"PITCHSIDE_MATCH_BUDGET_USD": "-1"})
        self.assertIn("PITCHSIDE_MATCH_BUDGET_USD", str(ctx.exception))
        with self.assertRaises(ConfigError):
            load_settings({"PITCHSIDE_LLM": "openai"})

    def test_gpt_5_mini_accepts_only_its_own_reasoning_efforts(self) -> None:
        for effort in ("minimal", "low", "medium", "high", "off"):
            s = load_settings(dict(LIVE, PITCHSIDE_PRICE_MODEL="gpt-5-mini", PITCHSIDE_REASONING_EFFORT=effort))
            self.assertEqual(s.reasoning_effort, effort)
        for effort in ("none", "xhigh", "max"):
            with self.assertRaises(ConfigError) as ctx:
                load_settings({"PITCHSIDE_PRICE_MODEL": "gpt-5-mini", "PITCHSIDE_REASONING_EFFORT": effort})
            self.assertIn("minimal, low, medium, high", str(ctx.exception))

    def test_unlisted_models_keep_every_effort(self) -> None:
        s = load_settings({"PITCHSIDE_PRICE_MODEL": "gpt-6-luna", "PITCHSIDE_REASONING_EFFORT": "none"})
        self.assertEqual(s.reasoning_effort, "none")

    def test_fake_mode_without_a_deployment_name_fails_only_when_asked(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings({}).deployment(Role.EXPLAINER)


class EnvExampleTests(unittest.TestCase):
    def test_env_example_lists_exactly_the_variables_the_code_reads(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))
        self.assertEqual(listed, set(ENV_VARS))

    def test_env_example_holds_no_secrets(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)^[A-Z_]*(KEY|SECRET|TOKEN|PASSWORD)[A-Z_]*=", "keys do not belong in config")

    def test_env_example_parses_as_valid_settings(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        env = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, flags=re.MULTILINE))
        self.assertEqual(load_settings(env).mode, "fake")


if __name__ == "__main__":
    unittest.main()
