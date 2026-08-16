# Observability (v1.1)

> 中文版:[`observability.zh.md`](observability.zh.md).
>
> **What this doc is**: the operator-facing enablement + reference
> guide for v1.1's Prometheus metrics, OpenTelemetry tracing, and
> structured logging.
>
> **What this doc is NOT**: architecture deep-dive (see
> [`tracing-architecture.md`](tracing-architecture.md)); selection
> rationale for OTel vs Langfuse (see
> [`observability-tool-choice.md`](observability-tool-choice.md));
> the formal contract spec (see
> `openspec/specs/observability/spec.md`).

## What you get in v1.1

Three signals shipped by the app, all **opt-in from the operator side**:

| Signal | Endpoint / Mechanism | Default state |
|---|---|---|
| **Prometheus metrics** | `GET /metrics` (always exposed) | On (but scraped only if a Prometheus is aimed at it) |
| **OpenTelemetry traces** | OTLP/gRPC to configured collector | Off (`OTEL_ENABLED=0`) |
| **Structured logs** | `LOG_FORMAT=json` → NDJSON to stdout | Off (`text` format = v1.0 one-liner) |

Everything below tells you how to turn them on and consume them.

---

## Path A: Enable on Docker Compose

Compose ships a self-contained observability stack under a
`--profile observability` guard — one command brings up OTel
Collector + Prometheus + Tempo + Grafana on the same network as
the app.

### 1. Bring up the stack

```
cd deploy/compose
docker compose --profile observability up -d
```

Five containers running: `zashiki-warasi` + `otel-collector` +
`prometheus` + `tempo` + `grafana`.

### 2. Turn on tracing on the app

Edit `.env`:

```
OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

Restart the app container:

```
docker compose up -d zashiki-warasi
```

Every `POST /poll` now produces a full span tree in Tempo.

### 3. Open Grafana

- URL: <http://127.0.0.1:3000> (loopback-only by default; see
  compose README for how to safely expose)
- Login: `admin` / value of `GRAFANA_ADMIN_PASSWORD` in `.env`
  (rotate the placeholder before exposing beyond loopback)
- Dashboard: `Zashiki-warasi > Zashiki-warasi overview` auto-imported

### 4. (Optional) JSON logs

Set `LOG_FORMAT=json` in `.env`, restart. Now `docker logs
zashiki-warasi` emits NDJSON — pipe into any structured log shipper.

Full details: [`deploy/compose/README.md`](../deploy/compose/README.md)
`Observability profile` section.

---

## Path B: Enable on Kubernetes (kube-prometheus-stack)

Assumes kube-prometheus-stack (or any Prometheus Operator install)
already lives in the cluster. This chart does NOT ship
Prometheus / Grafana / Alertmanager — it ships the integration
manifests to plug into your existing stack.

### 1. Enable ServiceMonitor (metrics scrape)

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi \
    --reuse-values \
    --set observability.serviceMonitor.enabled=true
```

If your Prometheus CR's `serviceMonitorSelector` requires a label
(usually `release: kube-prometheus-stack`):

```
    --set observability.serviceMonitor.additionalLabels.release=kube-prometheus-stack
```

### 2. Enable dashboards

```
    --set observability.dashboards.enabled=true
```

The kube-prom-stack Grafana sidecar picks up the ConfigMap
labeled `grafana_dashboard: "1"` and auto-imports the dashboard.

### 3. Enable alerts (recommend waiting 24h for metric data first)

```
    --set observability.prometheusRule.enabled=true
```

Tune individual alerts without editing PromQL:

```
    --set observability.prometheusRule.alerts.tickErrorRateHigh.ratePerMinute=0.1 \
    --set observability.prometheusRule.alerts.tickConflictRateHigh.enabled=false
```

### 4. Enable tracing (needs an OTel Collector in-cluster)

```
    --set env.OTEL_ENABLED=1 \
    --set env.OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc.cluster.local:4317
```

The chart does NOT deploy a Collector — expected to already exist
in the cluster (or deploy separately).

