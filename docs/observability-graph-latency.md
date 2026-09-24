# Graph-node latency (v1.5.0)

> 中文版:[`observability-graph-latency.zh.md`](observability-graph-latency.zh.md).
>
> **What this doc is**: operator-facing reference for the v1.5.0
> per-node latency instrumentation — four Prometheus histogram
> families, one Grafana dashboard, and their PromQL entry points.
>
> **What this doc is NOT**: the v1.1 metric contract (see
> [`observability.md`](observability.md)); trace architecture (see
> [`tracing-architecture.md`](tracing-architecture.md)); the formal
> spec (see `openspec/specs/observability/spec.md`).

## What v1.5.0 adds

Four new Prometheus histogram families cover the LangGraph pipeline
end-to-end:

| Family | Labels | What it measures |
|---|---|---|
| `zashiki_graph_node_duration_seconds` | `node`, `vertical`, `outcome` | Wall-clock time inside one LangGraph node execution. |
| `zashiki_llm_call_duration_seconds` | `purpose`, `model` | One LLM invocation. Split by purpose so classify vs extract vs summary can be compared independently. |
| `zashiki_external_api_duration_seconds` | `service`, `operation` | One outbound Google / Telegram HTTP call. |
| `zashiki_email_end_to_end_duration_seconds` | `category`, `outcome` | Gmail `internalDate` → Telegram-send completion. The operator-visible latency. |

All four share the same wide-range bucket set: `[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300]` seconds. This covers fast short-circuits (sub-100ms) through slow LLM calls (10s+) up to end-to-end latency including poll-interval contribution (5 min).

Every histogram observation is emitted from a `graph_span` / `llm_call` / `api_call` context manager that ALSO opens an OpenTelemetry span with matching timestamps. Log lines from within the block carry the same `trace_id` / `span_id` (v1.2 log-to-trace correlation), so a p99 outlier in Grafana pivots one click to Tempo, and one more click to Loki.

## Label vocabulary (bounded)

Cardinality is bounded up-front — no runtime label surprises.

- `node` ∈ `{analyze, extract, freebusy, create, dedup_check, notify, persist}` — 7 values enumerated at implementation. `freebusy` and `dedup_check` are reserved for future use (freebusy is currently inside `_create_node`; `dedup_check` lands with v1.4.2).
- `vertical` ∈ `{email, calendar_sg, expense_sg, notify_only}` — 4 values.
- `outcome` ∈ `{success, skipped, error}` — 3 values.
    - `success` — the node completed its intended work.
    - `skipped` — intentional early return (no-event-signal short-circuit, upstream side_effect present, iCalUID duplicate, sparse-draft-needs-review).
    - `error` — LLM failure, API failure, unhandled exception. Auto-flipped when the wrapped block raises.
- `purpose` ∈ `{classify, calendar_extract, expense_extract, subject_translation, body_summary}` — 5 values. Reserved `subject_translation` / `body_summary` are for a future `_analyze` split.
- `service` ∈ `{gmail, calendar, telegram}`; `operation` per service (gmail: `profile / history / message_get / attachment_get`; calendar: `freebusy / list / insert`; telegram: `send_message`).
- `category` — the analyze classifier vocabulary (`會議邀請`, `講座資訊`, `發票`, `廣告`, `技術文章`, ...). `unknown` when analyze failed and no category is known.

## Grafana dashboard

The chart ships `dashboards/zashiki-graph-latency.json` — 4 rows, 8 panels:

1. **End-to-end (operator-visible latency)** — p50/p95/p99 by category + outcomes per 1h.
2. **Graph nodes** — node latency p95 (by vertical.node), throughput (rate), outcome ratio per 1h.
3. **LLM calls (by purpose)** — p50/p95 by purpose + call rate stacked.
4. **External APIs** — p95 by service+operation + call rate stacked.

Enable via helm values:

```yaml
observability:
  dashboards:
    enabled: true
    graphLatency:
      enabled: true   # default when parent is on
```

### Cluster with a working sidecar

