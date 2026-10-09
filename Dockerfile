# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Multi-stage build using openenv-base
# This Dockerfile is flexible and works for both:
# - In-repo environments (with local OpenEnv sources)
# - Standalone environments (with openenv from PyPI/Git)
# The build script (openenv build) handles context detection and sets appropriate build args.

ARG BASE_IMAGE=ghcr.io/meta-pytorch/openenv-base:latest
FROM ${BASE_IMAGE} AS builder

WORKDIR /app

# Ensure git is available (required for installing dependencies from VCS)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Build argument to control whether we're building standalone or in-repo
ARG BUILD_MODE=in-repo
ARG ENV_NAME=meta_hackathon

# Copy environment code (always at root of build context)
COPY . /app/env

# For in-repo builds, openenv is already vendored in the build context
# For standalone builds, openenv will be installed via pyproject.toml
WORKDIR /app/env

# If a host virtualenv was copied into the context, remove it so uv can create
# a valid Linux environment inside the container.
RUN rm -rf .venv

# Ensure uv is available (for local builds where base image lacks it)
RUN if ! command -v uv >/dev/null 2>&1; then \
        curl -LsSf https://astral.sh/uv/install.sh | sh && \
        mv /root/.local/bin/uv /usr/local/bin/uv && \
        mv /root/.local/bin/uvx /usr/local/bin/uvx; \
    fi
    
# Install dependencies using uv sync
# If uv.lock exists, use it; otherwise resolve on the fly
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-install-project --no-editable; \
    else \
        uv sync --no-install-project --no-editable; \
    fi

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-editable; \
    else \
        uv sync --no-editable; \
    fi

# Final runtime stage
FROM ${BASE_IMAGE}

WORKDIR /app

# Install git + docker CLI + docker compose plugin so the env-server can drive
# a real CI/CD pipeline against a mounted /var/run/docker.sock.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    . /etc/os-release && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from builder
COPY --from=builder /app/env/.venv /app/.venv

# Copy the environment code
COPY --from=builder /app/env /app/env

# Set PATH to use the virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Set PYTHONPATH so imports work correctly
ENV PYTHONPATH="/app/env:$PYTHONPATH"

# Enable OpenEnv web interface so /web is available when running locally.
ENV ENABLE_WEB_INTERFACE=true

# DB lives in the server folder; path is overridable via env var.
ENV AGENT_MEMORY_DB_PATH=/app/env/server/agent_memory.db

# Initialise the SQLite DB at build time so it survives HF Spaces ephemeral restarts.
RUN cd /app/env && python -c 'from server.agent_memory import _init_db; _init_db(); print("DB ready:", __import__("os").getenv("AGENT_MEMORY_DB_PATH"))' && \
    ls -lh /app/env/server/agent_memory.db

# Health check — cicd_api.py exposes /health on port 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run cicd_api.py (workspace + WebSocket API) on port 8000.
# Exposes:
#   POST /api/workspace/create         — bootstrap workspace, inject fault, pre-run pipeline
#   WS   /api/ws/{workspace_id}        — persistent per-episode WebSocket session
#   GET  /health                       — health probe
CMD ["sh", "-c", "cd /app/env && uvicorn server.cicd_api:app --host 0.0.0.0 --port 8000 --ws-ping-interval 30 --ws-ping-timeout 60"]
