# Zashiki-warasi — Docker Compose quickstart

Runs the FastAPI service in a container, with an **external** Postgres
(bring your own) and an **external** cron scheduler on the Docker host
driving `POST /poll`.

## Prerequisites

- Docker + Docker Compose v2 on the host.
- A reachable Postgres (LAN, `host.docker.internal`, existing DB
  server — the compose file does NOT ship a Postgres).
- OAuth client secrets JSON from Google Cloud Console. See
  [`docs/oauth-redirect-uri.md`](../../docs/oauth-redirect-uri.md) for
  which redirect URI to register.

## 5-minute setup

1. **Copy env template.**

   ```
   cp .env.example .env
   ```

   Fill in at least `DATABASE_URL`, `HTTP_API_KEY`, `OAUTH_REDIRECT_URI`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

2. **Drop your OAuth client secrets in place.**

   ```
   mkdir -p secrets data
   cp /path/to/downloaded/credentials.json secrets/credentials.json
   ```

   If you already have a valid `token.json` from a previous v0.6.x
   deploy, put it in `data/token.json`. Otherwise you'll bootstrap
   OAuth on first run via CLI reauth (see step 5).

   **Fix ownership.** The container runs as uid `10001` (`zashiki`);
   host bind-mounts inherit host ownership, so the container can't
   read your files unless you match:

   ```
   sudo chown -R 10001:10001 secrets data
   chmod 600 secrets/credentials.json data/token.json 2>/dev/null || true
   ```

3. **Build the image** (skip if pulling from a registry).

   ```
   docker compose build
   ```

4. **Start the service.**

   ```
   docker compose up -d
   docker compose ps                     # expect status=running (healthy)
   curl http://127.0.0.1:8080/healthz    # expect 200 with checks={db:true, oauth:true}
   ```

   If `/healthz` returns 503 with `oauth: false`, run step 5 to
   bootstrap the token.

5. **Bootstrap OAuth (first-time only, if you didn't seed `data/token.json`).**

   Option A — CLI reauth on your workstation:

   ```
   # On your workstation (not in the container)
   uv run zashiki-warasi reauth
   # This opens a browser via InstalledAppFlow.run_local_server().
   # The resulting token.json goes to $GMAIL_TOKEN_PATH — copy it
   # into `data/` on the Docker host.
   ```

   Option B — headless reauth via the service:

   ```
   curl -X POST http://127.0.0.1:8080/reauth -H "X-API-Key: ${HTTP_API_KEY}"
   # Open the returned auth_url in a browser; the callback writes
   # token.json into /data/ inside the container.
   ```

6. **Wire cron.**

   ```
   crontab -e
   # Paste the single line from crontab.example (edit the API key).
   ```

   Confirm cron is firing:

   ```
   tail -f /var/log/zashiki-cron.log
   # Expect: `200` lines every minute (or `409` if a previous tick is
   # still running — signal, not error).
   ```

## Ongoing operations

- **Live-tail service logs**: `docker compose logs -f zashiki-warasi`
- **Restart after config changes**: `docker compose up -d` (docker
  detects env changes and recreates the container)
- **Manual one-off tick**: `docker compose exec zashiki-warasi zashiki-warasi tick`
- **Trigger OAuth reauth via URL**: `POST /reauth` (see step 5B)
- **Cron log grep for problems**:
  - `grep -v ' 200$' /var/log/zashiki-cron.log` — non-successes
  - `grep ' 409$' /var/log/zashiki-cron.log` — overlap events

## Rollback

```
docker compose down
# Restart your v0.6.x daemon (existing DB state carries over — no
# schema incompatibility; `oauth_flows` table stays but v0.6.x
# ignores it).
```

## Where to look for known-issue root causes

- **DB unreachable at startup**: container exits code 71; `docker
  compose ps` shows `restarting`. Check `DATABASE_URL` reachability
  from inside the container (`docker compose exec zashiki-warasi curl
  ${DATABASE_URL%%\?*}` won't work — use `pg_isready` on the DB host).
- **OAuth expiry**: `/healthz` returns 503 with `oauth: false` +
  Telegram notify fires. Trigger reauth per step 5.
- **Tick stuck**: `POST /poll` returns 409 continuously. Investigate
  with `docker compose exec zashiki-warasi zashiki-warasi tick` to see
  whether the tick is genuinely long-running or the advisory lock is
  stuck (see the openspec migrate-to-fastapi-service change's
  MIGRATION-v1.md troubleshooting section).