Full details:
[`deploy/helm/zashiki-warasi/README.md`](../deploy/helm/zashiki-warasi/README.md)
`Observability` section.

---

## Metric contract

13 zashiki_* metric families + standard `process_*` / `python_gc_*`
from `prometheus-client`. Names + labels are the contract — renaming
a metric or altering its labelset breaks dashboards + alerts. See
`openspec/specs/observability/spec.md` for the authoritative table.

| Name | Type | Labels | Meaning |
|---|---|---|---|
| `zashiki_tick_duration_seconds` | Histogram | `outcome` (success\|error) | Wall-clock time of one `/poll` handler invocation |
| `zashiki_tick_messages_processed_total` | Counter | — | Cumulative Gmail messages processed |
| `zashiki_tick_conflicts_total` | Counter | — | Cumulative 409 `tick_in_flight` responses |
| `zashiki_tick_rebaseline_total` | Counter | — | Cumulative Gmail history rebaselines |
| `zashiki_gmail_api_calls_total` | Counter | `operation` (history\|message_get\|attachment_get\|profile), `outcome` | Gmail API calls |
| `zashiki_gmail_api_latency_seconds` | Histogram | `operation` | Gmail API latency |
| `zashiki_llm_calls_total` | Counter | `node` (analyze\|expense_extract), `outcome` | LLM invocations |
| `zashiki_llm_latency_seconds` | Histogram | `node` | LLM latency |
| `zashiki_telegram_send_total` | Counter | `outcome` | Telegram alert dispatches |
| `zashiki_oauth_refresh_total` | Counter | `outcome` | OAuth token refresh attempts |
| `zashiki_oauth_token_expires_in_seconds` | Gauge | — | Seconds until cached OAuth token expiry (negative = expired) |
| `zashiki_healthz_status` | Gauge | — | Result of most recent `/healthz`: 1 = healthy, 0 = unhealthy |
| `zashiki_traces_dropped_total` | Counter | `reason` (queue_full\|export_failed\|shutdown) | OTel spans the BSP could not export |

**NO generic `zashiki_http_requests_total`** — design decision D16.
Each concern with real diagnostic value has a dedicated business
metric; a generic HTTP counter would duplicate signals with no unique
information for this app's narrow HTTP surface.

**Cardinality guardrails**: `message_id` / `email` / `user_id` /
`email_address` are forbidden as metric labels — enforced at
declaration time by `_assert_no_forbidden_labels` in
`src/zashiki_warasi/observability/metrics.py`. Those identifiers
belong on span attributes or log context fields, never on metric
labels.

---

## Span structure

`POST /poll` produces a span tree of about **15-30 spans per tick**.
Contract shape:

```
POST /poll                          [SERVER, auto — FastAPIInstrumentor]
└── zashiki.tick_once               [INTERNAL, attributes from TickResult]
    ├── HTTP GET /gmail/.../profile [CLIENT, auto — httpx instrumentation]
    ├── HTTP GET /gmail/.../history [CLIENT, auto]
    ├── HTTP GET /gmail/.../messages/... [CLIENT, auto]
    ├── zashiki.node.analyze        [INTERNAL, zashiki.message_id attr]
    │   ├── zashiki.llm.chat        [INTERNAL, GenAI semconv attrs]
    │   │   └── HTTP POST /v1/chat/completions [CLIENT, auto]
    │   └── ... (DB writes via psycopg — auto span if OTel instr)
    ├── zashiki.node.expense_extract [INTERNAL]
    │   ├── zashiki.llm.chat
    │   │   └── HTTP POST /v1/chat/completions
    │   └── ...
    └── zashiki.notify.telegram     [INTERNAL, messaging.system=telegram]
        └── HTTP POST /bot.../sendMessage [CLIENT, auto]
```

**409 conflict short-circuits**: when advisory-lock acquisition fails,
`zashiki.tick_once` is NOT opened — only the SERVER span exists,
with `http.status_code=409`.

