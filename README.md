# Zashiki-warasi

A self-hosted Gmail polling agent that classifies incoming mail with an LLM,
extracts structured expense records from receipts / cloud invoices / utility
bills (including PDF attachments), and mirrors them to a Notion database.
Built on [LangGraph](https://github.com/langchain-ai/langgraph),
[pydantic-settings](https://docs.pydantic.dev/latest/), and Postgres for
durable state.

The name comes from [座敷童子](https://en.wikipedia.org/wiki/Zashiki-warashi),
a household spirit said to quietly bring fortune to the home it lives in —
which is roughly what an email agent that watches your inbox is supposed to do.

## What it does

1. Runs as a FastAPI service (`uvicorn zashiki_warasi.web:app`); an
   external scheduler (host `crontab` or k8s `CronJob`) triggers one
   poll cycle per invocation by hitting `POST /poll`. Each cycle
   advances the Gmail `historyId` cursor (incremental, no
   re-fetching).
2. For each new message, asks an LLM to produce a structured
   `{ category, importance, urgency, summary, keywords }` analysis.
3. When the category is `消費支出` (personal spending) or `帳單通知`
   (bills / cloud-service invoices / recurring charges), routes the mail
   into an **expense subgraph** — combines the body text with any PDF
   attachments (pdfplumber), asks the LLM for structured payment fields
   (`amount`, `currency`, `vendor`, `transacted_at`, `transaction_id`,
   `payment_method`, …), dedups against existing records, and writes to
   Postgres.
4. Sends every analysis (with the expense record when present) as a
   Telegram message.
5. **Optional Notion mirror.** Successful expense records are written to
   a Notion database. A background thread reconciles user edits made in
   Notion (typo fixes, category corrections) back into Postgres — Notion
   wins.
6. LLM context-window failures are caught and surfaced as a distinct
   `⚠️ LLM 分析失敗` Telegram message; the poller does not get stuck
   re-firing the doomed call.

Crash-safe by design: per-message dedup plus LangGraph checkpoints (keyed by
Gmail message ID) mean a restart never loses or re-bills a message, even if
the process dies mid-LLM-call.

**Body source for the LLM**: `text/plain` is preferred when present
(cleanest, no markup noise). HTML-only emails (modern e-receipts,
newsletters) fall back to a stripped-down conversion via `html2text`
so the LLM sees the full content instead of just Gmail's 200-char
snippet. The snippet is the last-resort fallback when neither plain
nor HTML is available. In the expense subgraph the body is further
concatenated with the text extracted from any PDF attachments before
the LLM sees it.

## Architecture

```
                  ┌──────────────────────────────────────┐
Gmail API ◀──────▶│ GmailClient (auth, fetch, history)   │
                  └────────────────┬─────────────────────┘
                                   │
                  ┌────────────────▼─────────────────────┐
                  │ Poller (historyId cursor + dedup)    │
                  └────────────────┬─────────────────────┘
                                   │ EmailMessage
                  ┌────────────────▼─────────────────────┐
                  │ EmailAgent (LangGraph)               │
                  │                                      │
                  │   analyze ─┬─ 消費支出 / 帳單通知 ─▶ │──▶ chat model
                  │            │        expense_sg       │    (llama.cpp /
                  │            │  ┌──────────────────┐   │     OpenAI / ...)
                  │            │  │ collect_text     │──▶│──▶ pdfplumber
                  │            │  │ (body + PDF/HTML)│   │
                  │            │  ├──────────────────┤   │
                  │            │  │ extract (LLM)    │   │
                  │            │  ├──────────────────┤   │
                  │            │  │ persist + dedup  │   │
                  │            │  └────────┬─────────┘   │
                  │            │           │             │
                  │            ▼           ▼             │
                  │          notify (Telegram)           │──▶ Telegram
                  └───────────────────┬──────────────────┘
                                      │
              ┌───────────────────────┴─────────────────────────┐
              ▼                                                 ▼
   Postgres                                     Notion (optional)
   ── gmail_sync_state       (poll cursor)      ── expense DB
   ── notion_sync_state      (puller cursor)          ▲       │
   ── processed_messages     (dedup)                  │       │
   ── email_analyses         (LLM output)     write ──┘       │
   ── expenses               (payments)          (NotionExpenseRecorder)
   ── checkpoints, …         (LangGraph state)               │
                                                             │
                                                     read ◀──┘
                                                     (NotionExpensePuller,
                                                     background thread,
                                                     reverse-syncs edits)
```

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- PostgreSQL (local or remote). Create the database with
  `WITH ENCODING 'UTF8' TEMPLATE template0` — the analyze node writes
  Chinese JSON to `email_analyses.keywords`; a `SQL_ASCII`-encoded DB
  will crash on `_persist` (see `docs/v1.0-smoke-lessons.md` issue #14).
- An LLM endpoint. Defaults assume [llama.cpp](https://github.com/ggerganov/llama.cpp)
  running locally with `llama-server` on port 8080.
- A Google Cloud OAuth 2.0 Client ID for Gmail access. **Desktop app**
  type is the simplest — Google auto-allows loopback redirect URIs
  (`http://127.0.0.1:*`) without any manual URI registration, which is
  what both the CLI `reauth` and the container's headless flow rely
  on. **Web application** type works too but requires you to register
  the redirect URI in the console. See [`docs/oauth-redirect-uri.md`](docs/oauth-redirect-uri.md).

### Optional (v1.1 observability)

- **Prometheus + Grafana** — if you want to scrape the `/metrics`
  endpoint. Compose deploy ships a self-contained stack under
  `--profile observability` (Prometheus + Tempo + Grafana); k3s
  deploys assume kube-prometheus-stack already lives in the cluster.
  See [`docs/observability.md`](docs/observability.md) for enable
  paths.
- **OpenTelemetry Collector** — required only if you want tracing
  (opt-in via `OTEL_ENABLED=1`). Compose observability profile
  bundles a collector; k3s expects one deployed separately (chart
  does not ship a Collector).
- **Runtime worker count** — the runtime is designed for
  single-worker uvicorn. `WEB_CONCURRENCY>1` fails fast at startup
  (`prometheus_client` registry is process-local; horizontal scale
  should go through `replicaCount`, not workers). Details in
  [`docs/observability.md`](docs/observability.md#troubleshooting)
  troubleshooting section.

## Setup

### 1. Install

```bash
git clone <this repo>
cd Zashiki-warasi
uv sync
```

### 2. Postgres

Create a database and apply migrations:

```bash
createdb --encoding=UTF8 --locale=C --template=template0 zashiki_warasi
uv run alembic upgrade head
```

> **The database must use UTF-8 encoding.** The agent stores Chinese
> / Japanese keywords and summaries; on a `SQL_ASCII` cluster the
> JSON column write fails with `psycopg.errors.UntranslatableCharacter`
> because Postgres parses JSON server-side and cannot store code
> points above U+007F. The `--template=template0` flag is required
> because `template1` typically inherits the cluster's default
> encoding, which on some macOS Postgres installs is `SQL_ASCII`.
> Verify with `psql -d zashiki_warasi -c "SHOW server_encoding"`
> — you should see `UTF8`.

If your connection differs from the default, set `DATABASE_URL`:

```bash
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/zashiki_warasi
```

LangGraph's checkpoint tables are created automatically at first run
(via `PostgresSaver.setup()`); only the application-domain tables are
managed by Alembic.

### 3. Gmail OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/), create
   an OAuth 2.0 Client ID of type **Desktop app** and download the
   `client_secret_*.json` file.
2. Save it as `credentials.json` in the project root (or set
   `GMAIL_CREDENTIALS_PATH`).
3. **Publish the OAuth consent screen** (APIs & Services → OAuth consent
   screen → `PUBLISH APP`). A consent screen left in **Testing** status
   expires every refresh token after **7 days**, so the poller will
   crash weekly with `invalid_grant: Token has been expired or revoked`.
   Individual Gmail accounts can publish without Google verification —
   external users just see an "unverified app" warning.
4. The first run will open a browser for one-time consent; the refresh
   token is then cached at `~/.config/zashiki-warasi/token.json`.

If the refresh token is later revoked (Google security action, manual
revoke, or the 50-refresh-tokens-per-user cap being hit during dev),
the poller sends a Telegram alert and exits with code 78. To recover:

```bash
uv run zashiki-warasi reauth
```

This deletes the stale `token.json` and re-runs the browser consent
flow. Then restart the poller.

### 4. LLM

The default config expects `llama-server` on `http://localhost:8080/v1`
(OpenAI-compatible). Start it with whatever GGUF model you prefer:

```bash
llama-server -m /path/to/model.gguf --port 8080
```

To point at OpenAI or another provider instead, see the env vars below.

### 5. Notion (optional)

Set `NOTION_TOKEN` and `NOTION_EXPENSE_DATABASE_ID` to mirror every
recorded expense into a Notion database. Leave either empty and the
agent skips Notion entirely — no calls, no extra dependency to think
about at runtime.

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations);
   copy the token (starts with `secret_`).
2. Create a Notion database for expenses with the schema below
   (property names are matched exactly):

   | Property | Type | Notes |
   | --- | --- | --- |
   | 消費名稱 | Title | Required by Notion (every DB needs one Title-type property). Receives the LLM-generated short description of the expense (e.g. `拿鐵 + 摩卡星冰樂`, `Amazon Kindle 訂單`). Falls back to the vendor name, then `(不明)`. The default Notion title column is usually named "Name"; rename it to `消費名稱` to match, or change `PROP_TITLE` in `notifications/notion.py`. |
   | 消費店家 | Rich text | The merchant name (e.g. `Starbucks 渋谷店`), separate from the title. Omitted when the LLM couldn't extract a vendor. |
   | 消費金額 | Number | |
   | 幣別 | Select | Predefine options: `日幣`, `台幣`, `美金` (the agent translates ISO codes from extraction) |
   | 消費日期 | Date | Date+time supported via ISO 8601 |
   | 消費類別 | Select | Predefine your category options (e.g. `飲食`, `交通`, `購物`, `訂閱`, `水電`, `講座`, `其他`); the LLM must use a label that already exists, so add new options as you encounter new expense kinds |
   | 支付方式 | Select | Predefine options: `Rakuten Pay`, `SMBC Olive`, `三菱UFJ-JCB`, `PayPay`, `信用卡`, `現金`, `其他` |
   | UUID | Rich text | Both real transaction ids (e.g. SMBC's `承認番号`) and `AUTO-…` placeholders |

   Add a `備註` (Rich text) column. The agent stamps every row it
   creates with `auto generated by zashiki-warasi` in this column so
   you can tell agent-created rows apart from ones you added
   manually. Free-form text below the marker is yours to add.

   The `location` extracted from each email is intentionally **not**
   mirrored to Notion; it stays in the Postgres `expenses.location`
   column.

3. Open the database as a full page → **Share** → invite your
   integration so it can write.
4. Copy the database id (the 32-char hex chunk in the URL, before
   any `?v=`) into `NOTION_EXPENSE_DATABASE_ID`.

Sync is **best-effort**: a failed Notion call (network down, schema
mismatch, integration revoked) is captured as `notion_sync_error` on
the `expenses` row and surfaced in the Telegram message as
`⚠️ Notion 同步失敗: …`. The Postgres write is unaffected and
remains the source of truth.

### 6. Run

Local dev / smoke:

```bash
uv run zashiki-warasi serve          # start FastAPI on 127.0.0.1:8080
uv run zashiki-warasi tick           # one-off poll cycle, prints TickResult JSON
uv run zashiki-warasi reauth         # bootstrap OAuth (opens a browser)
uv run zashiki-warasi sync-notion    # one-shot Notion→DB pull
```

Production is the containerized service driven by external cron —
see [Deploy](#deploy).

On the first `tick` (whether via CLI, HTTP, or cron) the poller
fetches the current `historyId` as a baseline — backlog is **not**
processed; only messages arriving from that point onwards are picked
up.

#### Starting from a clean slate

```bash
uv run zashiki-warasi run-legacy --reset -y   # hidden subcommand: TRUNCATE + one tick
```

`--reset` `TRUNCATE`s every domain table (`gmail_sync_state`,
`processed_messages`, `email_analyses`, `expenses`) **and** every
LangGraph checkpoint table (`checkpoints`, `checkpoint_writes`,
`checkpoint_blobs`, `checkpoint_migrations`) before booting the
poller. After the reset the poller behaves exactly as on first run:
re-baselines the Gmail `historyId`, skips backlog, and starts
collecting from that moment forward.

The Notion mirror is **not** touched — any rows previously written
to Notion stay there. If you want a matching wipe, delete them
manually in the Notion UI.

#### Notion → DB reverse sync

Edits the user makes in the Notion expense database are pulled back
into Postgres so the local store stays the source of truth even after
manual corrections (e.g. fixing an LLM-misextracted vendor / amount).

- The puller runs as a background thread inside the poller; cadence
  is `NOTION_SYNC_INTERVAL_SECONDS` (default `300`s, `0` disables).
- Only pages stamped with the `auto generated by zashiki-warasi`
  marker in the 備註 column are touched — manually-added Notion rows
  are ignored.
- Syncable fields: `消費名稱`, `消費店家`, `消費金額`, `幣別`,
  `消費日期`, `消費類別`, `支付方式`. `UUID` and `備註` are immutable.
- Conflict policy: **Notion wins** (latest-write-wins). The assumption
  is that you edit Notion when the LLM extracted something wrong.

One-shot manual sync (e.g. for debugging or after `NOTION_SYNC_INTERVAL_SECONDS=0`):

```bash
uv run zashiki-warasi sync-notion
```

## Deploy

v1.0 ships two deploy modes on top of the same container image, both
assuming an **external** Postgres reachable via `DATABASE_URL`. The
built-in daemon loop is gone — cadence is owned by an external cron
(host `crontab`) or a k8s `CronJob` calling `POST /poll` on the
service.

Upgrading from v0.6.x: see [`MIGRATION-v1.md`](MIGRATION-v1.md).

### Docker Compose (single-node homelab)

```bash
cd deploy/compose
cp .env.example .env                                 # fill DATABASE_URL, HTTP_API_KEY, OAUTH_REDIRECT_URI, …
mkdir -p secrets data
cp /path/to/credentials.json secrets/credentials.json
docker compose up -d --build
curl http://127.0.0.1:8080/healthz                   # expect 200
crontab -e                                           # paste from crontab.example
```

Full quickstart: [`deploy/compose/README.md`](deploy/compose/README.md).

### Helm (k8s)

```bash
helm install zashiki ./deploy/helm/zashiki-warasi -f values.local.yaml
kubectl port-forward svc/zashiki-zashiki-warasi 8080:8080
curl http://127.0.0.1:8080/healthz
```

Values shape is in [`deploy/helm/zashiki-warasi/values.yaml`](deploy/helm/zashiki-warasi/values.yaml).
`replicaCount` defaults to `1` but the app's Postgres-backed
coordination (advisory-lock tick single-flight + shared OAuth flow
store) makes any positive value safe — bump it when the WebUI epic
lands and read traffic wants HA. For `replicaCount > 1` on a
multi-node cluster you must also set `persistence.accessMode:
ReadWriteMany` AND have an RWX-capable storage class (Longhorn
share-manager, NFS, CephFS, ...). The cron cadence is a `CronJob`
spawning a `curlimages/curl` pod every minute (`* * * * *` by
default, `concurrencyPolicy: Forbid`).

### Smoke-test lessons

Everything caught during v1.0 Proxmox docker + k3s Helm smoke
(14 issues, categorised by symptom → root cause → fix → prevention)
is in [`docs/v1.0-smoke-lessons.md`](docs/v1.0-smoke-lessons.md)
(中文版 [`v1.0-smoke-lessons.zh.md`](docs/v1.0-smoke-lessons.zh.md)).
Where the shipped v1.0 deviates from what the original design
specified is in [`docs/v1.0-implementation-notes.md`](docs/v1.0-implementation-notes.md)
(中文版 [`v1.0-implementation-notes.zh.md`](docs/v1.0-implementation-notes.zh.md)).
Read both before rolling a similar-scope migration yourself — the
things that break at deploy time, and the things whose implementation
drifts from paper design, are rarely the things unit tests catch.

## Reauth (headless)

OAuth token refresh in a container without an interactive terminal
on the host, via the FastAPI-brokered web flow:

```
# 1. From your workstation:
ssh -L 8080:localhost:8080 you@container-host      # loopback tunnel

# 2. Trigger the flow:
curl -X POST http://127.0.0.1:8080/reauth -H "X-API-Key: $HTTP_API_KEY"
# Response:
#   {"auth_url": "http://127.0.0.1:8080/auth/start?csrf=…", "expires_in": 600, "state": "…"}

# 3. Open the auth_url in your workstation's browser.
#    Google consent → redirect to /auth/callback → token.json updated
#    → the running Gmail client reloads its credentials → 200 OK.
```

The CLI `zashiki-warasi reauth` (`InstalledAppFlow.run_local_server()`)
also still works for local dev.

Redirect URI setup (which URI to register in the Google Cloud
Console, and how to pick between loopback / Cloudflare Tunnel /
public domain) is in [`docs/oauth-redirect-uri.md`](docs/oauth-redirect-uri.md).

With `replicaCount > 1`, the callback may land on a different replica
than the one that served `/reauth` — the Postgres-backed flow store
makes this transparent.

## Configuration

All settings come from environment variables (a `.env` file in the
project root is supported via `pydantic-settings`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://localhost/zashiki_warasi` | Postgres connection string. Consumed by both the SQLAlchemy engine (app tables) and the LangGraph checkpointer connection pool (below) — same DB, two distinct client stacks |
| `DATABASE_CHECKPOINTER_POOL_MIN_SIZE` | `1` | Connections kept warm in the checkpointer pool at all times |
| `DATABASE_CHECKPOINTER_POOL_MAX_SIZE` | `5` | Hard cap on concurrent checkpointer connections. Must be `>= MIN_SIZE` |
| `DATABASE_CHECKPOINTER_POOL_MAX_LIFETIME_SECONDS` | `1800` | Force-recycle checkpointer connections older than this. Matches the SQLAlchemy engine's `pool_recycle` for consistency |
| `DATABASE_CHECKPOINTER_POOL_MAX_IDLE_SECONDS` | `600` | Recycle idle checkpointer connections. Sized below typical NAT eviction windows (5-15 min) so the pool retires the connection before the router silently drops it |
| `GMAIL_CREDENTIALS_PATH` | `credentials.json` | OAuth client secrets JSON |
| `GMAIL_TOKEN_PATH` | `~/.config/zashiki-warasi/token.json` | Cached user token |
| `GMAIL_SCOPES` | `https://www.googleapis.com/auth/gmail.readonly` | Comma-separated OAuth scopes |
| `GMAIL_HTTP_TIMEOUT_SECONDS` | `60` | Per-request socket timeout; prevents dead-connection blocking of the poller loop |
| `LLM_PROVIDER` | `llamacpp` | One of `llamacpp`, `openai`, `anthropic` |
| `LLM_BASE_URL` | `http://localhost:8080/v1` | OpenAI-compatible endpoint (used by `llamacpp` and `openai`) |
| `LLM_API_KEY` | `not-needed` | API key for the provider |
| `LLM_MODEL` | `local-model` | Model identifier passed to the provider |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `NOTION_TOKEN` | _(empty)_ | Internal integration token (`secret_…`); empty disables Notion |
| `NOTION_EXPENSE_DATABASE_ID` | _(empty)_ | UUID of the target Notion database |
| `NOTION_TIMEOUT_SECONDS` | `10.0` | Notion API request timeout |
| `NOTION_SYNC_INTERVAL_SECONDS` | `300` | Background Notion→DB sync cadence; `0` disables the puller thread |
| `LOG_LEVEL` | `INFO` | Root logger level. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive) |
| `LOG_LEVEL_ZASHIKI` | _(inherit)_ | Level for our `zashiki_warasi.*` tree only — flip DEBUG on our code without unmuting httpx / google.auth / openai chatter |
| `HTTP_BIND_HOST` | `127.0.0.1` | FastAPI listener bind address. Container defaults to `0.0.0.0` |
| `HTTP_BIND_PORT` | `8080` | FastAPI listener port |
| `HTTP_API_KEY` | _(empty)_ | Optional shared secret. When set, `POST /poll` and `POST /reauth` require `X-API-Key`. **Required** whenever `HTTP_BIND_HOST != 127.0.0.1` — the app refuses to start if the combination is unauthenticated-non-loopback |
| `OAUTH_REDIRECT_URI` | _(unset)_ | Absolute URL Google redirects to after consent. Required for the headless `POST /reauth` flow; unused by CLI `reauth`. See [`docs/oauth-redirect-uri.md`](docs/oauth-redirect-uri.md) |

**Removed in v1.0** (silently ignored on startup with a one-time INFO
log): `POLLER_INTERVAL_SECONDS`, `POLLER_HEARTBEAT_INTERVAL_SECONDS`.
Cadence is owned by the external scheduler; liveness is `/healthz` +
the scheduler's own logs.

Switching to `anthropic` additionally requires `uv add langchain-anthropic`.

## Logging

Every message-processing log line carries a `message_id=<gmail_id>`
context field so `grep` follows one email's entire lifecycle across
`email_agent`, the expense subgraph, and the notification sinks:

```
$ zashiki-warasi 2>&1 | grep 'message_id=17c8f1a9b3d4e5f6'
2026-07-28T09:12:34+0000 INFO  zashiki_warasi.agents.email_agent[message_id=17c8f1a9b3d4e5f6]: classified as 消費支出
2026-07-28T09:12:34+0000 INFO  zashiki_warasi.agents.email_agent[message_id=17c8f1a9b3d4e5f6]: routing to expense (category=消費支出)
2026-07-28T09:12:35+0000 INFO  zashiki_warasi.agents.verticals.expense[message_id=17c8f1a9b3d4e5f6]: expense: extracted vendor=SEVEN-ELEVEN amount=803 currency=JPY
2026-07-28T09:12:35+0000 INFO  zashiki_warasi.agents.verticals.expense[message_id=17c8f1a9b3d4e5f6]: expense: persisted record 42
2026-07-28T09:12:36+0000 INFO  zashiki_warasi.agents.email_agent[message_id=17c8f1a9b3d4e5f6]: notified user
```

### Level policy

| Level | What you'll see | When to use |
| --- | --- | --- |
| `DEBUG` | Per-node entry/exit + `elapsed_ms`, decision inputs, tick heartbeats (`tick: 0 new`) | Interactive debugging session, incident triage. Do NOT leave on in production — DEBUG on `zashiki_warasi.*` is chatty (dozens of lines per message) |
| `INFO` | State transitions worth knowing about after the fact: classified, routed, extracted, persisted, notified, tick advanced, credentials refreshed, checkpointer pool opened/closed, OAuth reauth initiated / completed, HTTP request-id stamped on every record inside a `POST /poll` / `POST /reauth` handler. Liveness signal is external in v1.0 (cron log + `/healthz`) — no in-process heartbeat | Default steady-state cadence |
| `WARNING` | Recoverable anomalies: HistoryExpiredError rebaseline, Notion write failure marked on the row, LLM token limit hit (falls back to `AnalysisFailed`), checkpointer pool discards a stale connection | Should surface even in quiet operation — worth glancing at daily |
| `ERROR` | A specific unit of work failed and was abandoned; poller keeps running | Always visible; typically indicates a bug or bad data |
| `CRITICAL` | Process cannot continue without operator action: `CredentialRefreshError` (exit `78`), checkpointer pool exhausted its reconnect budget (exit `71`) | Always visible |

### Two knobs, one common recipe

- **Quiet steady state, loud on our code when I need it:**
  `LOG_LEVEL=INFO LOG_LEVEL_ZASHIKI=DEBUG` — our per-node traces
  visible, third-party (`httpx`, `google.auth`, `openai._base_client`,
  etc.) muted to WARNING regardless.
- **Everything quiet except real anomalies:**
  `LOG_LEVEL=WARNING` — poller boots silently, only WARN+ surfaces.
- **Full firehose:**
  `LOG_LEVEL=DEBUG` — DEBUG on everything we own. The hard-coded
  WARNING pins on `httpx` / `httpcore` / `urllib3` / `google.auth` /
  `google_auth_httplib2` / `openai._base_client` / `httpx._client` /
  `psycopg.pool` stay in effect (they would otherwise dominate the
  output during a Gmail history burst). To unmute one of those for
  a specific investigation, override with e.g.
  `python -c "import logging; logging.getLogger('httpx').setLevel(logging.DEBUG)"`
  in a shell before importing the app — or add a targeted env knob
  in a follow-up change.

**Grep recipes:**

- `grep 'message_id=<gmail_id>'` — one email's full lifecycle across
  modules (shown above).
- `grep 'checkpointer pool'` — full lifetime of the LangGraph
  checkpointer pool: `pool opened (…)` → any `discarded stale
  connection` events → `pool closed`. Useful when investigating DB
  hiccups on an incident timeline.
- `grep 'node=' | grep 'exit '` — per-node timing across the graph
  (requires DEBUG on `zashiki_warasi.*`).
- `grep 'request_id=<value>'` — full lifecycle of one FastAPI request
  across the HTTP handler, tick body, LLM/Gmail/DB calls. The value
  comes from either the client-supplied `X-Request-ID` header or a
  fresh `uuid.hex[:12]` the middleware generates; it's also echoed
  back on the response header.
- Liveness check: no `poller alive` heartbeat in v1.0 — instead
  probe `/healthz` (200 iff DB reachable AND OAuth token
  refreshable) or grep the cron log for `200` responses at expected
  cadence. `grep -v ' 200$' /var/log/zashiki-cron.log` surfaces every
  non-success including single-flight `409`s.

An unknown level (`LOG_LEVEL=verbose`) fails the process at startup
with a clear error — silent fallback to `INFO` would let you believe
DEBUG was on when it wasn't.

## Project layout

```
src/zashiki_warasi/
├── app.py                       # CLI entry (uv run zashiki-warasi) — click group.
│                                # Subcommands: serve (uvicorn), tick (one-off
│                                # tick_once + JSON), reauth, sync-notion.
├── web/                         # FastAPI service — `uvicorn zashiki_warasi.web:app`
│   ├── app.py                   # application factory + lifespan (build_services)
│   ├── dependencies/            # get_services + require_api_key
│   ├── middleware/              # request-id (honors X-Request-ID header)
│   └── routers/                 # health.py, poll.py, auth.py, reauth.py
├── coordination/                # Cross-replica primitives (Postgres-backed)
│   ├── advisory_lock.py         # pg_try_advisory_lock context manager for
│   │                            # POST /poll single-flight (session-scoped)
│   └── oauth_flow_store.py      # oauth_flows table wrapper for OAuth
│                                # web-flow state (survives cross-replica callback)
├── core/
│   ├── config.py                # BaseSettings: Http / OAuth / Gmail / Database /
│   │                            # LLM / Telegram / Notion / Logging / Poller
│   ├── services.py              # Services dataclass + build_services() —
│   │                            # single-shot bootstrap for CLI + FastAPI lifespan
│   ├── db.py                    # SQLAlchemy engine + session factory
│   ├── logging.py               # ContextFormatter + LoggerAdapter helpers;
│   │                            # request_id + message_id contextvars
│   ├── models.py                # ORM: GmailSyncState, ProcessedMessage,
│   │                            # EmailAnalysis, ExpenseRecord, NotionSyncState
│   └── schemas.py               # Pydantic: EmailMessage, TickResult,
│                                # EmailAnalysis, ExpenseDraft/Logged/NeedsReview,
│                                # AnalysisFailed, SideEffect discriminated union
├── gmail/
│   ├── auth.py                  # OAuth Installed App flow (CLI reauth path)
│   ├── client.py                # Gmail API wrapper + reload_credentials()
│   ├── exceptions.py            # GmailError hierarchy
│   └── poller.py                # tick_once(services.poller) + Poller helpers
├── agents/
│   ├── llm.py                   # Chat model factory (provider-agnostic)
│   ├── email_agent.py           # Analyze + router; catches
│   │                            # LengthFinishReasonError / BadRequestError
│   │                            # → AnalysisFailed
│   └── verticals/
│       ├── expense.py           # ExpenseSubgraph: extract → persist → Notion
│       ├── pdf.py               # pdfplumber wrapper + collect_text(body + PDFs)
│       └── html_text.py         # html2text wrapper for HTML-only mails
└── notifications/
    ├── telegram.py              # TelegramNotifier
    ├── notion.py                # NotionExpenseRecorder (write side)
    └── notion_puller.py         # NotionExpensePuller (background reverse-sync)

alembic/versions/                # domain migrations (LangGraph tables self-init;
                                 # oauth_flows table also idempotently created
                                 # by build_services())
Dockerfile                       # multi-stage uv build (python:3.13-slim-bookworm,
                                 # non-root uid 10001)
docker-entrypoint.sh             # `alembic upgrade head` → exec CMD (uvicorn)
.gitlab-ci.yml                   # build + push to gitlab.davidho.dev:5050
deploy/
  compose/                       # docker-compose.yml + .env.example + crontab.example
  helm/zashiki-warasi/           # Helm chart (Deployment / Service / ConfigMap /
                                 # Secret / PVC / CronJob / Ingress), init container
                                 # for token seeding
docs/
  oauth-redirect-uri.md          # OAuth strategies (SSH tunnel / tunnel / domain)
  v1.0-smoke-lessons.md          # 14 issues caught during v1.0 smoke (+.zh.md)
scripts/check_env_parity.py      # CI-runnable compose ↔ Helm env drift check
MIGRATION-v1.md                  # v0.6.x → v1.0.0 upgrade path
tests/                           # Pytest scaffolding — 559 tests as of v1.0
```

## How crash recovery works

- **Process dies between fetching a message and the LLM finishing.** The
  message ID is not in `processed_messages`, so the next tick re-emits
  it. LangGraph sees an existing checkpoint for `thread_id=<message_id>`
  and resumes from the last completed node — no duplicate LLM call.
- **Process dies after the LLM but before persistence.** Same path; the
  cached checkpoint returns the prior analysis instantly, and an
  existence check on `email_analyses` skips the redundant insert.
- **Process is offline longer than Gmail's history retention (~7 days).**
  `history.list` returns 404; the poller catches `HistoryExpiredError`
  and re-baselines from the current `historyId` (backlog is skipped, as
  on first run).
- **Idle network connections killed by NAT / Postgres server timeout.**
  Both the Gmail HTTP client and the SQLAlchemy engine are hardened for
  overnight idle: Gmail requests carry an explicit 60 s socket timeout
  (`GMAIL_HTTP_TIMEOUT_SECONDS`) so a half-open TCP doesn't stall the
  loop for the ~13 min OS-level RTO; the DB engine uses
  `pool_pre_ping=True` + `pool_recycle=1800` so a connection killed by
  a router NAT eviction is detected on checkout and replaced instead of
  crashing the next query.
- **LangGraph checkpointer connection dropped mid-run.** The
  checkpointer's Postgres connection is wired through a
  `psycopg_pool.ConnectionPool` (env-tunable via
  `DATABASE_CHECKPOINTER_POOL_*`, see Configuration). Every checkout
  runs a `SELECT 1` health check; a stale connection is discarded
  and a fresh one provisioned, logged as one WARNING
  (`checkpointer pool: discarded stale connection, reconnecting`) —
  no manual restart, no missed history batch beyond the tick that
  was in flight. If the DB stays unreachable for longer than the
  pool's `reconnect_timeout` (default 300 s), the pool's
  `reconnect_failed` callback logs CRITICAL and the process exits
  with code `71` (`EX_OSERR`) so `restart: on-failure` in Docker /
  systemd kicks in without our own retry loop competing.
- **Gmail OAuth refresh token expired or revoked.** A `RefreshError`
  from Google's auth library is unrecoverable in-process — retrying the
  same refresh will always fail and would just hammer the token
  endpoint until Google rate-limits us. The poller (or startup path)
  catches it, sends a Telegram alert naming the failure, and exits with
  code `78` (`EX_CONFIG`). Recover with `zashiki-warasi reauth`, then
  restart. Container orchestrators using `restart: on-failure` will
  keep restarting the process, but each restart will land on the same
  bad token and exit immediately — the alert is the actionable signal,
  not the log spam.
- **Service unreachable / cron stopped firing** (was: "poller loop
  silently stops" in v0.6.x). v1.0 removes the in-process daemon
  loop, so this failure mode is now covered by three independent
  external signals — no in-process heartbeat needed:
  - The container-orchestrator status (`docker compose ps` /
    `kubectl get pod`) reflects `restarting` / `CrashLoopBackOff`
    for a crashed process.
  - `/healthz` returns 503 with per-check booleans (`db` / `oauth`).
    Wire it into the compose `healthcheck:` block, the k8s
    `livenessProbe`, and any external monitor (Uptime Kuma etc.).
  - The external scheduler's own log. Cron logs a `200` per
    successful `POST /poll`; missing lines OR non-2xx codes are
    the "loop stopped" signal. `grep -v ' 200$'
    /var/log/zashiki-cron.log` surfaces every anomaly including
    single-flight `409`s.

  Host suspension that stopped v0.6.x for hours (macOS App Nap,
  unattended sleep) can't hide from these signals — the scheduler
  and the probe run on infrastructure independent of the Python
  process.

## How LLM analyze failures are handled

The analyze node can hit two distinct hard failures around context
size — same underlying "mail too big" family, but the LLM surfaces
them as different exception classes and each needs its own catch:

**Output truncation** (`openai.LengthFinishReasonError`, `finish_reason=length`).
Server returns HTTP 200 with a partial JSON that never closed. Often
a symptom of degenerate generation on a local model rather than the
mail itself being enormous — see the `LLM_ANALYZE_MAX_TOKENS` cap.

**Input overflow** (`openai.BadRequestError`, HTTP 400 with an
`exceed_context_size_error` / `context_length_exceeded` signature).
Server rejects the request before generating a single token because
the prompt (system prompt + email body) alone exceeds the model's
context window. Typical trigger: a giant HTML newsletter with inline
base64 images that `html_to_text` didn't strip, a long forum thread
with quote piles, or an auto-generated content dump.

Both cases were previously uncaught — the exception escaped the
LangGraph invoke, the poller logged `Unhandled error during tick;
will retry`, and the same doomed call re-fired every 30 s until
Gmail's history retention rolled the message off. Meanwhile no other
emails got processed.

The related `/healthz` endpoint is orthogonal to these LLM failures:
it reflects **DB + OAuth token readiness**, not LLM reachability. A
Telegram `⚠️ LLM 分析失敗` message means the LLM is broken or the
mail is oversized; `/healthz` will still return 200 in that case
(unless the DB or OAuth token is also unhealthy).

`_analyze` now catches both and returns an `AnalysisFailed`
side-effect with a distinct `reason` variant (`content_too_long` for
the output case, `prompt_too_long` for the input case). The graph
completes normally, `notify` sends a Telegram message that names the
failure mode so the operator's remediation is unambiguous:

```
⚠️ LLM 分析失敗

標題: <the offending mail's subject>
寄件者: <sender>

原因: 郵件內容超出 LLM 上下文視窗 (HTTP 400),無法送出分析請求。
用量: prompt=58245 n_ctx=32768

→ 請打開原信手動處理。
```

No placeholder row lands in `email_analyses` — analyze failures
don't pollute the analytics table. The message id is still recorded
in `processed_messages`, so the poller does NOT re-fire the same
doomed request on the next tick.

Non-context-length 400s (bad auth, invalid tool spec, schema
violations) are **NOT** swallowed as `AnalysisFailed` — those are
real bugs that must surface, so they propagate to the poller's outer
`except Exception` where they show up as `Unhandled error during
tick`. The catch's heuristic is narrow: matches only on
`error.type == "exceed_context_size_error"` (llama-cpp-server),
`error.code == "context_length_exceeded"` (OpenAI), or a message
text containing both `"context"` and a length/size/window word.

## How the analyze prompt separates real transactions from boilerplate

Bank / card / cloud-service notification emails routinely include a
disclaimer line at the bottom about a hypothetical fee — for example
SMBC Olive デビット's `ご利用のお知らせ` mail carries a single real
transaction block

```
◇利用日  : 2026/07/03 09:43:03
◇利用先  : SEVEN-ELEVEN
◇利用金額: 280円
◇承認番号: 498134
```

followed by

```
※海外ATMでの現地通貨の引き出しは上記金額にATM利用手数料110円を
加えて引き落とし致します。
```

The 110 is a hypothetical fee for an ATM withdrawal that didn't
happen, not a second transaction. A naïve read of the mail sees
two amounts, decides "this is a multi-transaction digest", and
misclassifies it as `消費資訊彙整` — which does **not** route
into the expense subgraph, so no `ExpenseRecord` gets persisted
and the phantom 110 leaks into the Telegram summary.

The analyze prompt makes this distinction explicit:

- **Summary rule:** a line only counts as a transaction if it has
  date + vendor + amount all three present. Fee disclaimers,
  hypothetical scenarios, and boilerplate promo asides are
  excluded from the summary regardless of what numbers they
  contain.
- **Classification rule:** `消費資訊彙整` requires **multiple**
  real transactions in the mail. One real transaction plus any
  amount of boilerplate stays `消費支出` and gets routed to the
  expense subgraph.

The expense extraction prompt carries the same guard as rule 10 —
so even if the classification is ever wrong, the extractor won't
pull `amount=110` from a fee disclaimer either. Both guards are
pinned by regression tests that assert the concrete SMBC Olive
example survives future prompt rewrites.

## How expense deduplication works

A single real-world purchase commonly produces more than one email —
for example a credit-card authorisation notice plus the merchant's own
receipt arriving seconds apart with no shared identifier. Before
inserting a new row, the expense subgraph runs `find_duplicate(draft,
session)` against the existing `expenses` table:

**Stage 1 — real `transaction_id` collision.** If `draft.transaction_id`
matches an existing row, that row is treated as the same transaction.
Auto-generated IDs (prefix `AUTO-`) are excluded here because they are
derived per-email and cannot legitimately coincide across distinct
emails.

**Stage 2 — amount + currency + ±15-minute window.** If Stage 1 produced
no match, the subgraph looks for an existing row with the same
`amount`, the same `currency`, and `transacted_at` within ±15 minutes.
Vendor name is intentionally **not** part of the match because
cross-system emails use different strings for the same merchant
(`STARBUCKS MOBILE ORDER` from SMBC Olive vs.
`スターバックス コーヒー Olive LOUNGE 渋谷店` from the merchant itself).
If more than one existing row falls inside the window the subgraph
gives up and inserts the new email as a distinct record — it would
rather keep one spurious duplicate than collapse two real transactions
into one.

When `find_duplicate` returns a row, persist_node skips the `INSERT`,
emits an `ExpenseLogged` SideEffect pointing at the existing
`record_id`, and writes a log line:

```
expense: msg-<id> matches existing record <uuid>
    (duplicate transaction) → skip persist
```

The Telegram notification still goes out, but it carries the original
record's fields. The follow-up email is not annotated as a duplicate
in the message itself — surfacing that is left for a future iteration.

**Known limitations.** Long-range duplicates (Amazon "order confirmed"
plus "shipped" hours or days later) are intentionally **not**
deduplicated. Widening the time window beyond 15 minutes starts
collapsing routine recurring purchases — e.g. three identical coffee
runs in the same day — into a single record. Stage 2 also requires all
three signals (`amount`, `currency`, `transacted_at`); a draft missing
any of them skips Stage 2 and is persisted as new.

## License

See [LICENSE](./LICENSE).
