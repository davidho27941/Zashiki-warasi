# Observability(v1.1)

> English:[`observability.md`](observability.md).
>
> **這份文件是什麼**:v1.1 Prometheus metrics、OpenTelemetry tracing、
> 結構化 log 的**操作者面 enable + 參考手冊**。
>
> **這份文件不是**:架構深度解說(見
> [`tracing-architecture.md`](tracing-architecture.md));OTel vs
> Langfuse 選型 rational(見
> [`observability-tool-choice.md`](observability-tool-choice.md));
> 正式 contract spec(見 `openspec/specs/observability/spec.md`)。

## v1.1 提供什麼

三種訊號,全部**operator 端 opt-in**:

| Signal | Endpoint / 機制 | 預設 |
|---|---|---|
| **Prometheus metrics** | `GET /metrics`(永遠 on)| On,但沒 scraper 就沒人收 |
| **OpenTelemetry traces** | OTLP/gRPC 送到指定 collector | Off(`OTEL_ENABLED=0`)|
| **結構化 log** | `LOG_FORMAT=json` → NDJSON 到 stdout | Off(`text` = v1.0 一行格式)|

以下告訴你怎麼開起來、怎麼看。

---

## 路徑 A:Docker Compose 啟用

Compose 內建一整套 observability stack,在 `--profile observability`
guard 底下 —— 一個指令帶起 OTel Collector + Prometheus + Tempo +
Grafana,跟 app 同網路。

### 1. 起 stack

```
cd deploy/compose
docker compose --profile observability up -d
```

5 個 container 跑起來:`zashiki-warasi` + `otel-collector` +
`prometheus` + `tempo` + `grafana`。

### 2. 開 app 的 tracing

編輯 `.env`:

```
OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

重啟 app container:

```
docker compose up -d zashiki-warasi
```

之後每次 `POST /poll` 都會在 Tempo 產生完整 span tree。

### 3. 開 Grafana

- URL:<http://127.0.0.1:3000>(預設 loopback-only,想要對外請看
  compose README)
- Login:`admin` / `.env` 內的 `GRAFANA_ADMIN_PASSWORD`(對外前**先
  換掉 placeholder**)
- Dashboard:`Zashiki-warasi > Zashiki-warasi overview` 自動 import

### 4. (選項)JSON logs

`.env` 設 `LOG_FORMAT=json` 重啟。`docker logs zashiki-warasi` 現在
出 NDJSON —— 可 pipe 進任何結構化 log shipper。

完整細節:[`deploy/compose/README.md`](../deploy/compose/README.md)
的 `Observability profile` 章節。

---

## 路徑 B:Kubernetes(kube-prometheus-stack)啟用

假設 cluster 已有 kube-prometheus-stack(或任何 Prometheus
Operator 部署)。**這個 chart 不部署** Prometheus / Grafana /
Alertmanager 本體 —— 只提供整合 manifest 塞進你既有的 stack。

### 1. 開 ServiceMonitor(metrics scrape)

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi \
    --reuse-values \
    --set observability.serviceMonitor.enabled=true
```

若你的 Prometheus CR `serviceMonitorSelector` 要 label
(通常是 `release: kube-prometheus-stack`):

```
    --set observability.serviceMonitor.additionalLabels.release=kube-prometheus-stack
```

### 2. 開 dashboards

```
    --set observability.dashboards.enabled=true
```

kube-prom-stack 的 Grafana sidecar 抓貼 `grafana_dashboard: "1"`
label 的 ConfigMap,自動 import dashboard。

### 3. 開 alerts(建議收 24h metric 資料再開)

```
    --set observability.prometheusRule.enabled=true
```

單獨調某條 alert 不用碰 PromQL:

```
    --set observability.prometheusRule.alerts.tickErrorRateHigh.ratePerMinute=0.1 \
    --set observability.prometheusRule.alerts.tickConflictRateHigh.enabled=false
```

### 4. 開 tracing(需要 cluster 內有 OTel Collector)

```
    --set env.OTEL_ENABLED=1 \
    --set env.OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc.cluster.local:4317
```

