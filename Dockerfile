# syntax=docker/dockerfile:1
# =============================================================================
# Multi-stage build.
#
# Stage 1 resolves dependencies into a virtualenv; stage 2 copies that venv into
# a clean runtime image.
#
# Dependencies are installed from a list exported out of pyproject.toml rather
# than by running `pip install .`. The build backend would need the `app` and
# `mcp_servers` packages in order to build a wheel, and copying them first would
# make every source edit invalidate the (slow) dependency layer. Exporting the
# list keeps that layer keyed on pyproject.toml alone, with no duplicated
# requirements file to drift.
#
# The application is not installed as a package: it runs from /app with
# PYTHONPATH set, which is also how the MCP servers are launched
# (`python -m mcp_servers.crm_server`).
# =============================================================================

# --------------------------------------------------------------------------- #
# Stage 1 -- builder
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build toolchain for any dependency without a manylinux wheel; dropped in stage 2.
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Only what is needed to resolve dependencies -- this layer stays cached while
# application source changes.
COPY pyproject.toml ./
COPY scripts/export_requirements.py ./scripts/
RUN python -m pip install --upgrade pip setuptools wheel \
 && python scripts/export_requirements.py > requirements.txt \
 && python -m pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# Stage 2 -- runtime
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# curl is used by the compose healthcheck.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app mcp_servers ./mcp_servers
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini pyproject.toml README.md ./

# Writable location for the mock email outbox.
RUN mkdir -p /app/data/outbox && chown -R app:app /app/data

# Run unprivileged: the agent executes tools, so the blast radius of a
# compromised tool should not include root in the container.
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

# The API launches the MCP servers as stdio subprocesses of this same image,
# which is why mcp_servers/ ships in the runtime layer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
