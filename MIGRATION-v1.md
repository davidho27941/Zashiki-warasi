# Migrating from v0.6.x to v1.0.0

v1.0.0 replaces the long-running Python daemon with a FastAPI service
driven by an external scheduler (host cron or k8s CronJob). No DB
schema breaks; no data migration required. The upgrade path is
container-swap + scheduler-install + old-daemon-stop.

## What's changing

- **Entry point.** `python -m zashiki_warasi` (bare) is gone. The v1.0
  CLI has explicit subcommands: `serve` (uvicorn wrapper), `tick`
  (one-off in-process poll), `reauth` (existing InstalledAppFlow),
  `sync-notion` (unchanged). A hidden `run-legacy` subcommand
  preserves the v0.6.x default behavior for wrapper scripts that
  hardcode the old shape.
- **Process model.** In production, run the container's default
  `uvicorn zashiki_warasi.web:app`. Cadence comes from an external
  cron (host crontab or k8s CronJob) hitting `POST /poll`.
- **HTTP surface.** `/healthz`, `POST /poll`, `POST /reauth`,
  `GET /auth/start`, `GET /auth/callback`. Optional `X-API-Key` auth
  on the two POSTs. See [`docs/oauth-redirect-uri.md`](./docs/oauth-redirect-uri.md)
  for the reauth setup.
- **Removed env vars.** `POLLER_INTERVAL_SECONDS` and
  `POLLER_HEARTBEAT_INTERVAL_SECONDS` are silently ignored (soft
  migration) — cadence and liveness are external now. If either is
  set, a single INFO line on startup warns the operator to clean up
  their `.env`.
- **New DB table.** `oauth_flows` (state, flow_json, created_at) is
  created idempotently at startup by the same bootstrap that calls
  `PostgresSaver.setup()`. No manual migration script.
- **Multi-replica readiness.** Coordination surfaces (tick lock,
  OAuth flow store) live in Postgres, so `replicaCount > 1` works
  without code changes when the WebUI epic wants HA reads. Default
  stays 1.

## Prerequisites

- Docker or a k8s cluster reachable from the host.
- The **existing** Postgres from your v0.6.x deploy — v1.0 does not
  provision one. Confirm reachable from where the container will run.
- The **existing** Google OAuth client — no new client needed. Just
  add one authorized redirect URI:
  - **Pick a redirect URI strategy from
    [`docs/oauth-redirect-uri.md`](./docs/oauth-redirect-uri.md).**
    Homelab default is Strategy A (SSH tunnel + loopback URI
    `http://127.0.0.1:8080/auth/callback`).
  - Register it in the Google Cloud Console: APIs & Services →
    Credentials → your OAuth Client → Authorized redirect URIs → Save.
  - Wait up to 5 minutes for propagation.

## Upgrade — Docker Compose path

1. **Pull down the new deploy artifacts** (or checkout `v1.0.0`).

2. **Set up compose env.** Copy your existing `.env` values across:
   ```
   cd deploy/compose
   cp .env.example .env
   $EDITOR .env
   ```
   Carry over: `DATABASE_URL`, all `LLM_*`, all `TELEGRAM_*`, all
   `NOTION_*`, all `DATABASE_CHECKPOINTER_POOL_*` (if you tuned them).
   Add: `HTTP_API_KEY` (generate with `openssl rand -hex 32`),
   `OAUTH_REDIRECT_URI` (from step 3 of Prerequisites).
   Drop: `POLLER_INTERVAL_SECONDS`, `POLLER_HEARTBEAT_INTERVAL_SECONDS`
   (silently ignored, but clean up anyway).

3. **Mount your existing OAuth artifacts.**
   ```
   mkdir -p secrets data
   cp /path/to/your/v0.6.x/credentials.json secrets/credentials.json
   cp /path/to/your/v0.6.x/token.json data/token.json    # if you have one
   chmod 600 data/token.json                              # match the app's expectation
   ```
   Adjust `GMAIL_CREDENTIALS_HOST_PATH` / `DATA_DIR` in `.env` if
   your files live elsewhere.

