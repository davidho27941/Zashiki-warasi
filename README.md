# Zashiki-warasi

A self-hosted Gmail polling agent that classifies and summarises incoming
mail with an LLM. Built on [LangGraph](https://github.com/langchain-ai/langgraph),
[pydantic-settings](https://docs.pydantic.dev/latest/), and Postgres for
durable state.

The name comes from [座敷童子](https://en.wikipedia.org/wiki/Zashiki-warashi),
a household spirit said to quietly bring fortune to the home it lives in —
which is roughly what an email agent that watches your inbox is supposed to do.

## What it does

1. Polls your Gmail account using the `historyId` cursor (incremental, no
   re-fetching).
2. For each new message, asks an LLM to produce a structured
   `{ category, importance, summary }` analysis.
3. Persists the analysis to Postgres, deduplicated by message ID.

Crash-safe by design: per-message dedup plus LangGraph checkpoints (keyed by
Gmail message ID) mean a restart never loses or re-bills a message, even if
the process dies mid-LLM-call.

**Body source for the LLM**: `text/plain` is preferred when present
(cleanest, no markup noise). HTML-only emails (modern e-receipts,
newsletters) fall back to a stripped-down conversion via `html2text`
so the LLM sees the full content instead of just Gmail's 200-char
snippet. The snippet is the last-resort fallback when neither plain
nor HTML is available.

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
                  │ EmailAgent (LangGraph: analyze node) │──▶ chat model
                  └────────────────┬─────────────────────┘    (llama.cpp /
                                   │                           OpenAI / ...)
                                   │ EmailAnalysis
                                   ▼
            Postgres ── gmail_sync_state      (cursor)
                     ── processed_messages    (dedup)
                     ── email_analyses        (LLM output)
                     ── checkpoints, …        (LangGraph state)
```

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- PostgreSQL (local or remote)
- An LLM endpoint. Defaults assume [llama.cpp](https://github.com/ggerganov/llama.cpp)
  running locally with `llama-server` on port 8080.
- A Google Cloud OAuth 2.0 Client ID (Desktop type) for Gmail access.

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
3. The first run will open a browser for one-time consent; the refresh
   token is then cached at `~/.config/zashiki-warasi/token.json`.

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

```bash
uv run zashiki-warasi
```

On startup the poller fetches the current `historyId` as a baseline —
backlog is **not** processed; only messages arriving from that point
onwards are picked up. Polling runs at 30-second intervals by default.

#### Starting from a clean slate

```bash
uv run zashiki-warasi --reset       # asks [y/N] first
uv run zashiki-warasi --reset -y    # skip the prompt
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

## Docker

A self-contained stack (app + Postgres) is provided for users without
an existing Postgres. Migrations are run automatically by the
entrypoint before the poller boots.

```bash
cp .env.example .env                                # fill in LLM / Telegram / Notion
mkdir credentials && cp /path/to/credentials.json credentials/   # OAuth client
docker compose up --build
```

First boot: the container needs to complete OAuth interactively.
Until `credentials/token.json` exists on the host, run the flow once
with stdin attached:

```bash
docker compose run --rm app
# follow the OAuth URL, paste the code; token.json gets cached in the
# named volume so subsequent runs are non-interactive.
```

CLI flags pass through `docker compose run`:

```bash
docker compose run --rm app --reset -y
docker compose run --rm app sync-notion
```

**Using your own Postgres** — don't `docker compose up`. Build the
image and `docker run` it directly with `DATABASE_URL` pointing at
your existing instance:

```bash
docker build -t zashiki-warasi .
docker run --rm -it \
  --env-file .env \
  -v "$PWD/credentials:/app/credentials:ro" \
  -v zashiki_token:/root/.config/zashiki-warasi \
  zashiki-warasi
```

## Configuration

All settings come from environment variables (a `.env` file in the
project root is supported via `pydantic-settings`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://localhost/zashiki_warasi` | SQLAlchemy connection string |
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

Switching to `anthropic` additionally requires `uv add langchain-anthropic`.

## Project layout

```
src/zashiki_warasi/
├── app.py              # entry point (uv run zashiki-warasi)
├── core/
│   ├── config.py       # GmailSettings / DatabaseSettings / LLMSettings
│   ├── db.py           # SQLAlchemy engine + session factory
│   ├── models.py       # ORM: GmailSyncState, ProcessedMessage, EmailAnalysis
│   └── schemas.py      # Pydantic: EmailMessage, AttachmentMeta, EmailAnalysis, …
├── gmail/
│   ├── auth.py         # OAuth Installed App flow
│   ├── client.py       # Gmail API wrapper (tool-call friendly)
│   ├── exceptions.py   # GmailError hierarchy
│   └── poller.py       # historyId-based polling loop
└── agents/
    ├── llm.py          # Chat model factory (provider-agnostic)
    └── email_agent.py  # LangGraph triage agent
alembic/                # Database migrations for domain tables
tests/                  # Pytest scaffolding
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