Chart **不部署 Collector** —— 要 cluster 內已有,或另外自架。

完整細節:
[`deploy/helm/zashiki-warasi/README.md`](../deploy/helm/zashiki-warasi/README.md)
的 `Observability` 章節。

---

## Metric 契約

13 個 `zashiki_*` metric families + 標準 `process_*` / `python_gc_*`
(來自 `prometheus-client`)。名字 + label 就是**契約** —— 改名
或改 labelset 會破壞 dashboard + alert。權威表在
`openspec/specs/observability/spec.md`。

| Name | Type | Labels | 意義 |
|---|---|---|---|
| `zashiki_tick_duration_seconds` | Histogram | `outcome` (success\|error) | 一次 `/poll` handler 執行的 wall-clock |
| `zashiki_tick_messages_processed_total` | Counter | — | 累計處理的 Gmail messages |
| `zashiki_tick_conflicts_total` | Counter | — | 累計 409 `tick_in_flight` |
| `zashiki_tick_rebaseline_total` | Counter | — | 累計 Gmail history rebaseline |
| `zashiki_gmail_api_calls_total` | Counter | `operation` (history\|message_get\|attachment_get\|profile), `outcome` | Gmail API 呼叫數 |
| `zashiki_gmail_api_latency_seconds` | Histogram | `operation` | Gmail API 延遲 |
| `zashiki_llm_calls_total` | Counter | `node` (analyze\|expense_extract), `outcome` | LLM 呼叫數 |
| `zashiki_llm_latency_seconds` | Histogram | `node` | LLM 延遲 |
| `zashiki_telegram_send_total` | Counter | `outcome` | Telegram 送信次數 |
| `zashiki_oauth_refresh_total` | Counter | `outcome` | OAuth token refresh 次數 |
| `zashiki_oauth_token_expires_in_seconds` | Gauge | — | 快取的 OAuth token 距離 expiry 幾秒(負值 = 已過期)|
| `zashiki_healthz_status` | Gauge | — | 最近一次 `/healthz` 結果:1 = healthy、0 = unhealthy |
| `zashiki_traces_dropped_total` | Counter | `reason` (queue_full\|export_failed\|shutdown) | OTel BSP 送不出去的 span |

**沒有** generic `zashiki_http_requests_total` —— 設計決策 D16。
每個有診斷價值的 concern 都有專屬 metric;通用 HTTP counter 對這個
app 窄 HTTP 表面沒獨特資訊。

**Cardinality 護欄**:`message_id` / `email` / `user_id` /
`email_address` **禁止**當 metric label —— `_assert_no_forbidden_labels`
在宣告時就擋(見 `src/zashiki_warasi/observability/metrics.py`)。
這些 identifier 屬於 span attribute 或 log context field,不是
metric label。

---

## Span 結構

`POST /poll` 產生大約 **15-30 spans / tick**。契約 shape:

```
POST /poll                          [SERVER, 自動 — FastAPIInstrumentor]
└── zashiki.tick_once               [INTERNAL, 屬性從 TickResult 抽]
    ├── HTTP GET /gmail/.../profile [CLIENT, 自動 — httpx instrumentation]
    ├── HTTP GET /gmail/.../history [CLIENT, 自動]
    ├── HTTP GET /gmail/.../messages/... [CLIENT, 自動]
    ├── zashiki.node.analyze        [INTERNAL, zashiki.message_id 屬性]
    │   ├── zashiki.llm.chat        [INTERNAL, GenAI semconv 屬性]
    │   │   └── HTTP POST /v1/chat/completions [CLIENT, 自動]
    │   └── ... (psycopg 若 instrument 就有 DB span)
    ├── zashiki.node.expense_extract [INTERNAL]
    │   ├── zashiki.llm.chat
    │   │   └── HTTP POST /v1/chat/completions
    │   └── ...
    └── zashiki.notify.telegram     [INTERNAL, messaging.system=telegram]
        └── HTTP POST /bot.../sendMessage [CLIENT, 自動]
```