4. **Stop the v0.6.x daemon.** Do this BEFORE starting v1.0 so both
   don't race on the same LangGraph checkpointer state:
   ```
   # However you were running the v0.6.x daemon:
   # systemctl stop zashiki-warasi.service   OR
   # kill $(pgrep -f 'zashiki_warasi')       OR
   # docker compose down                     (if you used the v0.5 compose stack)
   ```

5. **Start v1.0.**
   ```
   docker compose up -d --build
   docker compose ps                          # expect status=running (healthy)
   curl http://127.0.0.1:8080/healthz         # expect 200 with checks={db:true, oauth:true}
   ```
   If `/healthz` returns 503 with `oauth: false`, either seed
   `token.json` (step 3) or bootstrap via headless reauth (below).

6. **Install cron.**
   ```
   crontab -e
   # Paste the line from deploy/compose/crontab.example, editing the API key.
   ```
   Verify:
   ```
   tail -f /var/log/zashiki-cron.log
   # Expect one `200` line per minute. `409` = single-flight overlap (fine).
   ```

7. **Verify end-to-end** by sending yourself a test email and
   confirming the Telegram notification lands within ~90 s.

## Upgrade — Helm / k8s path

1. **Build + push the image.**
   ```
   docker build -t <your-registry>/zashiki-warasi:1.0.0 .
   docker push <your-registry>/zashiki-warasi:1.0.0
   ```

2. **Prepare a values file** with your secrets — keep this OUT of
   version control:
   ```yaml
   # values.local.yaml
   image:
     repository: <your-registry>/zashiki-warasi
     tag: "1.0.0"
   secrets:
     databaseUrl: "postgresql+psycopg://user:pw@postgres.internal:5432/zashiki"
     httpApiKey: "…openssl rand -hex 32…"
     telegramBotToken: "…"
     telegramChatId: "…"
   oauth:
     credentialsJson: |
       …paste credentials.json contents here…
     tokenJson: |
       …paste token.json contents here (or leave empty and bootstrap via /reauth)…
   env:
     OAUTH_REDIRECT_URI: "…from Prerequisites step 3…"
   ```

3. **Stop the v0.6.x daemon.** Same reasoning as compose step 4.

4. **Install the chart.**
   ```
   helm install zashiki ./deploy/helm/zashiki-warasi -f values.local.yaml
   kubectl rollout status deployment/zashiki-zashiki-warasi
   kubectl port-forward svc/zashiki-zashiki-warasi 8080:8080
   curl http://127.0.0.1:8080/healthz
   ```

5. **Verify the CronJob is firing.**
   ```
   kubectl get cronjob zashiki-zashiki-warasi-poll
   kubectl get jobs -l app.kubernetes.io/instance=zashiki --sort-by=.metadata.creationTimestamp | tail
   kubectl logs -l job-name=<latest-job-name>          # expect a single line: 200
   ```

## Bootstrapping OAuth on a fresh deploy

If you don't have a v0.6.x `token.json` to seed with, use one of:

**A — CLI reauth on your workstation, copy the resulting token.json:**
```
uv run zashiki-warasi reauth
# Opens a browser via InstalledAppFlow. The resulting token.json lands
# at $GMAIL_TOKEN_PATH — copy it to the container's /data/ mount.
```

**B — Headless reauth via the service (Strategy A from
[`docs/oauth-redirect-uri.md`](./docs/oauth-redirect-uri.md)):**
```
# From your workstation, over SSH tunnel:
ssh -L 8080:localhost:8080 you@container-host
# Then, in another terminal:
curl -X POST http://127.0.0.1:8080/reauth -H "X-API-Key: $HTTP_API_KEY"
# Open the returned auth_url in a browser, complete Google consent.
```

## Rollback

**Docker Compose:**
```
docker compose down
# Restart your v0.6.x daemon. DB state is compatible — the new
# `oauth_flows` table is unused by v0.6.x code, no other changes.
```

