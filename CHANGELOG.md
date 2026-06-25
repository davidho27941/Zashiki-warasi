# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Telegram notification node** in the email agent graph:
  `analyze -> notify -> END`. Every analysed email produces a Telegram
  message containing icon + importance + category + sender + subject +
  summary (HTML formatted, all user-controlled fields escaped). The
  notify node fails closed — if the Telegram API is unreachable, the
  analyze result stays in the LangGraph checkpoint and the next tick
  resumes at notify without re-billing the LLM.
- `notifications/telegram.py`: `TelegramNotifier` wrapping the Bot
  API `sendMessage` endpoint, with explicit `TelegramError` for
  transport failures, non-2xx responses, and `ok: false` API
  rejections.
- `TelegramSettings` (env prefix `TELEGRAM_`): bot_token, chat_id,
  api_base, timeout_seconds.
- `agents/tools/registry.py`: `ToolRegistry` for `langchain_core.tools.BaseTool`
  instances. Supports `register` (also usable as a decorator),
  `get`, `all`, `names`, plus `in` / `len` / iteration. Rejects
  duplicate names and non-BaseTool inputs with actionable error
  messages. Module-level `default_registry` provided for code that
  wants a shared instance. Subgraph-as-tool patterns are explicitly
  out of scope — those stay with the agent that composes them.
- `httpx` pinned as an explicit direct dependency
  (previously transitive via `langchain-openai`).

### Changed
- `EmailAgent.__init__` now requires a `notifier: TelegramNotifier`.
- `EmailAgent.handle_email` is now properly idempotent across
  re-invocations: it inspects `graph.get_state(config)` first and
  reuses the cached analysis when the thread is already complete,
  so a second call (e.g. from the poller after an unrelated crash)
  no longer re-bills the LLM or re-sends Telegram. Interrupted
  threads are resumed via `invoke(None, config)`.

### Tests
- 36 new tests (169 total):
  - `tests/notifications/test_telegram.py` (14): construction
    guards, URL / payload shape, parse_mode handling, timeout
    pass-through, and error mapping for transport / HTTP /
    `ok: false` cases.
  - `tests/agents/test_email_agent.py::TestNotifyNode` (7): notify
    invoked exactly once, message contents, HTML escaping of
    untrusted fields, fail-closed behaviour blocking persistence,
    second-call idempotency, ordering relative to analyze.
  - `tests/agents/tools/test_registry.py` (15): register /
    decorator usage / duplicate guard / non-BaseTool guard /
    lookup / `all()` snapshot semantics / dunder methods /
    `default_registry` presence.

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
