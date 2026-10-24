"""State of the hermetic-test guard (used by conftest.py and test_hermetic.py; tests/e2e has its own conftest)."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
REAL_ENV_FILE = ROOT / ".env"
# A host the guard always blocks, so the guard itself can be tested without a deployed server configured.
SENTINEL_HOST = "deployed-mcp.pitchside.invalid"


def _deployed_hosts() -> frozenset[str]:
    """Hosts of the deployed MCP server, from the shell and the real ``.env`` (read for this one key only, without
    touching ``os.environ``), plus the sentinel."""
    urls = [os.environ.get("PITCHSIDE_MCP_URL", "")]
    if REAL_ENV_FILE.is_file():
        from dotenv import dotenv_values

        urls.append(dotenv_values(REAL_ENV_FILE).get("PITCHSIDE_MCP_URL") or "")
    hosts = {urlparse(u.strip()).hostname for u in urls if u.strip()}
    return frozenset(h.lower() for h in hosts if h) | {SENTINEL_HOST}


DEPLOYED_HOSTS = _deployed_hosts()
BLOCKED_ATTEMPTS: list[str] = []  # connections the guard refused during the current test


def refuse_deployed(host: str) -> None:
    host = host.lower()
    if host in DEPLOYED_HOSTS:
        BLOCKED_ATTEMPTS.append(host)
        raise RuntimeError(f"a test tried to reach the deployed MCP server ({host}); tests must use the in-process "
                           "server or local tools")
