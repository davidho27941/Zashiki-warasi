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
createdb zashiki_warasi
uv run alembic upgrade head
```

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

### 5. Run

```bash
uv run zashiki-warasi
```

On startup the poller fetches the current `historyId` as a baseline —
backlog is **not** processed; only messages arriving from that point
onwards are picked up. Polling runs at 30-second intervals by default.

## Configuration

All settings come from environment variables (a `.env` file in the
project root is supported via `pydantic-settings`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://localhost/zashiki_warasi` | SQLAlchemy connection string |
| `GMAIL_CREDENTIALS_PATH` | `credentials.json` | OAuth client secrets JSON |
| `GMAIL_TOKEN_PATH` | `~/.config/zashiki-warasi/token.json` | Cached user token |
| `GMAIL_SCOPES` | `https://www.googleapis.com/auth/gmail.readonly` | Comma-separated OAuth scopes |
| `LLM_PROVIDER` | `llamacpp` | One of `llamacpp`, `openai`, `anthropic` |
| `LLM_BASE_URL` | `http://localhost:8080/v1` | OpenAI-compatible endpoint (used by `llamacpp` and `openai`) |
| `LLM_API_KEY` | `not-needed` | API key for the provider |
| `LLM_MODEL` | `local-model` | Model identifier passed to the provider |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |

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

## License

See [LICENSE](./LICENSE).