**LangChain / LangGraph internal spans are disabled** (D23) via
`LANGCHAIN_TRACING_V2=false` + `LANGSMITH_TRACING=false` set at
`configure_tracing` startup — prevents doubled `langchain.chat_model` /
`langgraph.node.*` spans that would duplicate our manual wrapping.

**Cron-triggered `/poll` has no upstream traceparent**: each tick is
a root trace. If your cron / scheduler starts sending a `traceparent`
header, our OTel instrumentation honors it and parents the tick
under that trace automatically — no code change needed.

Full attribute contract:
[`tracing-architecture.md`](tracing-architecture.md) `§3 Span 契約`.

---

## JSON log format

Enable with `LOG_FORMAT=json`. Emits one JSON object per line
(NDJSON). Contract:

```json
{
  "timestamp": "2026-08-16T09:12:34.567Z",
  "level": "INFO",
  "logger": "zashiki_warasi.agents.email_agent",
  "message": "classified msg-42 as 消費支出",
  "request_id": "cron-2026-08-16-0912",
  "trace_id": "abc123...def",
  "span_id": "abc123...",
  "message_id": "msg-42"
}
```

Fields:
- `timestamp` (always) — ISO-8601 UTC, millisecond precision, `Z` suffix
- `level` (always) — uppercase (`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`)
- `logger` (always) — dotted Python logger name
- `message` (always) — post-`%`-substitution string
- `request_id` (present when in an HTTP request context) — from `X-Request-ID` header or auto-generated 12-hex UUID
- `trace_id` / `span_id` (present when OTel span active) — 32-hex / 16-hex lowercase, matches W3C traceparent convention
- `message_id` / `thread_id` / `expense_id` (present when in per-message processing scope)
- On exception (`exc_info`): additional `traceback` string + `exception` object with `type` + `message`

**Absent fields are OMITTED**, not emitted as `null`. Grep-by-key
in Loki / Vector works without dealing with null-value filtering.

**Chinese characters preserved verbatim** — `ensure_ascii=False` is
a hard requirement. `消費支出` renders as `消費支出`, not
`消費支出`.

**Non-serializable extras don't crash the log call** — `json.dumps`
uses `default=str` fallback. Passing `Decimal(42.50)` or a `datetime`
in `extra=` renders as `str()` of the value; the code path emitting
the log line continues normally.

---

## Tuning knobs

### Sampling ratio

Default `OTEL_TRACES_SAMPLER_ARG=1.0` (100% sampling). Fine for
homelab volume (~1500 traces/day × 20 spans ≈ 15 MB/day).

Drop to `0.1` if traffic scales:

```
OTEL_TRACES_SAMPLER_ARG=0.1
```

`parentbased_traceidratio` (default sampler) means: if the incoming
request carries a `traceparent`, inherit the upstream decision; else
apply the ratio. Compose deploys (cron only) always take the ratio
branch since cron doesn't send `traceparent`.

### Prometheus retention (compose only)

Default 7d. Change via `.env`:

```
PROMETHEUS_RETENTION_TIME=30d
```

Sizing at current metric shape:

| Retention | Approx. disk |
|---|---|
| 7d (default) | < 100 MB |
| 30d | < 500 MB |
| 90d | < 2 GB |

Kubernetes deploys don't ship a Prometheus — retention there is
controlled by your kube-prometheus-stack values
(`prometheus.prometheusSpec.retention`), not this chart.

### Per-alert threshold tuning (Kubernetes)

The 5 starter alerts are individually toggleable + tunable via
values without touching PromQL:

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi --reuse-values \
    --set observability.prometheusRule.alerts.tickErrorRateHigh.ratePerMinute=0.1 \
    --set observability.prometheusRule.alerts.tickConflictRateHigh.for=30m \
    --set observability.prometheusRule.alerts.tickNoSuccessfulTick.severity=critical
