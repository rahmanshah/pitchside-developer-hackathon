"""Price table for cost accounting. Measured token counts times these prices; no estimates elsewhere.

Prices are USD per 1M tokens. Update ``PRICE_TABLE_VERSION`` whenever a price changes, so every usage record
says which table priced it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ConfigError


@dataclass(frozen=True)
class Price:
    input_per_m: float
    cached_input_per_m: float
    output_per_m: float
    source: str


PRICE_TABLE_VERSION = "2026-10-18"

PRICE_TABLE: dict[str, Price] = {
    # Short context, Global Standard deployment. Source as recorded in CLAUDE.md (Oct 2026).
    "gpt-6-luna": Price(
        input_per_m=0.10,
        cached_input_per_m=0.01,
        output_per_m=0.50,
        source="Microsoft Foundry blog and Azure OpenAI pricing page, Oct 2026 (Global Standard, short context)",
    ),
}


def price_for(name: str) -> Price:
    """Look up a price; unknown models fail closed so we never log a call as free."""
    try:
        return PRICE_TABLE[name]
    except KeyError:
        known = ", ".join(sorted(PRICE_TABLE))
        raise ConfigError(f"no price for model {name!r} (PITCHSIDE_PRICE_MODEL); known: {known}") from None


def cost_usd(price: Price, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Cost of one call. ``input_tokens`` includes the cached ones; reasoning tokens are part of the output count."""
    cached = min(max(cached_tokens, 0), max(input_tokens, 0))
    uncached = max(input_tokens, 0) - cached
    return (
        uncached * price.input_per_m + cached * price.cached_input_per_m + max(output_tokens, 0) * price.output_per_m
    ) / 1_000_000