**409 conflict 短路**:advisory-lock 沒拿到時,`zashiki.tick_once`
**不會 open** —— 只有 SERVER span 存在,`http.status_code=409`。

**LangChain / LangGraph 內建 span 已關**(D23),`configure_tracing`
啟動時設 `LANGCHAIN_TRACING_V2=false` + `LANGSMITH_TRACING=false`,
避免 `langchain.chat_model` / `langgraph.node.*` 跟我們手埋的 span
重複。

**Cron 打的 `/poll` 沒 upstream traceparent**:每個 tick 是 root
trace。若未來 cron / scheduler 開始送 `traceparent` header,我們的
OTel instrumentation 會 honor 並自動掛到那條 trace 底下 —— 不用改
code。

完整屬性契約:
[`tracing-architecture.md`](tracing-architecture.md) `§3 Span 契約`。

---

## JSON log 格式

用 `LOG_FORMAT=json` 開啟。每行一個 JSON object(NDJSON)。契約:

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

欄位:
- `timestamp`(必有)—— ISO-8601 UTC、millisecond 精度、`Z` 後綴
- `level`(必有)—— 大寫(`DEBUG` / `INFO` / ...)
- `logger`(必有)—— 點分 Python logger 名
- `message`(必有)—— `%`-substitution 後的字串
- `request_id`(在 HTTP request context 內有)—— 從 `X-Request-ID`
  header 或自動 12-hex UUID
- `trace_id` / `span_id`(有 OTel span 時有)—— 32-hex / 16-hex
  小寫,對齊 W3C traceparent
- `message_id` / `thread_id` / `expense_id`(在 per-message 處理
  scope 內有)
- 有 exception(`exc_info`)時多 `traceback` 字串 + `exception` 物件
  (含 `type` + `message`)

**缺席欄位 OMIT**,不會給 `null`。Loki / Vector grep-by-key 不用處理
null 過濾。

**中文字元原樣保留** —— `ensure_ascii=False` 是硬需求。`消費支出`
就是 `消費支出`,不會變 `消費支出`。

**非 JSON-serializable 的 extra 不會 crash log call** —— `json.dumps`
用 `default=str` fallback。塞 `Decimal(42.50)` 或 `datetime` 在
`extra=` 進去,會以 `str()` 值 render;emit log 的 code path 照常
繼續。

---

## Tuning 旋鈕

### Sampling ratio

預設 `OTEL_TRACES_SAMPLER_ARG=1.0`(100% sample)。homelab 量級
綽綽有餘(~1500 traces/day × 20 spans ≈ 15 MB/day)。

流量爆大時降到 `0.1`:

```
OTEL_TRACES_SAMPLER_ARG=0.1
```

`parentbased_traceidratio`(預設 sampler)意思:進來的 request 若
帶 `traceparent`,繼承上游決定;否則用 ratio。Compose(只有 cron)
永遠走 ratio branch,因為 cron 不送 `traceparent`。

### Prometheus retention(compose only)

預設 7d,`.env` 改:

```
PROMETHEUS_RETENTION_TIME=30d
```

現有 metric 形狀下的 disk 用量:

| Retention | 大約 disk |
|---|---|
| 7d(預設)| < 100 MB |
| 30d | < 500 MB |
| 90d | < 2 GB |

Kubernetes 部署不 ship Prometheus —— retention 由你 kube-prometheus-
stack values(`prometheus.prometheusSpec.retention`)控,不是這個
chart。

### Per-alert 門檻調整(Kubernetes)

5 條 starter alert 可個別開關 + 調門檻,不用改 PromQL:

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi --reuse-values \
    --set observability.prometheusRule.alerts.tickErrorRateHigh.ratePerMinute=0.1 \
    --set observability.prometheusRule.alerts.tickConflictRateHigh.for=30m \
    --set observability.prometheusRule.alerts.tickNoSuccessfulTick.severity=critical
