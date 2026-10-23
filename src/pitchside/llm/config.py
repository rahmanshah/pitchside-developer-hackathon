"""Settings for the model client, read from the environment (and an optional ``.env`` file).

No keys live here or anywhere else: live calls authenticate with Microsoft Entra ID. Every variable the code
reads is listed in ``ENV_VARS`` and must appear in ``.env.example`` (a test checks this).
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Literal, Mapping, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Role(str, Enum):
    INTERPRETER = "interpreter"
    EXPLAINER = "explainer"
    VERIFIER = "verifier"
    PERSONALIZER = "personalizer"


ROLE_ENV = {
    Role.INTERPRETER: "MODEL_INTERPRETER",
    Role.EXPLAINER: "MODEL_EXPLAINER",
    Role.VERIFIER: "MODEL_VERIFIER",
    Role.PERSONALIZER: "MODEL_PERSONALIZER",
}

# env var -> settings field
ENV_VARS = {
    "PITCHSIDE_LLM": "mode",
    "FOUNDRY_ENDPOINT": "foundry_endpoint",
    "FOUNDRY_API": "foundry_api",
    **{env: f"model_{role.value}" for role, env in ROLE_ENV.items()},
    "PITCHSIDE_PRICE_MODEL": "price_model",
    "PITCHSIDE_MATCH_BUDGET_USD": "match_budget_usd",
    "PITCHSIDE_MATCH_MAX_CALLS": "match_max_calls",
    "PITCHSIDE_TOTAL_BUDGET_USD": "total_budget_usd",
    "PITCHSIDE_MAX_CONCURRENCY": "max_concurrency",
    "LLM_TIMEOUT_S": "timeout_s",
    "LLM_MAX_RETRIES": "max_retries",
    "PITCHSIDE_USAGE_LOG": "usage_log",
    "PITCHSIDE_REASONING_EFFORT": "reasoning_effort",
    **{f"PITCHSIDE_MAX_OUTPUT_{role.name}": f"max_output_{role.value}" for role in Role},
    "PITCHSIDE_LANGUAGES": "languages",
    "PITCHSIDE_TOOLS": "tools",
    "PITCHSIDE_MCP_URL": "mcp_url",
}

ReasoningEffort = Literal["off", "none", "minimal", "low", "medium", "high", "xhigh", "max"]

# Reasoning efforts a model accepts, keyed by price model, for models where we checked the docs. gpt-5-mini predates
# gpt-5.1, so it has no "none" (Microsoft Foundry reasoning docs, read 2026-10-23). Unlisted models are not checked.
REASONING_EFFORTS: dict[str, frozenset[str]] = {
    "gpt-5-mini": frozenset({"minimal", "low", "medium", "high"}),
}


class ConfigError(ValueError):
    """Settings are missing or invalid."""


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["fake", "foundry"] = Field(
        "fake", description="'fake' (offline, the default, never spends money) or 'foundry' (live calls)."
    )
    foundry_endpoint: Optional[str] = Field(None, description="Foundry *project* endpoint.")
    foundry_api: Literal["responses", "chat_completions"] = Field(
        "responses", description="Responses API (recommended) or Chat Completions, if the deployment lacks Responses."
    )
    model_interpreter: Optional[str] = None
    model_explainer: Optional[str] = None
    model_verifier: Optional[str] = None
    model_personalizer: Optional[str] = None
    price_model: str = "gpt-5-mini"
    match_budget_usd: float = Field(0.25, gt=0)
    match_max_calls: int = Field(200, gt=0)
    total_budget_usd: float = Field(10.0, gt=0)
    max_concurrency: int = Field(4, gt=0)
    timeout_s: float = Field(20.0, gt=0)
    max_retries: int = Field(3, ge=0)
    usage_log: Path = Path("runs/llm_usage.jsonl")
    reasoning_effort: ReasoningEffort = Field(
        "low", description="Responses API reasoning effort for every agent; 'off' sends no reasoning option."
    )
    # Output caps include reasoning tokens, so they sit well above the size of the JSON each role returns.
    max_output_interpreter: int = Field(1500, gt=0, le=32000)
    max_output_explainer: int = Field(3000, gt=0, le=32000)
    max_output_verifier: int = Field(1500, gt=0, le=32000)
    max_output_personalizer: int = Field(4000, gt=0, le=32000)
    languages: str = Field("en,es", pattern=r"^[a-z]{2}(,[a-z]{2})*$",
                           description="Comma-separated language codes; each needs a phrase table.")

    tools: Literal["local", "mcp"] = Field(
        "local", description="How the interpreter reaches the stats tools: 'local' function tools, or 'mcp'."
    )
    mcp_url: Optional[str] = Field(
        None, pattern=r"^https?://", description="Streamable HTTP endpoint of a deployed pitchside MCP server; "
                                                  "empty = an in-process MCP server.",
    )

    def max_output(self, role: Role) -> int:
        return getattr(self, f"max_output_{role.value}")

    @property
    def language_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.languages.split(",")))

    def deployment(self, role: Role) -> str:
        name = getattr(self, f"model_{role.value}")
        if not name:
            raise ConfigError(f"{ROLE_ENV[role]} is not set (the model deployment name for the {role.value} agent)")
        return name

    def check_reasoning_effort(self) -> None:
        """Fail at startup, not on the first live call, if the model does not accept the effort."""
        allowed = REASONING_EFFORTS.get(self.price_model)
        if allowed is not None and self.reasoning_effort != "off" and self.reasoning_effort not in allowed:
            raise ConfigError(
                f"PITCHSIDE_REASONING_EFFORT={self.reasoning_effort} is not accepted by {self.price_model}; "
                f"use off or one of: {', '.join(e for e in get_args(ReasoningEffort) if e in allowed)}"
            )

    def require_live(self) -> None:
        """Fail early, with every problem at once, if live mode is not fully configured."""
        missing = [] if self.foundry_endpoint else ["FOUNDRY_ENDPOINT"]
        missing += [ROLE_ENV[r] for r in Role if not getattr(self, f"model_{r.value}")]
        if missing:
            raise ConfigError(f"PITCHSIDE_LLM=foundry needs: {', '.join(missing)} (see .env.example)")
        if not self.foundry_endpoint.startswith("https://") or "/api/projects/" not in self.foundry_endpoint:
            raise ConfigError(
                "FOUNDRY_ENDPOINT must be the Foundry project endpoint, "
                "https://<account>.services.ai.azure.com/api/projects/<project>"
            )


def load_settings(env: Optional[Mapping[str, str]] = None, env_file: Optional[Path] = Path(".env")) -> LLMSettings:
    """Build settings from ``env`` (default: the process environment, after loading ``env_file`` if it exists).

    Real environment variables win over ``.env`` values. Empty strings count as unset.
    """
    if env is None:
        if env_file is not None and env_file.is_file():
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        env = os.environ
    values = {field: env[name].strip() for name, field in ENV_VARS.items() if env.get(name, "").strip()}
    for key in ("mode", "foundry_api", "reasoning_effort", "languages", "tools"):
        if key in values:
            values[key] = values[key].lower()
    if "languages" in values:
        values["languages"] = values["languages"].replace(" ", "")
    try:
        settings = LLMSettings(**values)
    except ValidationError as error:
        problems = "; ".join(
            f"{_env_name(str(e['loc'][0]))}: {e['msg']}" for e in error.errors()
        )
        raise ConfigError(f"invalid model settings: {problems}") from None
    settings.check_reasoning_effort()
    if settings.mode == "foundry":
        settings.require_live()
    return settings


def _env_name(field: str) -> str:
    return next((name for name, f in ENV_VARS.items() if f == field), field)
