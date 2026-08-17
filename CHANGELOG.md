# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Adds the third leg of the observability trilogy — **log aggregation
via Loki**, unified with Alloy as the shipper on both compose and
k3s. The JSON log format's `trace_id`/`span_id` fields (shipped in
v1.1) now unlock end-to-end **log → trace jump** in Grafana:
click any log row's `trace_id` → land on the corresponding Tempo
span tree. Zero app code, chart, or spec-runtime change; pure
infrastructure + docs on top of v1.1's foundation.

### Added

- **Compose observability profile**: two new services under
  `--profile observability` — `loki` (`grafana/loki:3.2.1`,
  single-binary mode, filesystem backend, env-driven retention via
  `LOKI_RETENTION_PERIOD`) and `alloy` (`grafana/alloy:v1.5.1`,
  tails Docker container stdout via `/var/run/docker.sock`, forwards
  to Loki with container name + stream + compose-service labels).
- **Grafana provisioning**: Loki datasource pre-provisioned with a
  **derived field** `trace_id → Tempo` — every JSON log row with a
  `trace_id` gets a clickable `View trace in Tempo` link. Tempo
  datasource also gains `tracesToLogsV2` so the jump works
  bidirectionally (span → filtered log query).
- **`LOKI_RETENTION_PERIOD`** env var (compose-only, defaults `7d`)
  — same env-driven-retention shape as `PROMETHEUS_RETENTION_TIME`.
