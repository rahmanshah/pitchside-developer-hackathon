"""Shared pytest fixtures. The suite is hermetic: it never reads the developer's real ``.env``, never reaches the
deployed MCP server, and model-client tests never reach the network at all.

Why: ``load_settings()`` without a mapping calls ``load_dotenv('.env', override=False)``, which copies the real
``.env`` into ``os.environ`` for the rest of the session. One CLI test was enough to leak ``PITCHSIDE_LLM=foundry``,
``PITCHSIDE_TOOLS=mcp`` and the deployed ``PITCHSIDE_MCP_URL`` into every later test.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from _hermetic import BLOCKED_ATTEMPTS, REAL_ENV_FILE, refuse_deployed
from pitchside.llm.config import ENV_VARS

_LOCAL = {"127.0.0.1", "::1", "localhost"}


def _host(address) -> str:
    return address[0] if isinstance(address, tuple) else str(address)


def _refuse_deployed(address) -> None:
    refuse_deployed(_host(address))


@pytest.fixture(autouse=True, scope="session")
def _hermetic_environment(tmp_path_factory):
    """For the whole session: no settings variable from the shell, fake model client and local tools, usage logged to
    a temporary file, the real ``.env`` never loaded, and no connection to the deployed MCP server."""
    import dotenv

    real_load_dotenv = dotenv.load_dotenv
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def load_dotenv(dotenv_path=None, *args, **kwargs):
        # A path-less call searches upwards for a .env and would find the real one, so it is refused too.
        if dotenv_path is None or Path(dotenv_path).resolve() == REAL_ENV_FILE:
            return False
        return real_load_dotenv(dotenv_path, *args, **kwargs)

    def connect(self, address):
        _refuse_deployed(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _refuse_deployed(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        _refuse_deployed((host,))
        return real_getaddrinfo(host, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        mp.setenv("PITCHSIDE_LLM", "fake")
        mp.setenv("PITCHSIDE_TOOLS", "local")
        mp.setenv("PITCHSIDE_USAGE_LOG", str(tmp_path_factory.mktemp("usage") / "llm_usage.jsonl"))
        mp.setattr(dotenv, "load_dotenv", load_dotenv)
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        yield


@pytest.fixture(autouse=True)
def _fail_on_deployed_server_attempts():
    """Fail the test if it tried to reach the deployed server, even when a library caught the guard's error."""
    BLOCKED_ATTEMPTS.clear()
    yield
    if BLOCKED_ATTEMPTS:
        hosts = ", ".join(sorted(set(BLOCKED_ATTEMPTS)))
        BLOCKED_ATTEMPTS.clear()
        pytest.fail(f"the test tried to reach the deployed MCP server: {hosts}")


@pytest.fixture(autouse=True)
def _no_network_in_llm_tests(request, monkeypatch):
    """Block outbound connections and DNS in tests/test_llm_*.py (loopback stays open for asyncio)."""
    if not request.node.fspath.basename.startswith("test_llm_"):
        yield
        return
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def guard(address) -> None:
        if _host(address) not in _LOCAL:
            raise RuntimeError(f"network access in an offline test: {address!r}")

    def connect(self, address):
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        guard((host,))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
