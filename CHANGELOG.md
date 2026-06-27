# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### Telegram notifications and tool registry

- **Telegram notification node** in the email agent graph
  (`analyze -> notify -> END`). Every analysed email produces a
  Telegram message; the notify node fails closed so a Telegram
  outage keeps the analyze checkpoint and the next tick resumes
  at notify without re-billing the LLM.
- `notifications/telegram.py`: `TelegramNotifier` wrapping the Bot
  API `sendMessage` endpoint, with explicit `TelegramError` for
  transport failures, non-2xx responses, and `ok: false` API
  rejections.
- `TelegramSettings` (env prefix `TELEGRAM_`): bot_token, chat_id,
  api_base, timeout_seconds.
- `agents/tools/registry.py`: `ToolRegistry` for
  `langchain_core.tools.BaseTool` instances. Supports `register`
  (also usable as a decorator), `get`, `all`, `names`, plus `in` /
  `len` / iteration. Rejects duplicate names and non-BaseTool
  inputs with actionable error messages. Module-level
  `default_registry` provided. Subgraph-as-tool patterns are
  explicitly out of scope.
- `httpx` pinned as an explicit direct dependency.

#### Analyze redesign and expense vertical

- **Analyze node** rewritten to produce the full new
  `EmailAnalysis` schema: `importance` 1-5, `urgency`
  (very_urgent / urgent / normal / none), Chinese `category`
  (Literal of 13 values), 5W1H `summary` (50-200 字, explicitly
  excluding specific payment details), `keywords` (≤5). System
  prompt rewritten in Chinese per the product spec, including
  the importance scoring rules ("科技新知 ≥3", "促銷/廣告/信貸 ≤3")
  and the financial-product → 廣告/促銷 classification rule.
- **Expense vertical** as a LangGraph subgraph routed from analyze
  when `category == "消費支出"`:
  - `extract` builds combined context (email body + PDF
    attachment text), calls LLM with
    `with_structured_output(ExpenseDraft)`, **early-bails** to
    `ExpenseNeedsReview(image_pdf_unreadable)` when the only
    attachment is an unreadable PDF (does not hallucinate fields
    from sender / subject alone).
  - `persist` writes `ExpenseRecord` with the full draft JSON
    for audit, handles UNIQUE collision on `message_id` for
    crash-resume idempotency. All-null drafts route to
    `ExpenseNeedsReview(extraction_yielded_nulls)` instead of
    persisting.
- `ExpenseDraft` / `ExpenseLogged` / `ExpenseNeedsReview` pydantic
  models; `SideEffect` discriminated union for typed dispatch in
  notify.
- `PaymentMethod` Literal: 7 values (Rakuten Pay, SMBC Olive,
  三菱UFJ-JCB, PayPay, 信用卡, 現金, 其他). The prompt distinguishes
  null (信件未提及) from 其他 (提及但不在白名單).
- `agents/verticals/pdf.py`: `pdf_extract_text` (pdfplumber-backed)
  returns empty string on image-only / corrupt / encrypted PDFs;
  `collect_text` returns `(combined_text, unreadable_pdf_filenames)`
  so callers can route deterministically.
- `ExpenseRecord` ORM on the new `expenses` table.
- Alembic migrations `0003_analysis_v2.py` (drops +
  recreates `importance` as INTEGER, adds `urgency` + `keywords`)
  and `0004_expenses.py`.
- Notify formatter rewritten to the spec output template:
  importance stars + category header, 標題 / 寄件者 / 內容摘要 /
  急迫性, per-side_effect-kind block (`expense_logged` with
  金額/商家/時間/支付/編號; `expense_needs_review` with reason +
  filename list), 關鍵字 hashtags. `payment_method == "其他"` gets
  a `⚠️` prefix prompting manual check.
- `pdfplumber` (plus pdfminer.six / pillow / pypdfium2 transitive).

### Changed

- **Breaking — `EmailAnalysis` schema**: `importance` becomes
  `int (1-5)` (was `Literal["high","medium","low"]`); `urgency`
  and `keywords` added; `category` is now a Chinese Literal of 13
  values (was English Literal of 6). Migration 0003 drops the old
  column and recreates — existing dev rows are not preserved.