- **`docs/observability.md`** (+ `.zh.md`): Path A gains a rewritten
  "Enable log aggregation" section 4 (bundled Loki/Alloy + trace-jump
  UX); Path B adds a new section 5 for k3s (install
  `grafana/loki` single-binary + `grafana/alloy` DaemonSet + UI-add
  Loki datasource with derived field, since kube-prom-stack Grafana
  sidecar's provisioning-reload API is auth-locked).

### Changed

- `scripts/check_env_parity.py` `COMPOSE_ONLY` allowlist gains
  `LOKI_RETENTION_PERIOD` (compose-only, same rationale as
  `PROMETHEUS_RETENTION_TIME` + `GRAFANA_ADMIN_PASSWORD` — Helm
  deploys own log retention via the operator-installed loki chart).
- `deploy/compose/README.md` Observability profile section: service
  table grows from 4 → 6 rows, adds "Adjusting Loki retention"
  subsection, updates data-volumes list.
- `LOG_FORMAT` env in `.env.example` gets a "recommended for the
  observability profile" note (derived-field regex only matches JSON).

### Backward compatibility

- App: **zero change**. `LOG_FORMAT=json` + `trace_id`/`span_id`
  in log fields already shipped in v1.1. Default remains `text`.
- Chart: **zero change**. K3s operators install `grafana/loki` +
  `grafana/alloy` charts separately (same pattern as v1.1.1
  Tempo-direct recommendation).
- Compose: `docker compose up` without `--profile observability`
  still renders the same one-service stack as v1.0/v1.1.

### Non-goals

- Log-based alerts (v1.1 metric alerts cover the operational
  scenarios; adding Loki-alerting rules doubles mental footprint
  for minimal marginal value).
- Bundling Loki/Alloy into the app Helm chart (cluster observability
  infra belongs in kube-prometheus-stack namespace, not app namespace).
- Multi-tenant Loki / S3-backed storage (homelab scale).
- Migrating Prometheus/Tempo shippers through Alloy (Prom scrape
  works, no reason to disturb).

## [1.1.1] — 2026-08-17

Docs-only patch. Closes the deferred v1.1 task 9.7 (k3s tracing e2e)
knowledge gap surfaced during the k3s smoke on nttu-gpu-lab. No chart,
code, or runtime behavior change; upgrade path is `git pull` / re-read
docs — no `helm upgrade` needed for this release.

### Changed

- **Docs** — `docs/observability.md` (+ `.zh.md`) k3s section 4
  ("Enable tracing") rewritten. Recommended path is now installing
  `grafana/tempo` in single-binary mode and pointing the app's OTLP
  exporter directly at Tempo — no OTel Collector in the middle.
  Standalone OTel Collector / Grafana Alloy / Tempo Operator are now
  presented as trigger-based alternatives (fan-out, sampling, planned
  Loki, multi-tenant), not equal-weight options. Notes explicitly that
  the compose observability profile bundles a collector as a
  reference-architecture demo, not a prescription for k3s. No chart,
  code, or runtime behavior change — this closes the deferred v1.1
  task 9.7 (k3s tracing e2e) knowledge gap. See the
  `enable-k3s-tracing` OpenSpec change for context.

## [1.1.0]

Adds the observability side of v1.0's FastAPI service. Prometheus
metrics endpoint (always on), OpenTelemetry tracing (opt-in), JSON
log format with trace-context correlation, and both a self-contained
Docker Compose observability stack (`--profile observability`) and
opt-in kube-prometheus-stack integration templates in the Helm chart.
Fully backward-compatible: v1.0.x → v1.1 with no config changes = same
runtime behavior. Full write-up in the `add-observability-stack`
OpenSpec change (25 design decisions D1–D25). Operator-facing enable
guide in `docs/observability.md` (+ `.zh.md`); tracing implementation
KT in `docs/tracing-architecture.md`; OTel-vs-Langfuse selection
rationale in `docs/observability-tool-choice.md`.

### Added

- **`observability` capability** — new `zashiki_warasi/observability/`
  package. `/metrics` Prometheus endpoint (always on, no app-layer
  toggle — D7); 13-family metric contract with cardinality guardrails
  (`_assert_no_forbidden_labels` rejects `message_id`/`email`/etc.
  at declaration time); OTel SDK bootstrap (`configure_tracing`,
  opt-in via `OTEL_ENABLED=1`, lazy SDK import so disabled = zero
  cost); span emission at 3 tiers — auto (FastAPI/httpx/psycopg
  instrumentation), manual business spans (`zashiki.tick_once`,
  `zashiki.node.*`, `zashiki.llm.chat`, `zashiki.notify.telegram`),
  auto span attribute propagation (`zashiki.message_id`,
  `zashiki.request_id`, OTel GenAI semconv on LLM spans);
  `_RateLimitedBSP` BatchSpanProcessor subclass counting
  `zashiki_traces_dropped_total{reason}` + rate-limiting exporter-
  error WARNINGs to one per minute per reason.
- **Two runtime guardrails** — `check_web_concurrency` (D18) fail-
  fasts on `WEB_CONCURRENCY>1` because prometheus_client registry is
  process-local; `_guard_resource_attributes_secrets` (D22) fail-
  fasts if `OTEL_RESOURCE_ATTRIBUTES` looks like it carries a secret
  (5 known token prefixes + base64 shape), refusing to boot rather
  than leak into every exported span forever.
- **`logging` capability** — extended `LoggingSettings.format`
  (`text|json`), new `JsonContextFormatter` (NDJSON with
  `ensure_ascii=False` for Chinese preservation, `default=str` for
  non-JSON-serializable extras); `_install_trace_context_factory`
  chain-wraps `logging.getLogRecordFactory` exactly once per process
  so every LogRecord carries `trace_id` + `span_id` when an OTel span
  context is active; `_CONTEXT_FIELDS` extended to include
  `trace_id` + `span_id` so both text and JSON formatters render them.
- **`http-service` capability** — new `GET /metrics` route
  (unconditionally registered, no `X-API-Key` gate).
- **`container-deployment` capability**:
  - Compose: new `deploy/compose/observability/` config files
    (OTel Collector + Prometheus + Tempo + Grafana with pre-
    provisioned datasources + starter dashboard); 4 profile-guarded
    services in `docker-compose.yml`; Prometheus retention env-driven
    via `PROMETHEUS_RETENTION_TIME` (default 7d, D20); Grafana port
    loopback-only default (`127.0.0.1:3000:3000`) with rotate-
    password warning inline.
  - Helm: three new templates all default OFF —
    `servicemonitor.yaml` (with `job` relabeling for stable alert
    query matching), `prometheusrule.yaml` (5 starter alerts, each
    independently toggleable + threshold-tunable, `additionalRules`
    append point non-destructively, D19), `dashboard-configmap.yaml`
    (per-dashboard toggle so disabling → sidecar auto-removes panel
    from Grafana, no manual UI cleanup).
- **Middleware conversion** — `RequestIdMiddleware` refactored from
  Starlette `BaseHTTPMiddleware` to pure ASGI (D17); prevents
  anyio-task-group boundary from breaking OTel `contextvars`-based
  span parenting. New `MetricsMiddleware` also pure ASGI. Behavior
  identical to v1.0; all 7 pre-existing request-id tests pass without
  modification.
- **7 new business metric wire-ups** — `zashiki_tick_*`,
  `zashiki_gmail_api_*`, `zashiki_llm_*`, `zashiki_telegram_send_total`,
  `zashiki_oauth_refresh_total`, `zashiki_oauth_token_expires_in_seconds`,
  `zashiki_healthz_status` — emitted at business call sites via
  shared `record_call` / `observe_outcome` helpers so a future new
  concern only needs the metric declaration + one `with` at the call
  site.
- **Documentation** —
  - `docs/observability.md` + `.zh.md` — operator-facing enable +
    reference (compose + k3s paths, metric contract, span structure,
    JSON log schema, tuning knobs, troubleshooting).
  - `docs/tracing-architecture.md` — implementation KT (full-scope
    architecture, three landmines documented).
  - `docs/observability-tool-choice.md` — why OTel/Tempo over
    Langfuse for this project (three-axis evaluation framework).
  - `docs/lessons/2026-08-11-basehttp-middleware-vs-otel-tracing.md`
    — pure-ASGI-middleware requirement lesson.
  - `docs/lessons/2026-08-12-prometheus-multi-worker.md` —
    single-worker fail-fast lesson.

### Changed

- `LoggingSettings.format` field added (env `LOG_FORMAT`, default
  `text`). v1.0 text format preserved byte-identical for records
  without OTel context; regression pin in `tests/core/test_logging_
  group5.py::TestTextFormatRegression`.
- `_CONTEXT_FIELDS` allowlist extended with `trace_id` + `span_id`
  (order: `request_id` → `trace_id` → `span_id` → per-message IDs).
- `RequestIdMiddleware` — pure ASGI. `scope["state"]["request_id"]`
  set for handler consumption (unchanged semantics vs v1.0).
- `check_env_parity.py` extended `COMPOSE_ONLY` allowlist with
  `PROMETHEUS_RETENTION_TIME` + `GRAFANA_ADMIN_PASSWORD` (compose-
  only knobs; k3s uses kube-prom-stack's own retention + auth).
- FastAPI application factory now attaches `FastAPIInstrumentor` on
  create — reads current TracerProvider at request time so
  `OTEL_ENABLED=0` renders NoOp spans (near-zero overhead).

### Added dependencies

- `prometheus-client>=0.20,<1.0`
- `opentelemetry-api>=1.30,<2.0`
- `opentelemetry-sdk>=1.30,<2.0`
- `opentelemetry-exporter-otlp-proto-grpc>=1.30,<2.0`
- `opentelemetry-instrumentation-fastapi>=0.51b0,<1.0`
- `opentelemetry-instrumentation-httpx>=0.51b0,<1.0`
- `opentelemetry-instrumentation-psycopg>=0.51b0,<1.0` (compat probed
  against psycopg 3.2.13)

Image size delta: pending measurement (task 8a.2). No `python-json-
logger` dep (JSON formatter is hand-rolled, D5); no
`opentelemetry-instrumentation-logging` dep (custom `setLogRecord
Factory` bridge in `core/logging.py`, D4); no LangSmith / Langfuse
SDK (OTel + GenAI semconv is the neutral integration surface, see
`docs/observability-tool-choice.md`).

### Test count

559 (v1.0) → 700 (v1.1). New categories: observability metric
contract + label guard (14), `/metrics` endpoint (7), Group 3 metric
wire-ups (17), Group 4 tracing bootstrap + BSP (35), Group 4b span
tree integration (7), Group 5 log format + trace correlation (28),
Group 7 Helm observability manifests (17), + minor test additions
along refactors (D24 subclass isinstance, D25 factory sentinel attr).
Zero regression on the v1.0 baseline.

### Backward compatibility

Nothing on the runtime path changes for operators who don't opt in:

- `/healthz`, `/poll`, `/reauth`, `/auth/*` behavior identical
- Text log format byte-identical
- `docker compose up` (no profile) renders the same one-service stack
- `helm install ...` (no observability flags) renders the same
  manifests as v1.0

Upgrade v1.0.x → v1.1 requires no config change. Opt-in per surface
after the upgrade — start with metrics (near-zero cost), add
dashboards + alerts, enable tracing last (needs a collector).

### Non-goals (explicitly deferred to future changes)

- Log aggregation with Grafana Loki (candidate name
  `add-log-aggregation-loki`; depends on this change's `LOG_FORMAT=
  json` + trace context fields)
- Prometheus exemplars (metric → trace jump)
- Alertmanager receiver bundle (routing is cluster-owned)
- Route-specific trace sampling (fold in only if `/healthz` traces
  become clutter)
- Unified telemetry pipeline via Grafana Alloy
- LLM prompt / completion span content capture (PII + payload size)
- Multi-worker uvicorn support via `PROMETHEUS_MULTIPROC_DIR`
- CI test job for `.gitlab-ci.yml` (v1.0-inherited limitation:
  pipeline only builds image, does not run pytest)

## [1.0.0]

Replaces the long-running Python daemon with a FastAPI service driven
by an external scheduler (host cron / k8s CronJob). Same LangGraph
agent internals, same Postgres schema, same Telegram interface — new
transport + deployment shape that escapes host-suspension failure
modes (macOS App Nap) and unblocks multi-replica HA for the WebUI
epic. Full write-up in the `migrate-to-fastapi-service` OpenSpec
archive; migration guide in `MIGRATION-v1.md`.

### Added

- **`http-service` capability** — FastAPI application (`uvicorn
  zashiki_warasi.web:app`) with `/healthz`, `POST /poll`,
  `POST /reauth`, `GET /auth/start`, `GET /auth/callback`. Request-id
  middleware honors `X-Request-ID` header; optional `X-API-Key` auth
  on the two POSTs; fail-fast bootstrap if bind is non-loopback
  without a key.
- **`container-deployment` capability** — Dockerfile (multi-stage,
  `python:3.13-slim-bookworm`, non-root uid 10001, entrypoint runs
  `alembic upgrade head`), `deploy/compose/` template with
  crontab.example, `deploy/helm/zashiki-warasi/` chart (9 templates
  incl. init container that seeds `token.json` from the OAuth Secret,
  `imagePullSecrets` support, `replicaCount`-scalable).
- **`headless-authentication` capability** — OAuth web flow via
  `POST /reauth` → browser → `GET /auth/callback`; PKCE
  `code_verifier` persisted in Postgres `oauth_flows` table so the
  callback can land on any replica.
- **Cross-replica coordination** (design D17) — `pg_try_advisory_lock`
  replaces `asyncio.Lock` for `POST /poll` single-flight; Postgres
  `oauth_flows` table replaces the in-process OAuth flow dict.
  Validated with two pods on two different k3s nodes racing on
  `POST /poll` → exactly 1×200 + 9×409.
- **New CLI subcommands** — `serve` (uvicorn wrapper for dev), `tick`
  (one-off `tick_once` + JSON output). Hidden `run-legacy` retains
  v0.6.x one-shot semantics for wrapper compat.
- **GitLab CI pipeline** (`.gitlab-ci.yml`) — build + push to
  `gitlab.davidho.dev:5050/homelab/zashiki-warasi` on every push.
- **`docs/oauth-redirect-uri.md`** — three OAuth redirect URI
  strategies (SSH tunnel + loopback / Cloudflare Tunnel / public
  domain + Let's Encrypt) with troubleshooting appendix.
- **`docs/v1.0-smoke-lessons.md`** (+ `.zh.md`) — 14 issues caught
  during Proxmox + k3s smoke testing, each with symptom → root cause →
  fix → prevention. Operator checklist for future migrations of
  similar scope.
- **`MIGRATION-v1.md`** — end-to-end v0.6.x → v1.0.0 upgrade path.
- **`scripts/check_env_parity.py`** — CI-runnable diff between
  compose `.env.example` and Helm `values.yaml`.
- **`OAUTHLIB_RELAX_TOKEN_SCOPE=1`** set in Dockerfile ENV + Python
  `os.environ.setdefault` — Google's token endpoint returns every
  scope previously granted to the user/client pair; strict oauthlib
  scope check rejects the superset.
- **`gmail_client.reload_credentials()`** — swap the Gmail service's
  credentials without a process restart, called by `/auth/callback`
  after writing a fresh `token.json`.

### Changed (BREAKING)

- **Entry point removed**: bare `python -m zashiki_warasi` daemon
  mode is gone; the CLI requires a subcommand. Run `serve` for the
  FastAPI service, `tick` for a one-off poll, `reauth` for the
  interactive OAuth flow.
- **Process model**: the in-process polling loop is deleted;
  `tick_once` is a pure function invoked per-request by
  `POST /poll`. Cadence + liveness are owned by an external
  scheduler (cron / CronJob) + `/healthz` probe.
- **Deploy shape**: `docker-compose.yml` at the repo root and the
  `docker/` entrypoint directory are removed. Use
  `deploy/compose/docker-compose.yml` (external Postgres only, no
  bundled DB) or `deploy/helm/zashiki-warasi/` (k8s).
- **Env vars removed** (silently ignored with one-time INFO on
  startup): `POLLER_INTERVAL_SECONDS`, `POLLER_HEARTBEAT_INTERVAL_SECONDS`.
- **Env vars added**: `HTTP_BIND_HOST`, `HTTP_BIND_PORT`,
  `HTTP_API_KEY`, `OAUTH_REDIRECT_URI`, `OAUTHLIB_RELAX_TOKEN_SCOPE`.
- **DB tables added**: `oauth_flows` (idempotently created at
  startup). No existing table modified.

### Fixed (during v1.0 smoke)

- Deploy templates now use `GMAIL_*` env prefix matching the code
  (was `GOOGLE_*` in docs, which the app never read).
- `.dockerignore` no longer excludes `alembic.ini` + `alembic/` —
  fresh DBs auto-provision on container start.
- OAuth flow store persists PKCE `code_verifier` across put/pop —
  prevents `invalid_grant: Missing code verifier` on
  `/auth/callback`.
- `crontab.example` escapes `%` (cron's end-of-command marker) and
  documents that cron doesn't inherit shell env.
- Helm chart supports private registries via `imagePullSecrets`;
  init container seeds `/data/token.json` from OAuth Secret on
  first boot.
- Full retrospective in `docs/v1.0-smoke-lessons.md`.

### Migration

See [`MIGRATION-v1.md`](MIGRATION-v1.md). Rollback is a container
swap — no DB schema incompatibility with v0.6.x.

## [Unreleased]

### Fixed

#### Notion API migration — `databases.query` → `data_sources.query`

- `notion-client 3.x` removed `client.databases.query` when Notion's
  2025 API split databases into 1+ data sources. The freshly-shipped
  `NotionExpensePuller` broke on first run with
  `AttributeError: 'DatabasesEndpoint' object has no attribute 'query'`.
- Puller now resolves the data_source_id lazily via
  `databases.retrieve` and caches it for the process lifetime, then
  calls `data_sources.query(data_source_id=…)`. The write side
  (`NotionExpenseRecorder.record_expense`) is unaffected — `pages.create`
  with `parent={"database_id": …}` still works.
- 3 new tests: cache behaviour (retrieve called once across N syncs),
  correct arg name passed to `data_sources.query`, and clear
  `RuntimeError` when the retrieve response has no data_sources.

#### Classification: strict 4-signal definition of a real transaction

- A 楽天カード「【速報版】カード利用のお知らせ(本人ご利用分)」
  mail (just an aggregate daily total, no per-purchase detail)
  slipped through as `消費支出`. The previous phrasing
  ("若只有一筆真實交易 → 消費支出") was too loose — the local LLM
  read "one amount" as "one transaction" and ignored the
  Rakuten-specific rule that was already present.
- The classification rule now defines `消費支出` explicitly:
  the mail must contain **at least one** transaction with
  **all four** signals — (a) 具體日期時間 (b) 具體店家名稱
  (not `本人利用` / `カード利用` generic phrasing) (c) 金額
  (d) 交易識別碼 (承認番号 / 伝票番号 / 注文番号). Missing
  **any one** signal → `消費資訊彙整`.
- Both positive (SMBC Olive `ご利用のお知らせ` has all four) and
  negative (楽天カード `【速報版】` has amount + optional date
  only) examples are inlined so the LLM sees the discriminating
  signal (specific vendor + transaction id vs. aggregate total).
- 1 new pinned test:
  `test_rakuten_flash_notification_stays_digest_category`
  guards `速報版`, `楽天カード`, `四項`, and `本人利用` so a
  future rewrite that keeps only the abstract 4-signal definition
  still trips if the concrete Rakuten example is dropped.

#### Expense prompt rule 10 reframed as positive extraction

- First pass at the "boilerplate vs. real transaction" rule (added
  in 4bae084) fixed the classification but broke amount extraction.
  A live SMBC Olive charge (803 JPY at RENGESYOKUDO MIYAZAKI) came
  back with correct vendor / transaction_id / transacted_at /
  payment_method but `amount=None`. Root cause: the previous
  phrasing
  ("boilerplate 通常只有金額而缺日期或店家 — 這種數字**不是**
  這筆消費的金額") read as "if you see amounts, treat them as
  suspect" to the local LLM, and the safest response was to
  default `amount` to null.
- Rule 10 now leads with **positive** guidance: it shows the
  literal `◇利用日 / ◇利用先 / ◇利用金額 / ◇承認番号` block
  from a real SMBC Olive mail with each `◇` line annotated with
  its target ExpenseDraft field, and demands `amount` be filled
  ("必須全部抽出,尤其 amount 一定要填,不要漏掉 or 回 null").
  The boilerplate warning is still there for the `※` disclaimer
  below, but it's no longer the leading framing.
- 1 new pinned test in `TestExpenseExtractPromptRules` guards
  the `◇利用金額` / `◇利用日` / `◇利用先` markers and the
  concrete `803` example — so a future rewrite that keeps only
  the abstract mapping still trips the check.

#### Boilerplate-vs-transaction anti-hallucination rules

- A single-transaction SMBC Olive デビット notification
  (280 JPY at SEVEN-ELEVEN, 承認番号 498134) was misclassified
  as `消費資訊彙整` and its Telegram summary claimed a second
  110 JPY "ATM" transaction. The 110 came from a boilerplate
  disclaimer line at the bottom of the mail
  (「海外ATMでの現地通貨の引き出しは...ATM利用手数料110円を
  加えて引き落とし致します」) — a hypothetical fee note, not
  a transaction. Because the mail then routed to `notify`
  (not `expense_sg`), no expense record was created either.
- `ANALYZE_SYSTEM_PROMPT` gains two guards:
  1. A `⚠️ 反幻想` block in §2 (summary) telling the model
     that only date+vendor+amount-complete lines count as
     transactions; boilerplate (fee terms, hypothetical
     scenarios, promo asides) must be omitted from the summary.
  2. A new `分類規則` bullet in §3 clarifying that a mail with
     ONE real transaction plus boilerplate stays `消費支出`,
     not `消費資訊彙整`. Cites the SMBC Olive example verbatim
     with the 110 JPY fee text so the model sees the concrete
     failure pattern.
- `EXPENSE_EXTRACT_SYSTEM_PROMPT` gains rule 10 (`條款 vs.
  實際交易`) so even if a similar mail slips past classification,
  the extractor won't pull `amount=110` from an ATM-fee disclaimer.
  Same SMBC Olive citation for consistency.
- 3 regression tests in `TestAnalyzePromptRules` /
  `TestExpenseExtractPromptRules` — pinned string checks (`反幻想`,
  `boilerplate`, `SMBC Olive`, `承認番号`, `110`) so a future
  prompt rewrite that keeps only the intent but drops the concrete
  example still trips the test and surfaces to review.

#### Catch `LengthFinishReasonError` in the analyze node

- When an email + system prompt exceeds the LLM's context window
  (in the wild: a 31k-prompt email exhausted the 32k window
  mid-generation, `finish_reason=length`), `openai` raises
  `LengthFinishReasonError` from
  `_parse_chat_completion`. Previously it escaped the graph and
  surfaced as `Unhandled error during tick; will retry`, which
  meant the poller re-tried the same doomed LLM call every tick
  until Gmail's ~7-day history retention rolled the message off.
- `_analyze` now catches the error, logs the prompt/completion
  token counts, and returns
  `{"analysis": None, "side_effect": AnalysisFailed(...)}`.
  The graph completes normally, notify sends the user a Telegram
  message describing the failure, and `handle_email` skips
  persistence (no placeholder row in `email_analyses`).
- New `AnalysisFailed` variant added to the `SideEffect` union
  (`kind="analysis_failed"`, `reason: Literal["content_too_long"]`,
  optional `detail` for token counts). Extensible if future
  analyze failure modes need distinct handling.
- New `_format_analysis_failed` renderer produces the whole
  Telegram body (no analyze summary to hang it on) with the
  offending mail's subject / sender escaped, the reason in
  Chinese, and the raw token counts inside a `<code>` block.
- 8 new tests in `TestAnalyzeFailure` / `TestFormatAnalysisFailed`:
  handle_email returns normally, notify still sends, no analysis
  row persisted, structured runnable called exactly once (no
  retry inside the graph), content-too-long reason string, HTML
  escape guard, detail-omitted-when-None.

#### `帳單通知` category now routes to the expense subgraph

- `_route_by_category` was hardcoded to `category == "消費支出"`,
  so bill-notification emails (cloud invoices, utility bills,
  telecom charges, etc.) went straight to `notify` and never
  reached `ExpenseSubgraph._extract_node` — the only path that
  downloads and reads PDF attachments. Symptom in the wild: a
  Google Cloud Platform electronic invoice was classified as
  `帳單通知`, the invoice PDF was fetched by the Gmail poller but
  never opened, and the Telegram summary carried `消費金額為不明`.
- Router now dispatches to expense for both `消費支出` and
  `帳單通知` via a new `_EXPENSE_LIKE_CATEGORIES` tuple.
  Adjacent categories (`消費資訊彙整`, `點數資訊彙整`, `訂閱服務`,
  `廣告`, `促銷`) remain routed to `notify` — the tuple is the
  single source of truth so future additions are one-line.
- Extraction system prompt broadened from "消費支出資訊擷取助理"
  to "消費支出 / 帳單資訊擷取助理" and now lists cloud invoice /
  utility / subscription as valid sources. All other extraction
  rules (nulls-not-guesses, ISO 8601, payment_method taxonomy,
  title guidance) unchanged — bill fields map cleanly onto the
  existing schema.
- 10 new tests in `TestRouting`: parametrised route table for
  both expense-like categories, regression guards that 7 other
  categories still hit `notify`, and the `analysis is None`
  short-circuit.

#### Puller UUID duplicate guard

- `NotionExpensePuller._apply_page` now reads the Notion page's
  `UUID` property (== `expenses.transaction_id`) and, before doing
  any update, looks up any local `ExpenseRecord` with that
  transaction_id. If the lookup returns a row whose
  `notion_page_id` differs from the page currently being processed,
  the page is skipped with a log entry.
- Motivation: during the migration window from the previous n8n
  workflow, both systems process the same emails. A given
  transaction can end up as two Notion pages sharing one UUID
  (one Zashiki-linked, one n8n-created). The guard ensures an n8n
  page that somehow slips past the marker filter cannot drive an
  update against a row it doesn't own.
- Pages without a UUID (transaction_id was `None` on the original
  extraction) bypass the guard — existing behaviour preserved.
- 4 new tests in `TestUuidDuplicateGuard`.

#### Poller loop stability

- **Gmail HTTP socket timeout.** `GmailClient` now builds its own
  `httplib2.Http(timeout=…)` and wraps it in `AuthorizedHttp` before
  passing to `googleapiclient.discovery.build`. Without this,
  httplib2's default of `None` lets the OS TCP layer wait ~13min
  (RTO doubling) before a dead connection surfaces as
  `ConnectionResetError` — visibly stalling the poller. Default
  timeout `60`s via new `GMAIL_HTTP_TIMEOUT_SECONDS` env var.
  4 new tests in `test_client_api.py::TestHttpTimeout`.
- **DB pool pre-ping.** `create_engine` now sets `pool_pre_ping=True`
  and `pool_recycle=1800`. Overnight-idle connections killed by a
  home-router NAT eviction or by Postgres server-side idle-drop
  were surfacing as `SSL SYSCALL Operation timed out` /
  `server closed the connection unexpectedly` on the next query;
  both are eliminated by the cheap SELECT 1 pre-flight on checkout.
  Both the Gmail poller and Notion puller share the singleton
  engine, so both benefit. 2 new tests in `test_db.py::TestGetEngine`.

### Added

#### Notion → DB reverse sync

- New `NotionExpensePuller` (`notifications/notion_puller.py`) pulls
  user edits in the Notion expense database back into Postgres so
  the local store stays the source of truth after manual LLM-output
  corrections.
- Background thread launched from `app.run()` runs the puller every
  `NOTION_SYNC_INTERVAL_SECONDS` (default `300`, `0` disables).
  Shares the poller's `stop_event` so SIGTERM drains it cleanly.
- New one-shot CLI subcommand: `zashiki-warasi sync-notion` runs a
  single reconcile pass and exits (useful for debugging / manual
  catch-up when the thread is disabled).
- Query filter only matches pages stamped with the
  `auto generated by zashiki-warasi` marker in the 備註 column, so
  user-created Notion rows are ignored.
- Syncable fields: title, vendor, amount, currency, transacted_at,
  category, payment_method. Conflict policy: **Notion wins**.
- New table `notion_sync_state` (alembic `0007`) stores the
  per-database `last_edited_time` cursor; cursor advances to the max
  edit time seen in the batch (not `now()`) to avoid clock-skew rewind.
- New column `expenses.notion_synced_at` records the last reverse-sync
  write so diffs are inspectable.
- CLI refactored from `click.command` to `click.group` with
  `invoke_without_command=True`, preserving the existing `--reset` UX
  while adding subcommand surface area.

#### Docker deployment

- `Dockerfile` (multi-stage, uv-based) builds a slim Python 3.13
  runtime image with the project venv. BuildKit cache mounts keep
  cold builds under ~10s.
- `docker/entrypoint.sh` runs `alembic upgrade head` before exec'ing
  the CLI, so schema is always at HEAD on container boot. Uses `exec`
  so SIGTERM from `docker stop` reaches the Python process and the
  shutdown handlers can drain the in-flight message.
- `docker-compose.yml` provides a self-contained stack (app + Postgres
  16-alpine + healthcheck gate) for users without an existing
  Postgres. OAuth client is host-mounted (`./credentials/`); the
  refresh token cache lives in a named volume so it survives rebuilds.
- `.dockerignore` excludes `.git`, caches, `.venv`, secrets
  (`credential.json`, `credentials/`, `token.json`, `.env`), and
  local-only paths (`tests/`, `scripts/`, `docs/lessons/`).

#### Analyze prompt revisions (live-run feedback)

- **Summary now keeps payment / point / aggregate specifics.**
  The prompt's earlier "do NOT include amount / vendor / time in
  summary" rule (introduced to avoid duplication with the expense
  subgraph's structured output) was making non-expense
  notifications — Rakuten point summaries, credit-card roll-ups,
  bill aggregates — lose all useful detail, since those emails
  don't route to the expense subgraph and have no other structured
  fallback. Reverted to "include amount / time / location /
  transaction-id when present; use 不明 for missing".
- **Three new categories in the analyze Literal:**
  - `消費資訊彙整` — single-email roll-ups containing multiple
    transactions, including Rakuten card's daily
    「【速報版】カード利用のお知らせ」 with no per-line detail.
  - `點數資訊彙整` — Rakuten / d-point / similar point earning
    & spending notifications. Distinct from `消費支出`; does not
    route to the expense subgraph.
- **Classification rules added:** points are never `消費支出`,
  aggregate notifications are never `消費支出`, financial-product
  promos remain `廣告` / `促銷`.

Trade-off accepted: for `消費支出` emails the summary may now
duplicate or slightly diverge from the structured `ExpenseLogged`
fields. The structured fields remain source of truth; the Telegram
message renders both side-by-side so the user can judge.

#### `--reset` CLI flag

- `zashiki-warasi --reset` `TRUNCATE`s every app table
  (`gmail_sync_state`, `processed_messages`, `email_analyses`,
  `expenses`) plus every LangGraph PostgresSaver table
  (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`,
  `checkpoint_migrations`) before booting the poller, so the next
  run behaves exactly like a first install — re-baselines the
  Gmail `historyId`, no dedup memory, no checkpoint resume.
- Defaults to an interactive `[y/N]` confirmation prompt; `-y` /
  `--yes` skips it for non-interactive use.
- Notion is intentionally NOT touched — the mirror outlives a
  reset, matching the existing "Postgres is source of truth, Notion
  is best-effort" boundary.
- `reset_database()` in `core/db.py` looks up the target table
  list against `information_schema.tables` before TRUNCATE, so a
  reset on a fresh install (where LangGraph hasn't materialised
  its tables yet via `checkpointer.setup()`) does not crash.
- Console-script entry point updated from `app:run` to `app:main`
  to introduce the `click` command layer; `app.run()` itself is
  unchanged.
- CLI built on `click` (≥8.1) — `@click.command` decorating
  `main`, `@click.option` for `--reset` / `-y`, and
  `click.confirm(abort=True)` for the interactive prompt.
  `reset_database()` itself is now confirmation-free and
  unconditional: the CLI layer owns the prompt so the function
  stays single-purpose.

#### HTML body fallback

- `agents/verticals/html_text.py` — `html_to_text(html)` helper
  wrapping `html2text` with LLM-friendly defaults (links / images
  stripped, no markdown emphasis markers, no line re-wrapping).
  Defensive: empty / None / malformed input never raises; returns
  `""` so callers can chain it in `or` fallbacks.
- Body fallback chain in `agents/verticals/pdf.py::collect_text`
  and `agents/email_agent.py::_analyze` is now:
  `body_plain → html_to_text(body_html) → snippet`. HTML-only
  emails (modern e-receipts, marketing newsletters) used to
  degrade to the ~200-char Gmail snippet alone; they now feed
  the full converted HTML to the LLM.
- `html2text >= 2025.4.15` dependency added.

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
- **Notion expense database mirror (optional, env-gated).** When
  both `NOTION_TOKEN` and `NOTION_EXPENSE_DATABASE_ID` are set, every
  newly persisted expense row is also written to the configured Notion
  database via `notification/notion.py::NotionExpenseRecorder`. The
  resulting `https://notion.so/<page_id>` link is appended to the
  Telegram message; on dedup hit the existing row's link is reused
  (no second page is created). Failures are best-effort: the Notion
  exception is captured as `notion_sync_error` on the `expenses` row
  and surfaced in Telegram as `⚠️ Notion 同步失敗: …` while Postgres
  remains source of truth. Either env var missing → integration
  completely disabled, no calls attempted. Two new columns on
  `expenses` (`notion_page_id`, `notion_sync_error`) via migration
  `0005_expenses_notion.py`. Required Notion DB schema documented in
  the README. Adds `notion-client` dependency.
- **Cross-email expense deduplication**: when two different emails
  describe the same real-world transaction (e.g. SMBC Olive's
  「承認番号」 confirmation arriving at the same minute as the
  Starbucks merchant receipt), the second one no longer creates a
  duplicate row. `find_duplicate(draft, session)` in
  `agents/verticals/expense.py` does a two-stage match:
  1. Real `transaction_id` collision (AUTO- ids excluded so they
     can never coincide across distinct emails).
  2. `amount + currency + transacted_at ± 15 min`. The window is
     deliberately narrow so back-to-back same-amount purchases are
     not collapsed; ambiguous windows (≥ 2 candidates) bail to
     "insert as new" rather than risk merging the wrong record.
  Long-range duplicates (Amazon "order confirmed" + "shipped"
  hours/days later) are intentionally NOT deduplicated — widening
  the window starts producing false positives on routine recurring
  purchases.

### Tests

256 tests total (123 new over v0.1's 133):

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
  - `tests/agents/verticals/test_expense.py::TestCrossEmailDedup`
    (7): Stage 1 real-id match, AUTO- id non-collision across
    distinct emails, the Starbucks cross-system case (same amount,
    seconds apart, different vendor strings), time-outside-window
    leaves both rows, different-amount leaves both rows, multiple-
    candidates-in-window stays safe by inserting new, missing
    required fields skips Stage 2.
  - `tests/notifications/test_notion.py` (22):
    construction guards (token / database_id required), parent +
    response shape, full property mapping (title fallback, amount
    Decimal→float, currency/payment_method as `select`, category
    as `rich_text`, transacted_at ISO 8601, transaction_id /
    location as `rich_text`), per-field None-omit, error wrapping
    into `NotionSyncError`, missing-id-in-response defensive raise.
  - `tests/agents/verticals/test_expense.py::TestNotionSync` (4):
    no notion → no fields set, success records page id on row +
    SideEffect, failure is captured as `notion_sync_error` and
    never raises, dedup hit does not re-attempt Notion.
  - `tests/agents/test_email_agent.py::TestNotionLinkInNotify` (5):
    page id renders as `https://notion.so/...` link with 🔗, error
    renders as `⚠️ Notion 同步失敗: ...`, long error truncated to
    80 chars, neither field renders nothing, page id takes
    precedence over error.

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