```

在 starter 旁邊加自訂 alert(不覆蓋 starter):

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

### Per-dashboard toggle(Kubernetes)

每張內建 dashboard 有獨立 `enabled` flag 在
`observability.dashboards.<name>` 底下。關掉 → ConfigMap 不 render
→ kube-prom-stack Grafana sidecar 下次 reconcile 自動移除 Grafana
面板。不用進 UI 手動刪。

---

## Troubleshooting

### 「我開了 OTel 但 Tempo 沒 trace」

按這順序查:

1. **上游有送 `traceparent` header 嗎?** cron 情境不相關,但若你
   從 script 測且該 script 帶 traceparent,值得排除
2. **Sampler 是不是 `AlwaysOff` 或 `0`?** 檢查
   `OTEL_TRACES_SAMPLER_ARG`
3. **Collector 通嗎?** 看 `zashiki_traces_dropped_total` metric —
   `reason=export_failed` 非零就代表 app 試過送但失敗。看 app log
   有沒有 rate-limited WARNING 提 endpoint
4. **Collector 有 forward 到 Tempo 嗎?** 看 collector log
   (compose:`docker logs zashiki-otel-collector`)是否有給 Tempo
   的 export 錯誤
5. **Trace sample 到了但 Tempo 還沒 index?** Tempo indexing 落後
   幾秒 —— 在 Grafana Explore retry

### 「Scrape 有動但 dashboard 沒資料」

- ServiceMonitor 的 `job` label 有沒有跟 alert 查詢一致?
  `helm template ...` 然後 grep `relabelings:` 底下的 `replacement:`
- 若 Prometheus 依 label filter ServiceMonitor
  (`release: kube-prometheus-stack`),記得
  `observability.serviceMonitor.additionalLabels.release=kube-prometheus-stack`

### 「App log `WEB_CONCURRENCY=N is unsupported`」

設計 D18 fail-fast。`prometheus_client` registry 是 process-local,
多 worker 讓 counter 分裂到各 worker,scrape 契約破功。unset
`WEB_CONCURRENCY` 或設 `1`。真需要多 worker 吞吐是另一個 change
(要加 `PROMETHEUS_MULTIPROC_DIR` mode 支援,目前沒 ship)。

### 「App 因 `OTEL_RESOURCE_ATTRIBUTES` secret guard 拒絕啟動」

設計 D22 fail-fast。`OTEL_RESOURCE_ATTRIBUTES` 內某個值撞到已知
secret 前綴(`sk-`、`ghp_`、`AIza`、`xoxb-`、`AKIA`)或看起來像
base64(≥ 32 char)。Resource attribute 掛在**每一個** exported
span 上 —— secret 在裡面會永久 leak 到 Tempo。移除 offending
key/value。Log line **只印 key、不印 value**(避免 diagnostic 訊息
自己變 leak 通道)。

### 「Log 缺 trace_id 即使 OTel 開了」

Trace context factory 只在**當下有 active OTel span** 時才設
`trace_id` / `span_id`。Bootstrap log(`configure_logging` 完成前)
和背景 thread 內 log(無 tracing context)正確地不會有這些欄位。

---

## 延伸閱讀

- [`tracing-architecture.md`](tracing-architecture.md) —— tracing
  子系統完整架構、設計決策、testing 策略、實作期間三個踩雷
- [`observability-tool-choice.md`](observability-tool-choice.md) ——
  OTel/Tempo vs Langfuse 選型 rationale、什麼時候該重評估
- [`../deploy/compose/README.md`](../deploy/compose/README.md) ——
  compose 部署 quickstart + observability profile 細節
- [`../deploy/helm/zashiki-warasi/README.md`](../deploy/helm/zashiki-warasi/README.md)
  —— chart 級 knob 參考 + alert severity → routing plan
- `openspec/specs/observability/spec.md` —— 正式契約
- `docs/lessons/2026-08-11-basehttp-middleware-vs-otel-tracing.md`
  —— 為何要 pure ASGI middleware(D17)
- `docs/lessons/2026-08-12-prometheus-multi-worker.md` —— 為何
  `WEB_CONCURRENCY>1` 要 fail-fast(D18)
