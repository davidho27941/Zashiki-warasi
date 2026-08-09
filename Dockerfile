# syntax=docker/dockerfile:1.7

# ---------- builder ----------
# `uv` resolves + installs into an isolated venv the runtime stage
# copies verbatim. Kept in a separate stage so build-time tooling
# (uv, compilers, apt caches) doesn't leak into the runtime image.
FROM python:3.13-slim-bookworm AS builder

# Install uv into the builder image. Prefer pip over `COPY --from`
# because ghcr.io/astral-sh/uv requires authenticated pulls in some
# environments (CI runners with no GHCR credentials otherwise fail).
# uv itself has no runtime deps beyond CPython, so this stays clean.
RUN pip install --no-cache-dir uv

# psycopg[binary] wheels ship with the correct manylinux native libs;
# no compiler needed at build time — this stays lean.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app
# Lock+manifest first so any dep change invalidates the cache layer,
# but pure-source changes reuse the wheel install layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
# Now install the local package itself into the same venv.
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.13-slim-bookworm AS runtime

# curl is used by the HEALTHCHECK below and by k8s CronJob-based
# schedulers curling POST /poll from a sidecar. It's ~3 MB unpacked
# and worth the overhead for a one-line healthcheck vs. baking a
# Python-based probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (uid/gid 10001). Fixed value so k8s NetworkPolicies
# / SecurityContexts can reference it. Same uid across compose + Helm
# so any host-mounted /data volume permissions stay consistent.
RUN groupadd --system --gid 10001 zashiki \
    && useradd --system --uid 10001 --gid zashiki --home-dir /app --shell /sbin/nologin zashiki

WORKDIR /app
COPY --from=builder --chown=zashiki:zashiki /app/.venv /app/.venv
COPY --from=builder --chown=zashiki:zashiki /app/src /app/src
COPY --from=builder --chown=zashiki:zashiki /app/pyproject.toml /app/README.md /app/
# Alembic config + migration scripts. The entrypoint runs
# `alembic upgrade head` before uvicorn so fresh DBs auto-provision.
COPY --chown=zashiki:zashiki alembic.ini /app/
COPY --chown=zashiki:zashiki alembic /app/alembic
COPY --chown=root:root docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# /data must be writable by the container user — token.json lives here
# and the OAuth web flow writes to it on successful reauth. Compose
# mounts a host directory over this; Helm mounts a PVC.
RUN mkdir -p /data /secrets \
    && chown zashiki:zashiki /data /secrets

# venv on PATH so `uvicorn` / `zashiki-warasi` resolve without a shell
# activation dance.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GMAIL_CREDENTIALS_PATH=/secrets/credentials.json \
    GMAIL_TOKEN_PATH=/data/token.json \
    HTTP_BIND_HOST=0.0.0.0 \
    HTTP_BIND_PORT=8080

USER zashiki
EXPOSE 8080

# `curl -f` returns non-zero on 5xx so docker's healthcheck flips to
# unhealthy on any /healthz 503 (our real dependency-truth signal).
# start-period is generous because the entrypoint runs alembic upgrade
# head before uvicorn — a fresh DB with many migrations can take a
# few seconds before /healthz starts answering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# Entrypoint runs `alembic upgrade head` against DATABASE_URL, then
# execs CMD. Override for one-shots: `docker compose run --rm
# --entrypoint sh zashiki-warasi -c 'zashiki-warasi tick'`.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default CMD: run the FastAPI service. Override to `zashiki-warasi
# tick` / `... reauth` for one-shots.
CMD ["uvicorn", "zashiki_warasi.web:app", "--host", "0.0.0.0", "--port", "8080"]
