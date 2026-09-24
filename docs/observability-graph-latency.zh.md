# 節點延遲觀測 (v1.5.0)

> English: [`observability-graph-latency.md`](observability-graph-latency.md).
>
> **這份文件是什麼**:v1.5.0 每個 LangGraph 節點延遲量測的 operator 手冊 —— 四個 Prometheus histogram 家族、一份 Grafana dashboard、以及對應的 PromQL 入口。
>
> **不是什麼**:v1.1 metric 契約(見 [`observability.zh.md`](observability.zh.md));trace 架構(見 [`tracing-architecture.md`](tracing-architecture.md));正式 spec(見 `openspec/specs/observability/spec.md`)。

## v1.5.0 帶來的新東西

四個新 Prometheus histogram 家族,完整覆蓋 pipeline:

| 家族 | Labels | 量測內容 |
|---|---|---|
| `zashiki_graph_node_duration_seconds` | `node`, `vertical`, `outcome` | 一次 LangGraph 節點執行的 wall-clock 時間 |
| `zashiki_llm_call_duration_seconds` | `purpose`, `model` | 一次 LLM 呼叫。按用途拆分,方便對照 classify vs extract vs summary |
| `zashiki_external_api_duration_seconds` | `service`, `operation` | 一次對外 Google / Telegram HTTP 呼叫 |
| `zashiki_email_end_to_end_duration_seconds` | `category`, `outcome` | Gmail `internalDate` → Telegram 送出完成。使用者感受到的總延遲 |

四個共用同一組 bucket:`[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300]` 秒。覆蓋短路徑(sub-100ms)、慢 LLM(10s+)、含 poll 間隔的端到端(最多 5 分鐘)。

每個 histogram 觀察都由 `graph_span` / `llm_call` / `api_call` context manager 發出,同時開一個時間戳對齊的 OpenTelemetry span。區塊內的 log 帶同一組 `trace_id` / `span_id`(v1.2 log-to-trace correlation),Grafana p99 outlier 一鍵跳到 Tempo,再一鍵到 Loki。

## Label 邊界(有界)

Cardinality 預先劃死 —— 沒有 runtime label 意外。

- `node` ∈ `{analyze, extract, freebusy, create, dedup_check, notify, persist}` —— 7 個值。`freebusy` 和 `dedup_check` 保留給未來(freebusy 目前在 `_create_node` 內部;`dedup_check` 隨 v1.4.2 上線)。
- `vertical` ∈ `{email, calendar_sg, expense_sg, notify_only}` —— 4 個。
- `outcome` ∈ `{success, skipped, error}`。
    - `success` —— 節點完成該做的事
    - `skipped` —— 有意的 early return(no_event_signal 短路、上游 side_effect、iCalUID 重複、sparse draft)
    - `error` —— LLM 失敗、API 失敗、未 catch 的 exception。區塊 raise 時自動翻轉
- `purpose` ∈ `{classify, calendar_extract, expense_extract, subject_translation, body_summary}` —— 5 個。`subject_translation` / `body_summary` 保留給未來的 `_analyze` 拆分方案。
- `service` ∈ `{gmail, calendar, telegram}`;`operation` 各服務對應(gmail: `profile / history / message_get / attachment_get`;calendar: `freebusy / list / insert`;telegram: `send_message`)。
- `category` —— analyze 分類器的類別詞彙(`會議邀請`, `講座資訊`, `發票`, `廣告`, `技術文章`, ...)。analyze 失敗時用 `unknown`。

## Grafana Dashboard

Chart 內建 `dashboards/zashiki-graph-latency.json` —— 4 row / 8 panel:

1. **端到端(使用者感受)** —— category 分 p50/p95/p99 + 每小時 outcome 分佈
2. **Graph nodes** —— node latency p95(vertical.node)+ throughput + 每小時 outcome 比例
3. **LLM 呼叫(按 purpose)** —— purpose 分 p50/p95 + call rate 堆疊
4. **External APIs** —— service+operation 分 p95 + call rate 堆疊

helm values 開關:

```yaml
observability:
  dashboards:
    enabled: true
    graphLatency:
      enabled: true   # parent 開時預設 true
```

### Sidecar 正常的 cluster