**Helm:**
```
helm uninstall zashiki
# Restart your v0.6.x deploy.
```

No data-format change. Both versions can read the same
`gmail_sync_state`, `processed_messages`, `email_analyses`,
`expenses`, and LangGraph checkpoint tables.

## Troubleshooting

### `/healthz` returns 503 with `oauth: false`

- The `token.json` file at `$GMAIL_TOKEN_PATH` inside the container
  is missing, unreadable, expired without a refresh token, or has
  the wrong scopes.
- Fix: bootstrap via CLI reauth (option A above) OR headless reauth
  (option B above).

### `/healthz` returns 503 with `db: false`

- The container can't reach Postgres. Check `DATABASE_URL`
  reachability from INSIDE the container:
  ```
  docker compose exec zashiki-warasi sh -c 'python -c "import psycopg; print(psycopg.connect(\"$DATABASE_URL\".replace(\"+psycopg\",\"\")).info.status)"'
  ```
- The pool may have given up after `reconnect_timeout` (5 min
  default) — container will restart with exit code 71 and try again.

### Cron log shows nothing but `409` for hours

- Something is holding the tick advisory lock permanently. Very rare
  — usually means a process crash on the DB side that Postgres hasn't
  yet noticed. Force-release from any operator psql session:
  ```
  psql "$DATABASE_URL" -c "SELECT pid, granted, objid FROM pg_locks WHERE locktype = 'advisory';"
  # Identify the stuck session's pid, then:
  psql "$DATABASE_URL" -c "SELECT pg_terminate_backend(<pid>);"
  ```
  Or, if you can identify the specific stuck lock by objid:
  ```
  # `objid` for TICK_LOCK_KEY = the low 32 bits of -6178253175476858907
  psql "$DATABASE_URL" -c "SELECT pg_advisory_unlock_all();"   # frees all locks THIS session holds; only helps if the session that holds the lock is still this one, which it usually isn't
  ```
  The best fix is usually to restart the container that holds the
  stuck lock (`docker compose restart zashiki-warasi` /
  `kubectl delete pod`); Postgres releases the lock on TCP close.

### Cron log shows non-2xx AND non-409 codes

- The tick body crashed with an uncaught exception (HTTP 500) or the
  auth check failed (HTTP 401). Look at the container logs:
  ```
  docker compose logs --tail=200 zashiki-warasi
  # or:
  kubectl logs -l app.kubernetes.io/instance=zashiki --tail=200
  ```
  Grep by request-id to isolate the failing tick's log lines:
  ```
  docker compose logs zashiki-warasi | grep "request_id=<the-id-from-cron-response-header>"
  ```

### OAuth reauth via `/auth/callback` fails with `redirect_uri_mismatch`

- The `OAUTH_REDIRECT_URI` env var doesn't match any URI registered
  in the Google Cloud Console. Read
  [`docs/oauth-redirect-uri.md`](./docs/oauth-redirect-uri.md).

### `POLLER_HEARTBEAT_INTERVAL_SECONDS` still set — is it doing anything?

- No. It's silently ignored in v1.0. The one-time INFO log at startup
  is your reminder to remove it. Liveness is now:
  - `docker compose ps` / `kubectl get pod` (container status)
  - `/healthz` probe results
  - The cron log's per-tick HTTP response codes
  - Missing lines in the cron log = external-scheduler failure

### Container exits with code 78 (`EXIT_CREDENTIAL_FAILURE`)

- OAuth refresh token dead at startup. Same fix as v0.6.x: run
  `reauth` (CLI or headless) to write a fresh `token.json`, then
  restart the container.

### Container exits with code 71 (`EXIT_DB_UNREACHABLE`)

- The checkpointer pool exhausted its `reconnect_timeout` trying to
  reach Postgres. Container restart is automatic (compose
  `unless-stopped`, k8s `restartPolicy: Always`); persistent failure
  surfaces as `restarting` / `CrashLoopBackOff`. Investigate DB
  reachability from the container's network.
