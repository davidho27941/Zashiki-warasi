# syntax=docker/dockerfile:1.7

# Stage 1 — resolve & install Python dependencies with uv into a
# project-local virtualenv. uv is fastest when it can cache the lock
# resolution; the bind+cache mount below keeps cold builds <10s.
FROM python:3.13-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir uv==0.5.*

WORKDIR /app

# Install dependencies first (cacheable layer). The bind mount means
# pyproject.toml / uv.lock don't enter the layer tree — only the
# resolved .venv does, so changing app code doesn't bust dep cache.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# Now install the project itself (separate layer so app code edits
# don't re-resolve the full graph).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Stage 2 — minimal runtime image. Carries only the venv and the
# code Alembic + the CLI need at runtime.
FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default credentials/token locations the app reads. Mount over these
# with bind mounts (host ./credentials) or volumes in compose.
RUN mkdir -p /app/credentials /root/.config/zashiki-warasi

ENTRYPOINT ["/entrypoint.sh"]
CMD []