kube-prometheus-stack Grafana sidecar 下一次 reconcile 會自動掃到 ConfigMap,dashboard 直接出現在 Grafana **Dashboards → General**。

### Sidecar reload 被鎖的 cluster

Grafana sidecar reload API 401-locked 的情況(nttu-gpu-lab 依 memory `reference_kube_prom_stack_sidecar_auth`),需要手動 import:

```bash
kubectl -n zashiki get cm zashiki-zashiki-warasi-dashboard-graph-latency \
  -o jsonpath='{.data.zashiki-graph-latency\.json}' > /tmp/dash.json
```

Grafana 內:**Dashboards → New → Import → Upload JSON file** → `/tmp/dash.json`,import 頁選 Prometheus datasource。

## 資料怎麼讀

### 「一封 email 的時間都花去哪?」

比對四個家族在同一時間窗的 sum:

```
sum(rate(zashiki_email_end_to_end_duration_seconds_sum[5m]))     # 總計
sum by (node) (rate(zashiki_graph_node_duration_seconds_sum[5m])) # 每節點
sum by (purpose) (rate(zashiki_llm_call_duration_seconds_sum[5m])) # 每 LLM
sum by (service) (rate(zashiki_external_api_duration_seconds_sum[5m])) # 每 API
```

判斷準則:端到端 ≈ sum(nodes) + poll 等待時間。若 sum(nodes) 遠小於端到端,延遲主要卡在 poll 間隔上(`* * * * *` cron 最壞情況 60s 才處理到某封 email)。

### 「現在哪個節點最慢?」

```promql
histogram_quantile(0.95,
  sum by (node, vertical, le) (rate(zashiki_graph_node_duration_seconds_bucket[6h])))
```

實測(v1.5.0 上線後)`analyze` p95 落在 20-60 秒 —— 全被 LLM classify 主宰。verticals 通常 1-5s。

### 「LLM 有沒有塞車?」

```promql
sum by (purpose) (rate(zashiki_llm_call_duration_seconds_count[5m]))
```

多個 purpose 同時活躍 = 潛在 LLM contention。dashboard 的 LLM call rate 堆疊 panel 可視化這個。

### 「某個 external API 變慢了嗎?」

```promql
histogram_quantile(0.95,
  sum by (service, operation, le) (rate(zashiki_external_api_duration_seconds_bucket[6h])))
```

v1.4.2 dedup 上線時特別有用(每次建立多一次 `events.list`)—— 可以拿去對比 pre-dedup baseline 的成本增量。

## Log-to-trace 跳轉

每筆觀察都跟一個 OTel span 共時間戳(`graph.<vertical>.<node>`, `llm.<purpose>`, `api.<service>.<operation>`)。從 p99 outlier 追到肇事 email:

1. Grafana panel 內點該離群點
2. 選「Explore in Tempo」(v1.1 已配好 derived-field —— 見 `reference_grafana_derived_field_ui` memory)
3. Tempo 顯示完整 span tree;parent span 是 `node_trace` 打的 `zashiki.node.<name>`,graph-latency span 掛在下面 `graph.<vertical>.<node>`
4. 點任一 span → 「Logs」→ 跳到 Loki 對應 trace_id 的 log stream

v1.3 `📋 Copy trace ID` Telegram 按鈕也是同一條捷徑:使用者看到異常通知,直接複製 trace_id 貼到 Grafana。

## 與 pre-v1.5.0 metrics 共存

以下 v1.1 家族保持不變,舊 dashboard 不斷資料:

- `zashiki_tick_duration_seconds` —— tick 生命週期
- `zashiki_gmail_api_latency_seconds{operation}` —— Gmail API
- `zashiki_llm_latency_seconds{node}` —— LLM 按節點
- `zashiki_telegram_send_total{outcome}` —— telegram 發送 counter

每次 LLM 呼叫**同時**寫入 v1.1(`llm_latency_seconds{node}`)和 v1.5.0(`llm_call_duration_seconds{purpose, model}`)兩個家族。外部 Google/Telegram 呼叫同理。一次觀察兩個家族是暫時代價 —— pre-v1.5.0 dashboard 遷移完後再考慮下線 v1.1 家族。
