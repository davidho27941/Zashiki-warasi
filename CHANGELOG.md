# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-25

First public preview. End-to-end verified against a personal Gmail
account and a self-hosted llama.cpp server (Gemma 4 26B A4B-IT, Q4_K_XL).

### Added

#### Gmail polling
- OAuth 2.0 Installed App flow with cached token refresh
  (`gmail/auth.py`); first run opens a browser, subsequent runs use
  the cached refresh token at `~/.config/zashiki-warasi/token.json`
  (chmod 600).
- `GmailClient` wrapping the Gmail v1 REST API
  (`gmail/client.py`): `get_profile`, `get_message`,
  `get_attachment`, and a paginating `list_history` generator.
- MIME payload parser: DFS over the parts tree, base64url decode
  with charset detection, address parsing via `email.utils`.
- `Poller` driven by Gmail's `historyId` cursor (`gmail/poller.py`)
  with four explicit branches:
  - **A** first-run baseline (skips backlog),
  - **B** resume from `gmail_sync_state` on restart,
  - **C** re-baseline when `history.list` returns 404 (cursor older
    than Gmail's ~7-day retention),
  - **D** normal tick with D1 dedup-skip, D2 deleted-message
    handling, D3 success path.
- Per-message dedup in `processed_messages` plus per-tick cursor
  advance so a crash mid-batch cannot lose or duplicate messages.

#### Agent
- LangGraph `EmailAgent` (`agents/email_agent.py`) with a single
  `analyze` node producing structured `EmailAnalysis`
  (`category`, `importance`, `summary`).
- `PostgresSaver` checkpointer keyed by `thread_id=email.id` so an
  interrupted run resumes from the last completed node instead of
  re-billing the LLM.
- Provider-agnostic chat-model factory (`agents/llm.py`): supports
  `llamacpp` / `openai` (both via `ChatOpenAI` with a configurable
  `base_url`) and `anthropic` (lazy-imported with a friendly error
  when `langchain-anthropic` is missing).
- Analysis result persisted to `email_analyses` with an existence
  check, so a re-invocation never inserts duplicate rows.

#### Storage
- SQLAlchemy 2.0 ORM models (`core/models.py`):
  `GmailSyncState`, `ProcessedMessage`, `EmailAnalysis`.
- Engine / session-factory singletons (`core/db.py`).
- Alembic configured to read `DATABASE_URL` via
  `DatabaseSettings`; two initial migrations create the
  domain tables. LangGraph's checkpoint tables are created
  separately by `PostgresSaver.setup()` at startup.

#### Configuration
- pydantic-settings classes (`core/config.py`):
  `GmailSettings` (`GMAIL_*` env vars),
  `DatabaseSettings` (`DATABASE_URL`),
  `LLMSettings` (`LLM_*` env vars). `.env` file supported.
- `.env.example` documenting every recognised variable.

#### Application
- Console script `zashiki-warasi` mapped to `app.run`, which wires
  credentials → `GmailClient` → `EmailAgent` → `Poller` and blocks
  on the polling loop.
- Graceful shutdown via SIGINT / SIGTERM
  (`_install_shutdown_handlers`): the message currently in flight
  finishes (its `processed_messages` row is written) before the
  process exits. Pressing Ctrl+C twice restores Python's default
  handler and raises `KeyboardInterrupt` for a hard exit.
- `Poller.stop_event` exposed publicly so external coordinators
  (signal handlers, tests) can request a clean stop without
  reaching into private state.

#### Tests
- 133 pytest tests covering: every settings class and validator,
  payload parsing, Gmail API surface (with `googleapiclient` mocked),
  OAuth flow paths, engine/session-factory singletons, LLM factory
  provider switching, all four polling branches, agent persistence
  and idempotency, signal-handler behaviour, and the SQLAlchemy /
  libpq URL helper.

#### Documentation
- `README.md` with setup, configuration table, architecture diagram,
  and crash-recovery semantics.

### Fixed
- `GmailClient.get_attachment` now appends `"=="` before
  `urlsafe_b64decode`, matching `_decode_body`. Gmail strips trailing
  `"="` from base64url payloads; without re-padding, attachments
  whose raw byte length was not a multiple of 3 raised
  `binascii.Error: Incorrect padding`. Caught by the new
  `tests/gmail/test_client_api.py` suite.
- `GMAIL_SCOPES` env-var decoding: comma-separated strings reaching
  `pydantic-settings` were being JSON-decoded before our validator
  ran. Switched the field to `Annotated[list[str], NoDecode]`.

### Known limitations
- Single Gmail account per process (the schema is multi-account
  ready, but `Poller` and `app.py` assume one).
- Re-baseline on history expiry drops the backlog in that window;
  no `messages.list q="after:..."` fallback yet.
- No max-retry / dead-letter for persistently failing messages —
  they retry every tick until an operator intervenes.
- Agent has no tool calls; HTML body and attachment bytes are not
  consulted even when `body_plain` is empty (snippet is used as
  fallback).
- `psycopg[binary]` pinned to `>=3.2,<3.3` to avoid a SQLAlchemy
  dialect crash on `_get_server_version_info`. Revisit when an
  upstream fix lands.

[Unreleased]: https://github.com/davidho27941/Zashiki-warasi/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/davidho27941/Zashiki-warasi/releases/tag/v0.1.0
