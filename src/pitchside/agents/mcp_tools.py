"""The interpreter's stats tools over MCP (``PITCHSIDE_TOOLS=mcp``).

Two ways to reach the server, both Streamable HTTP, both through Agent Framework's own ``MCPStreamableHTTPTool``:

* **In-process (default):** the pitchside MCP server runs inside the pipeline process and the tool talks to its ASGI
  app through an in-memory HTTP transport. It is the real server and the real transport (no sockets, no
  subprocess), loaded with the match being run, so offline runs and tests exercise exactly the code a deployment
  runs.
* **Remote (``PITCHSIDE_MCP_URL``):** a deployed ``pitchside mcp`` server, e.g. on Container Apps (milestone 6). It
  must hold the same match; generated matches are fully determined by seed and storyline, so it does.

Stdio is for external MCP clients (``uv run pitchside mcp``); the pipeline does not spawn subprocesses.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Optional, Sequence
from urllib.parse import urlsplit

import httpx
from agent_framework import MCPStreamableHTTPTool

from ..mcp_server import build_server
from ..schema import Event
from ..stats.timeline import MatchStore

# What the interpreter may use: every stats tool (list_matches is for external clients; the host pins the match).
INTERPRETER_TOOLS = ("match_totals", "recent_window", "key_moments", "evidence_lookup", "momentum",
                     "control_vs_chaos", "possession", "field_tilt", "pressure", "passing", "shots_and_xg")
IN_PROCESS_HOST = "pitchside-mcp.internal"
REQUEST_TIMEOUT_S = 30


class McpTools:
    """The Agent Framework MCP tool the interpreter uses, plus the server behind it when it runs in-process."""

    def __init__(self, url: Optional[str] = None, *, http_client: Optional[httpx.AsyncClient] = None,
                 allowed_tools: Sequence[str] = INTERPRETER_TOOLS) -> None:
        self.url = url
        self.tool_names = tuple(allowed_tools)
        self.store: Optional[MatchStore] = None
        self._stack = AsyncExitStack()
        self._opened = False
        self._owned_client: Optional[httpx.AsyncClient] = None
        if url is None:
            self.store = MatchStore()
            server = build_server(self.store, stateless_http=True, json_response=True, allowed_hosts=[IN_PROCESS_HOST])
            self._server = server
            http_client = self._owned_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server.streamable_http_app()), base_url=f"http://{IN_PROCESS_HOST}")
            endpoint = f"http://{IN_PROCESS_HOST}/mcp"
        else:
            endpoint = url
        self.tool = MCPStreamableHTTPTool(
            name="stats", url=endpoint, http_client=http_client, load_prompts=False,
            allowed_tools=list(self.tool_names), approval_mode="never_require", request_timeout=REQUEST_TIMEOUT_S,
            description="Read-only football statistics for the match, already limited to this moment.")

    @property
    def location(self) -> str:
        """Where the server is, for the run manifest (scheme and host only)."""
        if self.url is None:
            return "in-process"
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def register_match(self, events: Sequence[Event]) -> None:
        """Give the in-process server the match (a remote server already holds it)."""
        if self.store is not None:
            self.store.add(list(events))

    async def open(self) -> None:
        """Start the in-process server (if any) and connect. Call and close from the same task."""
        if self._opened:
            return
        self._opened = True
        if self.url is None:
            await self._stack.enter_async_context(self._server.session_manager.run())
        await self.tool.connect()

    async def aclose(self) -> None:
        if not self._opened:
            return
        self._opened = False
        try:
            await self.tool.close()
        finally:
            if self._owned_client is not None:
                await self._owned_client.aclose()
            await self._stack.aclose()
