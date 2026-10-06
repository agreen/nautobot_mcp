# Nautobot MCP Server Dockerfile
# Multi-stage build for optimal image size and security
# Optimized for uv package manager with cache mounts and intermediate layers

FROM python:3.11-slim AS builder

# Copy uv from official image (pinned version for reproducibility)
# Version 0.5.21 matches local development environment
COPY --from=ghcr.io/astral-sh/uv:0.5.21 /uv /uvx /bin/

# Install system dependencies for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set uv environment variables optimized for Docker
# UV_COMPILE_BYTECODE: Precompile Python bytecode for faster startup
# UV_LINK_MODE: Use copy instead of hardlinks (required for cache mounts)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Set working directory
WORKDIR /app

# Install dependencies first (with cache mount and bind mounts)
# This layer is cached separately from project code for better rebuild performance
# Bind mounts prevent copying files into the image, improving layer efficiency
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Copy project source code
COPY . /app

# Install the project itself (with cache mount)
# This completes the installation by installing the project into the venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# Bake the embedding model into the image. The server loads it with
# local_files_only=True (no runtime downloads), and backend/models is excluded from the
# build context by .dockerignore, so without this step a container cannot start indexing.
ARG EMBEDDING_MODEL=all-MiniLM-L6-v2
RUN EMBEDDING_MODEL="${EMBEDDING_MODEL}" /app/.venv/bin/python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBEDDING_MODEL'], cache_folder='/app/backend/models')"

# Production stage
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd -m -u 1000 -s /bin/bash mcpuser

# Set working directory
WORKDIR /app

# Copy Python environment from builder
COPY --from=builder --chown=mcpuser:mcpuser /app/.venv /app/.venv

# Copy application code
COPY --chown=mcpuser:mcpuser . .

# Embedding model baked in by the builder stage
COPY --from=builder --chown=mcpuser:mcpuser /app/backend/models /app/backend/models

# Persistent data (ChromaDB indexes + cloned KB repos). Mount a volume here.
RUN mkdir -p /data/chroma /data/repos \
    && chown -R mcpuser:mcpuser /data /app/backend

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # UV configuration for production
    UV_NO_DEV=1 \
    # ChromaDB configuration
    TOKENIZERS_PARALLELISM=false \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    NAUTOBOT_MCP_DATA_DIR=/data/chroma \
    NAUTOBOT_MCP_REPO_CACHE=/data/repos \
    # MCP configuration (can be overridden at runtime). HTTP is the sensible default
    # for a container; set MCP_TRANSPORT=stdio for `docker run -i` usage.
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

# Switch to non-root user
USER mcpuser

# Expose HTTP port (only used in HTTP mode)
EXPOSE 8000

# Liveness only: /health answers as soon as the server is up, while indexes are still
# being built in the background. Use /ready (503 until indexed) for readiness gating.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MCP_PORT','8000'), timeout=4)" || exit 1

# Default entrypoint. No default CMD: transport/host/port come from MCP_* env vars
# (an explicit `--mode` in CMD would silently override MCP_TRANSPORT).
ENTRYPOINT ["python", "main.py"]