The kube-prometheus-stack Grafana sidecar picks up the ConfigMap on next reconcile — dashboard auto-appears in Grafana under **Dashboards → General**.

### Cluster with sidecar reload locked

If Grafana's sidecar reload API is 401-locked (nttu-gpu-lab per memory `reference_kube_prom_stack_sidecar_auth`), manual import is required:

```bash
kubectl -n zashiki get cm zashiki-zashiki-warasi-dashboard-graph-latency \
  -o jsonpath='{.data.zashiki-graph-latency\.json}' > /tmp/dash.json
```

Then in Grafana: **Dashboards → New → Import → Upload JSON file** → `/tmp/dash.json`. Select Prometheus datasource on the import screen.

## Reading the data

### "Where's the time going for one email?"

Compare same-window sums across the four families for a single email trace:

```
sum(rate(zashiki_email_end_to_end_duration_seconds_sum[5m]))     # total
sum by (node) (rate(zashiki_graph_node_duration_seconds_sum[5m])) # per-node
sum by (purpose) (rate(zashiki_llm_call_duration_seconds_sum[5m])) # per-LLM
sum by (service) (rate(zashiki_external_api_duration_seconds_sum[5m])) # per-API
```

Rule of thumb: end-to-end ≈ sum(nodes) + poll-interval-wait. If sum(nodes) is small relative to end-to-end, latency is dominated by the poll loop wait (`* * * * *` cron = up to 60s per email in the worst case).

### "Which node is slowest right now?"

```promql
histogram_quantile(0.95,
  sum by (node, vertical, le) (rate(zashiki_graph_node_duration_seconds_bucket[6h])))
```

Empirically (from post-v1.5.0 deploy), `analyze` p95 sits in the 20-60s range — LLM classify dominates. Verticals typically 1-5s.

### "Is the LLM contended?"

```promql
sum by (purpose) (rate(zashiki_llm_call_duration_seconds_count[5m]))
```

Concurrent purposes = potential contention on the shared LLM endpoint. The stacked-area LLM call rate panel visualizes this directly.

### "Is a specific external API slowing down?"

```promql
histogram_quantile(0.95,
  sum by (service, operation, le) (rate(zashiki_external_api_duration_seconds_bucket[6h])))
```

Especially useful when v1.4.2 dedup lands (adds one more `events.list` call per creation) — this panel shows the incremental cost against a pre-dedup baseline.

## Log-to-trace jump

Every observation shares a timestamp with an OTel span (`graph.<vertical>.<node>`, `llm.<purpose>`, `api.<service>.<operation>`). To pivot from a p99 outlier to the causing email:

1. Click the outlier point in a Grafana panel.
2. Choose "Explore in Tempo" (auto-populated via the derived-field configured in v1.1 — see `reference_grafana_derived_field_ui` memory).
3. Tempo shows the full span tree; the parent span is `zashiki.node.<name>` from `node_trace`, and the graph-latency span sits under it as `graph.<vertical>.<node>`.
4. Click any span → "Logs" → jump to the Loki log stream filtered by that trace_id.

The v1.3 `📋 Copy trace ID` Telegram button also short-cuts this flow: a user seeing an anomalous alert can copy the trace_id straight from Telegram and paste it into Grafana.

## Coexistence with pre-v1.5.0 metrics

The following families from v1.1 remain unchanged so pre-v1.5.0 dashboards keep working:

- `zashiki_tick_duration_seconds` — tick lifecycle.
- `zashiki_gmail_api_latency_seconds{operation}` — Gmail API.
- `zashiki_llm_latency_seconds{node}` — LLM invocations by node.
- `zashiki_telegram_send_total{outcome}` — telegram send counter.

Each LLM call now emits into BOTH the v1.1 histogram (`llm_latency_seconds{node}`) AND the v1.5.0 histogram (`llm_call_duration_seconds{purpose, model}`). Each external Google/Telegram call emits into BOTH the v1.1 counter+histogram AND the v1.5.0 `external_api_duration_seconds`. Two families per observation is a temporary cost; there's no plan to drop the v1.1 families until pre-v1.5.0 dashboards migrate.
