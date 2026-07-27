#!/bin/sh
# Container entrypoint:
#   1. Run any pending Alembic migrations against $DATABASE_URL.
#   2. Exec the CLI with whatever args were passed to `docker run`
#      (so `--reset`, `sync-notion`, `-h`, etc. all work unchanged).
#
# `exec` replaces the shell so signals (SIGTERM from `docker stop`)
# reach the Python process directly — the poller's shutdown handlers
# need this to drain the in-flight message before exit.
set -e

echo "[entrypoint] Running alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] Starting zashiki-warasi $*"
exec zashiki-warasi "$@"