- **Breaking — `EmailAgent.__init__`**: now requires `notifier:
  TelegramNotifier` and `client: GmailClient` (the latter for the
  expense subgraph's PDF fetch).
- `EmailAgent.handle_email` is now properly idempotent across
  re-invocations: it inspects `graph.get_state(config)` first and
  reuses the cached analysis when the thread is already complete,
  so a second call no longer re-bills the LLM or re-sends Telegram.
  Interrupted threads are resumed via `invoke(None, config)`. This
  closes a gap that v0.1's CHANGELOG actually oversold.
- ORM uses generic `sqlalchemy.JSON` / `sqlalchemy.Uuid` rather
  than Postgres-specific `JSONB` / `postgresql.UUID`, so the
  SQLite-backed test fixtures keep compiling. Production
  PostgreSQL still maps these to `JSONB` / `UUID` at the dialect
  layer.

### Post-design adjustments (from live-run feedback)

- `ExpenseSubgraph` is now a class (was `compile_expense_subgraph`
  factory), matching the `EmailAgent` shape for project-internal
  consistency. Exposes the compiled CompiledGraph as `.graph`.
- **Migration 0003 wraps a `TRUNCATE TABLE email_analyses`**
  before reshaping. Without it, the `ADD COLUMN importance
  INTEGER NOT NULL` raised `psycopg.errors.NotNullViolation` on
  any non-empty dev DB. The TRUNCATE makes the documented "dev
  rows not preserved" behaviour actually happen.
- **`importance` coercion at use-sites** (`_format_message` and
  `_persist`). A pydantic `field_validator` alone is not enough
  when LangGraph's checkpoint loader reconstructs state via
  `BaseModel.model_construct`, which bypasses validators. The
  shared `coerce_importance(value)` helper in `core.schemas`
  accepts ints, digit strings ("4"), digit-with-label ("4
  (重要)"), Chinese labels (非常不重要..非常重要), and English
  labels (very low..very high). Unmappable values fall back to a
  neutral 3 at the formatter rather than crashing the daemon.
- **Full expense field rendering**: `ExpenseLogged` gains
  `location` and `category`; the Telegram footer now always
  renders all seven payment fields (金額 / 商家 / 地點 / 類別 /
  時間 / 支付 / 編號) with `不明` for any field the LLM could
  not extract. Distinguishes "extraction failed for this field"
  from "format dropped this field".
- **Auto-generated `transaction_id`** with an `AUTO-` prefix when
  the email itself carries none — derived deterministically from
  `sha256(message_id)[:12]` so resumes / retries never split a
  single email across two ids. Telegram appends `(自動編號)` to
  flag the synthetic value.

### Tests

218 tests total (85 new over v0.1's 133):

- **Notifications + registry (v0.2 batch, 36):**
  - `tests/notifications/test_telegram.py` (14): construction
    guards, URL / payload / parse_mode / timeout, error mapping.
  - `tests/agents/test_email_agent.py::TestNotifyNode` (7):
    notifier called once, message contents, HTML escaping,
    fail-closed blocks persistence, second-call idempotency,
    ordering after analyze.
  - `tests/agents/tools/test_registry.py` (15): register /
    decorator / duplicate / non-BaseTool guards, lookup, `all()`
    snapshot, dunders, `default_registry` presence.
- **Analyze + expense vertical (v0.3 batch, 27):**
  - `tests/agents/verticals/test_pdf.py` (13): page concat,
    None-page handling, image-only / corrupt PDF safe returns,
    body+PDF combination, unreadable-PDF reporting, mixed
    readable/unreadable.
  - `tests/agents/verticals/test_expense.py` (7): happy path
    (extract + persist + ExpenseLogged), image-PDF early-bail
    skips LLM, all-null draft → needs_review, amount-only and
    vendor-only persist, UNIQUE collision reuses existing row,
    user prompt includes body + PDF text.
  - `tests/agents/test_email_agent.py::TestRouting` (2): non-expense
    category skips subgraph, `category == "消費支出"` invokes
    subgraph and Telegram message includes expense block.
  - `tests/agents/test_email_agent.py::TestNeedsReviewNotify` (2):
    image-PDF reason wording + filename listed, all-null reason
    wording.
  - `tests/agents/test_email_agent.py::TestExpenseLoggedNotify` (4):
    full-fields rendering, missing-amount shows 不明, `其他`
    payment method gets a ⚠️ prefix, AUTO- transaction id gets a
    (自動編號) suffix.
- **Post-design batch (22 new):**
  - `tests/core/test_schemas.py::TestImportanceCoercion` (17):
    int passthrough, digit-string coercion, digit-with-label,
    Chinese / English label mapping, out-of-range rejection,
    unmappable rejection.
  - `tests/agents/verticals/test_expense.py::TestAutoTransactionId`
    (4): LLM-provided id passes through, missing id triggers
    AUTO- generation with stable length, helper is deterministic
    and distinguishes inputs, auto-id reaches the persisted row.
  - `TestExpenseLoggedNotify::test_auto_transaction_id_marked_in_message`
    (1): (自動編號) suffix only on AUTO- ids.

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
