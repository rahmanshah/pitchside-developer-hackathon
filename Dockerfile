# syntax=docker/dockerfile:1
# The pitchside MCP server (read-only stats tools over Streamable HTTP), as deployed on Azure Container Apps.
# Pattern from the uv Docker guide (docs.astral.sh/uv/guides/integration/docker): pinned uv, locked dependencies in
# their own layer, non-editable install copied into a slim runtime stage that runs as a non-root user.
#   docker build -t pitchside-mcp .
#   docker run --rm -p 8000:8000 pitchside-mcp            # then: curl http://127.0.0.1:8000/healthz

FROM python:3.12-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first (cached until uv.lock changes). --frozen: use the lock as is, never re-resolve.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-editable

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable

FROM python:3.12-slim-trixie
RUN useradd --create-home --uid 10001 app
COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER 10001
WORKDIR /home/app
EXPOSE 8000
# Container Apps passes its own arguments (allowed host, matches, rate limit): see infra/main.bicep.
ENTRYPOINT ["pitchside"]
CMD ["mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--stateless", "--json-response", \
     "--allowed-host", "127.0.0.1", "--allowed-host", "localhost", "--generate", "7:comeback:aur:irv"]
