# Nautobot MCP Server
# Multi-stage build: dependencies and the embedding model are baked into the image.

FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.21 /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first (cached separately from project code)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Install the project itself (non-editable: the venv is self-contained)
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Bake the embedding model (~80 MB) into the image so a fresh container does not need
# internet access to chroma's model bucket on first start.
ENV NAUTOBOT_MCP_DATA_DIR=/data
RUN mkdir -p /data && /app/.venv/bin/python -c \
    "from nautobot_mcp.utils.embedding import LocalEmbeddingFunction; LocalEmbeddingFunction()"

# Production stage
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash mcpuser

WORKDIR /app

COPY --from=builder --chown=mcpuser:mcpuser /app/.venv /app/.venv
# Data dir pre-seeded with the embedding model. A named volume mounted at /data starts
# from this content.
COPY --from=builder --chown=mcpuser:mcpuser /data /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOKENIZERS_PARALLELISM=false \
    NAUTOBOT_MCP_DATA_DIR=/data \
    # MCP configuration (override at runtime). HTTP is the sensible default for a
    # container; set MCP_TRANSPORT=stdio for `docker run -i` usage.
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

USER mcpuser

EXPOSE 8000

# Liveness only: /health answers as soon as the server is up, while indexes are still
# being built in the background. Use /ready (503 until indexed) for readiness gating.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MCP_PORT','8000'), timeout=4)" || exit 1

# No default CMD: transport/host/port come from MCP_* env vars (an explicit `--mode` in
# CMD would silently override MCP_TRANSPORT).
ENTRYPOINT ["nautobot-mcp"]
