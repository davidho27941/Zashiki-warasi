#!/bin/sh
# Runs `alembic upgrade head` against $DATABASE_URL, then execs into
# the CMD (default: uvicorn zashiki_warasi.web:app). Alembic's own
# row-lock on `alembic_version` makes concurrent invocations safe when
# multiple replicas boot simultaneously — the loser is a no-op.
set -e

echo "[entrypoint] running alembic upgrade head..."
alembic upgrade head
echo "[entrypoint] migrations complete; starting: $*"
exec "$@"
