"""Shared pytest fixtures. Model-client tests must never reach the network."""

from __future__ import annotations

import socket

import pytest

_LOCAL = {"127.0.0.1", "::1", "localhost"}


def _host(address) -> str:
    return address[0] if isinstance(address, tuple) else str(address)


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
