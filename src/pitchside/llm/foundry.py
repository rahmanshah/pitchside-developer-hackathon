"""Live chat clients for the model deployments in a Microsoft Foundry project, authenticated with Entra ID.

``AIProjectClient.get_openai_client`` returns an ``AsyncOpenAI`` client on ``<project endpoint>/openai/v1``
that fetches and refreshes Entra tokens (scope ``https://ai.azure.com/.default``) itself. We turn its own
retries off and set its timeout, because the guard does retries and must see every attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from openai import AsyncOpenAI

from .config import LLMSettings

Api = Literal["responses", "chat_completions"]


@dataclass
class FoundryConnection:
    """One credential, project client and OpenAI client shared by every role. Close it when done."""

    project: AIProjectClient
    openai: AsyncOpenAI
    credential: Any
    owns_credential: bool

    async def aclose(self) -> None:
        await self.openai.close()
        await self.project.close()
        if self.owns_credential:
            await self.credential.close()


def connect(settings: LLMSettings, credential: Optional[Any] = None) -> FoundryConnection:
    """Open a connection to the Foundry project. No network traffic happens until the first call."""
    settings.require_live()
    owns = credential is None
    credential = credential or DefaultAzureCredential()
    project = AIProjectClient(endpoint=settings.foundry_endpoint, credential=credential)
    client = project.get_openai_client(max_retries=0, timeout=settings.timeout_s)
    return FoundryConnection(project, client, credential, owns)


def chat_client(
    connection: FoundryConnection, deployment: str, *, api: Api = "responses", middleware: Sequence[Any] = ()
):
    """An Agent Framework chat client for one deployment: Responses API, or Chat Completions as a fallback."""
    cls = OpenAIChatClient if api == "responses" else OpenAIChatCompletionClient
    return cls(model=deployment, async_client=connection.openai, middleware=list(middleware))