```

Add custom alerts alongside starters (non-destructive):

```yaml
observability:
  prometheusRule:
    additionalRules:
      - alert: MyExtraCheck
        expr: <PromQL>
        for: 5m
        labels: { severity: warning }
        annotations: { summary: "..." }
```

### Per-dashboard toggle (Kubernetes)

Each bundled dashboard has its own `enabled` flag under
`observability.dashboards.<name>`. Disabling removes the ConfigMap
→ the kube-prom-stack Grafana sidecar auto-removes the panel from
Grafana on next reconcile. No manual UI cleanup needed.

---

## Troubleshooting

### "I turned on OTel but Tempo shows no traces"

Check in this order:

1. **Was there a `traceparent` header from upstream?** Not relevant
   for our cron use case, but worth ruling out if you're testing
   from a script that sends one.
2. **Is the sampler `AlwaysOff` or set to `0`?** Check
   `OTEL_TRACES_SAMPLER_ARG` value.
3. **Is the collector reachable?** Check `zashiki_traces_dropped_total`
   metric — non-zero `reason=export_failed` means the app tried and
   failed. Check app logs for rate-limited WARNING about the
   endpoint.
4. **Is the collector forwarding to Tempo?** Check collector logs
   (compose: `docker logs zashiki-otel-collector`) for export errors
   to Tempo.
5. **Did the trace get sampled but is Tempo not indexed yet?**
   Tempo indexing lags by a few seconds — retry in Grafana Explore.

### "Metrics scrape works but dashboards show no data"

- Verify the ServiceMonitor's `job` label matches what the alert
  queries expect: run `helm template ...` and grep for
  `replacement:` under `relabelings:`.
- If your Prometheus is filtering ServiceMonitors by label
  (`release: kube-prometheus-stack`), add
  `observability.serviceMonitor.additionalLabels.release=kube-prometheus-stack`.

### "App logs `WEB_CONCURRENCY=N is unsupported`"

Design D18 fail-fast. `prometheus_client` registry is process-local;
multi-worker fragments counters across workers, breaking scrape
coherence. Unset `WEB_CONCURRENCY` or set to `1`. If you truly need
multi-worker throughput, that's a separate change (needs
`PROMETHEUS_MULTIPROC_DIR` mode support, not currently shipped).

### "App refuses to start with `OTEL_RESOURCE_ATTRIBUTES` secret guard error"

Design D22 fail-fast. Some value in `OTEL_RESOURCE_ATTRIBUTES`
matched a known secret prefix (`sk-`, `ghp_`, `AIza`, `xoxb-`,
`AKIA`) or looked base64-ish (≥ 32 chars). Resource attributes are
attached to every exported span — a secret in there leaks
permanently to Tempo. Remove the offending key/value. The log line
names the offending KEY but NOT the value (to avoid the diagnostic
becoming a second leak vector).

### "Log lines are missing trace_id even though OTel is on"

The trace context factory sets `trace_id` / `span_id` only when
there's an ACTIVE OTel span at record-emission time. Bootstrap
logs (before `configure_logging` finishes) and background-thread
logs outside any tracing context correctly omit these fields.

---

## Further reading

- [`tracing-architecture.md`](tracing-architecture.md) — full
  tracing subsystem architecture, design decisions, testing
  strategy, three landmines caught during implementation.
- [`observability-tool-choice.md`](observability-tool-choice.md) —
  OTel/Tempo vs Langfuse selection rationale, when to revisit.
- [`../deploy/compose/README.md`](../deploy/compose/README.md) —
  compose deploy quickstart + observability profile details.
- [`../deploy/helm/zashiki-warasi/README.md`](../deploy/helm/zashiki-warasi/README.md)
  — chart-level knob reference + alert severity → routing plan.
- `openspec/specs/observability/spec.md` — the formal contract.
- `docs/lessons/2026-08-11-basehttp-middleware-vs-otel-tracing.md`
  — why we use pure ASGI middleware (D17).
- `docs/lessons/2026-08-12-prometheus-multi-worker.md` — why
  `WEB_CONCURRENCY>1` is fail-fast (D18).
