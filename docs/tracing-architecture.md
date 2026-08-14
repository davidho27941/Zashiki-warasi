# v1.1 Tracing 架構文件

> **這份文件是什麼**:v1.1 `add-observability-stack` change 的
> Group 4(a + b)所建立的 OpenTelemetry tracing 子系統的完整架構
> 與設計說明。目的是讓未來的讀者(自己、新 contributor、operator)
> 不用重讀所有 commit message 就能掌握全景。
>
> **前置知識**:讀者應該熟悉 OpenTelemetry 基本概念(tracer /
> span / TracerProvider / SpanProcessor / Exporter / Resource /
> Sampler)以及 v1.0 的 FastAPI service 架構。如果你不熟 OTel,
> 建議先看 [OTel Python 官方 doc](https://opentelemetry.io/docs/languages/python/)
> 的 Instrumentation 章節。
>
> **這份文件的定位**:設計 + 運維 reference。**不是** tutorial(那是
> `docs/observability.md` 的事,由 change 的 Group 8 task 8.3 產出)。
> 這裡的重點是「為什麼這樣寫」跟「哪裡有暗雷」。

## 目錄

1. [全景圖](#1-全景圖)
2. [三個核心模組](#2-三個核心模組)
3. [Span 契約](#3-span-契約)
4. [兩個 fail-fast guardrails](#4-兩個-fail-fast-guardrails)
5. [Rate-limited BSP + traces_dropped 觀察機制](#5-rate-limited-bsp--traces_dropped-觀察機制)
6. [Testing 策略與踩過的雷](#6-testing-策略與踩過的雷)
7. [運維手冊](#7-運維手冊)
8. [已知限制 / defer 事項](#8-已知限制--defer-事項)
9. [相關文件 & 決策紀錄](#9-相關文件--決策紀錄)

---

## 1. 全景圖

### 1.1 系統架構圖

```
┌─────────────────────────────────────────────────────────────────┐
│                     zashiki-warasi 執行環境                       │
│                                                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  FastAPI process (single worker,強制)                     │  │
│  │                                                             │  │
│  │  ┌──────────────────────────────────────────────────┐     │  │
│  │  │  Middleware chain(pure ASGI,不用 BaseHTTP)     │     │  │
│  │  │  OpenTelemetryMiddleware(外層,SERVER span)      │     │  │
│  │  │    → RequestIdMiddleware                          │     │  │
│  │  │      → FastAPI routes                             │     │  │
│  │  │        → /poll: zashiki.tick_once                 │     │  │
│  │  │          → gmail (httpx CLIENT span 自動)         │     │  │
│  │  │          → llm.chat: zashiki.llm.chat + GenAI attr│     │  │
│  │  │          → node.analyze: zashiki.node.analyze    │     │  │
│  │  │          → telegram: zashiki.notify.telegram      │     │  │
│  │  │          → db (psycopg CLIENT span 自動)          │     │  │
│  │  └──────────────────────────────────────────────────┘     │  │
│  │                          │                                 │  │
│  │                          │ span 從 tracer 產生             │  │
│  │                          ▼                                 │  │
│  │  ┌──────────────────────────────────────────────────┐     │  │
│  │  │  TracerProvider(全域 singleton)                   │     │  │
│  │  │  ├─ Resource: service.name/version/instance.id    │     │  │
│  │  │  └─ Sampler: parentbased_traceidratio(default 1.0)│     │  │
│  │  └──────────────────────────────────────────────────┘     │  │
│  │                          │                                 │  │
│  │                          │ span → SpanProcessor            │  │
│  │                          ▼                                 │  │
│  │  ┌──────────────────────────────────────────────────┐     │  │
│  │  │  _RateLimitedBatchSpanProcessor(BSP 子類)         │     │  │
│  │  │  ├─ queue(memory-bounded)                         │     │  │
│  │  │  ├─ 背景 worker thread                            │     │  │
│  │  │  ├─ 攔 export failure → traces_dropped_total     │     │  │
│  │  │  └─ Rate-limited WARNING(每 reason 1/min)        │     │  │
│  │  └──────────────────────────────────────────────────┘     │  │
│  │                          │                                 │  │
│  │                          │ OTLP/gRPC(protobuf)             │  │
│  └──────────────────────────┼─────────────────────────────────┘  │
│                             │                                    │
└─────────────────────────────┼────────────────────────────────────┘
                              ▼
              ┌───────────────────────────────┐
              │  OTel Collector(外部)         │
              │  (compose profile 或 k3s 自架) │
              └───────────────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │  Grafana Tempo(儲存)          │
              └───────────────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │  Grafana UI(查詢 + 視覺化)    │
              └───────────────────────────────┘
```

### 1.2 資料流

一個 `POST /poll` request 進來,產生的 telemetry:

1. **request 進 uvicorn** → `OpenTelemetryMiddleware`(由 `FastAPIInstrumentor.instrument_app(app)` 加的)開 SERVER span,name=`POST /poll`,從 header 讀 `traceparent`(若有)
2. **`RequestIdMiddleware`(pure ASGI)** 執行,`_REQUEST_ID_CTX` 設 request_id
3. **`poll` handler** 執行,`asyncio.to_thread(_do_tick)` 進 threadpool(contextvars.copy_context() 把當下 span 一併帶過去)
4. **`advisory_lock` 拿到後**,`with zashiki_span("tick_once")` 開 INTERNAL span,parent=SERVER span
5. **`tick_once` 內部**:
   - `gmail.get_profile()` → httpx CLIENT span(自動,`HTTPXClientInstrumentor` 掛的)
   - `gmail.list_history()` 每頁 → httpx CLIENT span
   - `gmail.get_message()` → httpx CLIENT span
   - `analyze` node 呼叫 → `with node_trace(log, "analyze")` 開 `zashiki.node.analyze` INTERNAL span
     - 內部 `with zashiki_span("llm.chat")` 開 `zashiki.llm.chat` INTERNAL span,`set_gen_ai_attributes(...)` 設 GenAI 屬性
     - 內部 httpx.post → CLIENT span
   - `expense_extract` node 同樣結構
   - `notifier.send_message()` → `zashiki.notify.telegram` INTERNAL span + 內部 httpx CLIENT span
   - `checkpointer` DB 寫 → psycopg CLIENT span(自動)
6. **`tick_once` return** → `_set_tick_span_attributes(span, result)` 把 4 個業務欄位設上 tick_once span
7. **`tick_once` span __exit__** → 標 end time → 交給 BSP queue
8. **SERVER span __exit__** → 標 end time + status → BSP queue
9. **BSP 背景 thread** 定期(或滿 batch)拉 queue,用 OTLPSpanExporter 送到 collector
10. **Collector** 收到,forward 給 Tempo,operator 從 Grafana 查

一個 `/poll` 大約產生 **15-30 個 span**(視這輪處理幾封信、每封走到哪個 node 而定)。

### 1.3 兩個 code path:enabled vs disabled

| 情境 | `OTEL_ENABLED=0`(default) | `OTEL_ENABLED=1` |
|---|---|---|
| SDK import | **沒有**(lazy) | ~20-40 MB |
| TracerProvider | NoOp(default) | 真的 SDK `TracerProvider` |
| 每個 request 開的 span | NoOp(near-zero cost) | 真 span 進 queue |
| 背景 thread | 無 | 一條 BSP worker |
| 網路開銷 | 零 | OTLP/gRPC 到 collector |
| Operator 看得到 | 只有 `/metrics` | `/metrics` + Tempo trace explorer |

---

## 2. 三個核心模組

### 2.1 `observability/tracing.py` — bootstrap 中樞

**入口**:`configure_tracing(settings: ObservabilitySettings | None = None, *, log=None)`

**呼叫時機**:一次,`web/app.py::lifespan` 啟動時,在 `configure_logging()` 之後、`build_services()` 之前(這樣 psycopg instrumentation 已在,checkpointer pool 開 connection 時就會被 trace)。

**主要職責分工**:

| 函式 | 責任 |
|---|---|
| `configure_tracing()` | 主入口,協調所有 step,`OTEL_ENABLED=0` 時 short-circuit |
| `check_web_concurrency()` | D18 guardrail(見 §4.1) |
| `_guard_resource_attributes_secrets()` | D22 guardrail(見 §4.2) |
| `_disable_langchain_internal_tracing()` | D23 執行(見 §3.1) |
| `_build_resource()` | 組 `Resource`(`service.name/version/instance.id`) |
| `_parse_resource_attributes()` | 解析 OTel 標準 `k=v,k=v` |
| `_build_sampler()` | 4 種 sampler 名字對應到 SDK class |
| `_make_rate_limited_bsp()` | 建 BSP 子類(見 §5) |
| `_instrument_libraries()` | 掛 httpx + psycopg instrumentation |

**關鍵設計:lazy import**

```python
def configure_tracing(settings, *, log=None):
    ...
    if not settings.otel_enabled:
        return   # ← 這裡就結束,下面完全沒被執行
    
    # 只有 enabled path 才會 reach 這裡,SDK 才 import
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    ...
```

**為什麼**:opt-out operator 零 SDK 開銷,連 20-40 MB 的 module import 都不用付。實際測試 `python -c "import zashiki_warasi.web.app"` 在 `OTEL_ENABLED=0` 下,`opentelemetry.sdk` 從沒被 load(可用 `sys.modules` 檢查)。

### 2.2 `observability/instrumentation.py` — 埋點 helpers

**目的**:把「開 span + 設屬性 + 記 metric」這些重複的 pattern 抽成 helper,call site 只寫一行 `with`。

**Helper 目錄**:

| Helper | 用途 | 用在哪 |
|---|---|---|
| `record_call(counter, histogram, ...)` | metric-only:計算 outcome + latency | gmail_api、llm(組合)|
| `observe_outcome(counter, ...)` | metric-only:只計 outcome,不量 latency | telegram、oauth_refresh |
| `zashiki_span(name, attributes=None)` | trace-only:開 `zashiki.<name>` span,yield 讓 caller 加屬性 | tick_once、notify.telegram、llm.chat |
| `set_gen_ai_attributes(span, system, model, response=None)` | 給 llm span 掛 GenAI semconv 屬性 | analyze、expense_extract |
| `_safe_set_attribute(span, key, value)` | span 屬性設定的容錯 wrapper | 內部使用 |

**設計選擇:span + metric 不合成一個 helper**

考慮過寫一個 `record_llm_call(node, model)` 一次做完 span + metric 兩件事。**沒選這條**因為:

- LLM 呼叫需要 GenAI 屬性(model 名字等),Gmail / Telegram 不需要,強行合成一個 helper 就要吃 5 個 optional 參數,亂
- 兩個 concern 各自演化速度不同(metric 契約已鎖、span 屬性可能會補),分開好維護
- Call site 寫 `with zashiki_span(...), record_call(...):` 兩層 context manager 語法不長,`with a, b:` 是 Python 官方支援的合成

**設計選擇:`_safe_set_attribute` 的存在**

OTel Attribute type 有 strict 限制(`str` / `bool` / `int` / `float` / 或這些的 `Sequence`),丟 `datetime` / `Decimal` / `dict` 進去會 raise `TypeError`。這種錯誤**絕對不能讓 request 死**(telemetry 是輔助,不能反過來破壞主流程)。

```python
def _safe_set_attribute(span, key, value):
    try:
        span.set_attribute(key, value)
    except Exception:
        try:
            span.set_attribute(key, str(value))
        except Exception:
            pass  # 給不出就算了,不能 raise
```

**設計選擇:`set_gen_ai_attributes` 對 response usage 極度容錯**

LangChain 的 response usage metadata 出現在多個位置且格式不穩:

- `AIMessage.usage_metadata`(新版標準)
- `AIMessage.response_metadata["token_usage"]`(舊版)
- `AIMessage.response_metadata["usage"]`(某些 provider)
- 而且 dict / attribute 兩種存取都可能

helper 逐一嘗試,任一存在就用,都拿不到就靜默跳過(**structured-output paths 通常就是拿不到**)。這是「fail open」的觀察性 —— 有資料就記,沒資料不會炸,不影響業務。

### 2.3 `core/logging.py::node_trace` — 擴充成 log + span 二合一

**動機**:v1.0 已經有 `node_trace(log, name)` context manager,現有 4 個 node(analyze、expense_extract、expense_persist、notify)都用它發 DEBUG entry/exit log。**擴充它同時發 OTel span**,現有所有 call site 零改動就自動被 trace。

**擴充後行為**:

```python
with node_trace(log, "analyze"):
    # 這個 with block 現在同時:
    # 1. 發 DEBUG "node=analyze enter" log(v1.0 就有)
    # 2. 開 zashiki.node.analyze OTel span(新加)
    # 3. 從 log(如果是 LoggerAdapter)讀 message_id → zashiki.message_id 屬性
    # 4. 從 _REQUEST_ID_CTX contextvar 讀 request_id → zashiki.request_id 屬性
    # 5. exception 時 → span.set_status(ERROR) + span.record_exception + DEBUG exit_error log + re-raise
    # 6. 正常結束 → DEBUG "node=analyze exit elapsed_ms=X" log + span __exit__
    ...
```

**設計選擇:為什麼在 `core/logging.py` 加而不是 `observability/`**

- 這個 helper 從 v1.0 就住這裡,已經被 5 個檔案 import
- 改在原地擴充 = call site 零改動
- 搬到 `observability/` 會需要改 5 個 import,加 test 涵蓋,收益是「模組界線純」但代價太大

**設計選擇:為什麼不用 OTel 官方 decorator**

OTel 有 `@tracer.start_as_current_span` 的 decorator 用法。**沒選**因為:

- Decorator 綁在 function 上,拿不到 runtime 的 message_id / request_id 屬性(要另外寫 code 讀)
- context manager 顯式 `with node_trace(log, name):` 更清楚看到 span 的 scope 是哪一段
- 保留跟原 DEBUG log 邏輯的合體,比拆兩層乾淨

---

## 3. Span 契約

### 3.1 Span 命名

| Name | Kind | 什麼時候開 |
|---|---|---|
| `POST /poll` | SERVER | FastAPIInstrumentor 自動,涵蓋整個 request 生命週期 |
| `GET /healthz` | SERVER | 同上 |
| `GET /metrics` | SERVER | 同上 |
| `zashiki.tick_once` | INTERNAL | `poll` handler 拿到 advisory lock **之後**開,涵蓋 `tick_once()` 執行 |
| `zashiki.node.analyze` | INTERNAL | `_analyze` 執行時,`node_trace` 開 |
| `zashiki.node.expense_extract` | INTERNAL | 同上 |
| `zashiki.node.expense_persist` | INTERNAL | 同上 |
| `zashiki.node.notify` | INTERNAL | 同上 |
| `zashiki.llm.chat` | INTERNAL | analyze / expense_extract 呼叫 LLM 時開,parent 是 node span |
| `zashiki.notify.telegram` | INTERNAL | telegram send_message 呼叫時開 |
| `HTTP <METHOD>`(httpx auto)| CLIENT | `HTTPXClientInstrumentor` 對每個 httpx 呼叫自動開 |
| `<sql_operation>`(psycopg auto)| CLIENT | `PsycopgInstrumentor` 對每個 SQL 執行自動開 |

**409 短路規則**:`POST /poll` 收到 409 conflict 時,`zashiki.tick_once` span **不會開**(因為 tick body 沒執行)。只有 SERVER span 存在,attribute 上 `http.status_code=409`。

**LangChain / LangGraph 的 `langchain.*` / `langgraph.*` span**:靠 D23 (`LANGCHAIN_TRACING_V2=false` + `LANGSMITH_TRACING=false`)禁掉,不會出現。

### 3.2 屬性契約

#### `zashiki.tick_once` span

| Attribute | Type | 何時設 |
|---|---|---|
| `zashiki.messages_processed` | int | 每次 tick,從 TickResult |
| `zashiki.cursor_before` | int(可能無) | 有值才設(baseline / rebaseline 時無) |
| `zashiki.cursor_after` | int(可能無) | 有值才設 |
| `zashiki.rebaselined` | bool | 每次 tick,從 TickResult |
| `zashiki.error` | str(可能無) | TickResult.error 非空時才設 |

同時 tick 失敗時 span 標 `Status.ERROR`,message = TickResult.error。

#### `zashiki.node.*` span

| Attribute | 來源 |
|---|---|
| `zashiki.message_id` | LoggerAdapter.extra 有 `message_id` 時 |
| `zashiki.request_id` | `_REQUEST_ID_CTX` contextvar 有值時 |

exception 傳出時 span 標 `Status.ERROR` + `record_exception(exc)`。

#### `zashiki.llm.chat` span

遵循 [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

| Attribute | 何時設 | 來源 |
|---|---|---|
| `gen_ai.system` | invoke 前 | LLMSettings.provider(`llamacpp` / `openai` / `anthropic`) |
| `gen_ai.request.model` | invoke 前 | LLMSettings.model |
| `gen_ai.usage.input_tokens` | invoke 後,若 response 有 | LangChain response 的 usage metadata |
| `gen_ai.usage.output_tokens` | invoke 後,若 response 有 | 同上 |

structured-output paths(analyze / expense_extract 走這條)通常拿不到 usage —— helper 靜默跳過,不會出錯。

#### `zashiki.notify.telegram` span

| Attribute | 值 |
|---|---|
| `messaging.system` | `"telegram"` |

### 3.3 Span tree 拓撲(範例:一個成功的 `/poll` 處理 1 封 message,走 expense vertical)

```
POST /poll  [SERVER, http.method=POST, http.status_code=200]
└── zashiki.tick_once  [INTERNAL, zashiki.messages_processed=1, ...]
    ├── HTTP GET /gmail/v1/users/me/profile  [CLIENT, httpx]
    ├── HTTP GET /gmail/v1/users/me/history  [CLIENT, httpx]
    ├── HTTP GET /gmail/v1/users/me/messages/xyz  [CLIENT, httpx]
    ├── zashiki.node.analyze  [INTERNAL, zashiki.message_id=xyz]
    │   ├── zashiki.llm.chat  [INTERNAL, gen_ai.request.model=qwen-2.5:7b]
    │   │   └── HTTP POST /v1/chat/completions  [CLIENT, httpx]
    │   └── ... (寫 DB 的 psycopg span,若有)
    ├── zashiki.node.expense_extract  [INTERNAL, zashiki.message_id=xyz]
    │   ├── zashiki.llm.chat
    │   │   └── HTTP POST /v1/chat/completions
    │   └── ... (Notion / DB spans)
    └── zashiki.notify.telegram  [INTERNAL, messaging.system=telegram]
        └── HTTP POST /bot<token>/sendMessage  [CLIENT, httpx]
```

---

## 4. 兩個 fail-fast guardrails

### 4.1 `check_web_concurrency()` — D18

**規則**:`WEB_CONCURRENCY > 1` 或非整數 → CRITICAL log + `sys.exit(1)`,連 HTTP listener 都不開。

**背景**:`prometheus_client` 的 registry 是 process-local。uvicorn `--workers N` 底下是 fork 出 N 個獨立 process,每個都有自己的 registry,scrape 隨機打到某個 worker 拿到區域快照 → counter 對 Prometheus 契約破功(單調遞增假設不成立)。

**為什麼「禁」而不是「支援多 worker(用 `PROMETHEUS_MULTIPROC_DIR`)」**:

- v1.0 workload 極低(每 60 秒一個 tick),單 worker 遠遠夠
- 多 worker 支援要加 ~300 行 code + 跨環境 setup(shared dir)
- 需要 scale-out 時,`replicaCount` 已經是驗過的路(每 pod 一 process,各自 registry,Prometheus 各 pod 各 scrape,天然一致)

**為什麼 fail-fast 而不是 warning**:

- 「悄悄失效的 metric」是 debug 惡夢(operator 幾週後才發現 dashboard 亂跳)
- 5 行 code 換「不可能悄悄踩雷」,CP 值極高
- 若哪天要多 worker,再開一個 change 加 `PROMETHEUS_MULTIPROC_DIR` 支援,這條 guard 就是那個 change 該打通的入口

**完整文件**:`docs/lessons/2026-08-12-prometheus-multi-worker.md`

### 4.2 `_guard_resource_attributes_secrets()` — D22

**規則**:`OTEL_RESOURCE_ATTRIBUTES` 的 value 掃到已知 secret 前綴或 base64 shape → CRITICAL log(**只印 key + 匹配的 pattern,不印 value**)→ `sys.exit(1)`。

**背景**:Resource attribute 掛在**每一個** exported span 上。誤設 `OTEL_RESOURCE_ATTRIBUTES=api_token=sk-abc123` 會讓那個 token 出現在:
- Tempo 儲存的所有 trace(long-term)
- Grafana query cache
- 任何 downstream 消費 OTLP stream 的地方

**這是高 blast-radius 錯誤**(一次配錯永久 leak),而且很容易犯(copy-paste config 時)。10 行 code 換「refuse to boot on match」,值得。

**檢測範圍**:

| Pattern | 抓什麼 |
|---|---|
| `sk-` prefix | OpenAI / Anthropic legacy token |
| `ghp_` prefix | GitHub PAT(classic) |
| `github_pat_` prefix | GitHub PAT(fine-grained) |
| `AIza` prefix | Google API key |
| `xoxb-` / `xoxp-` / `xoxa-` / `xoxs-` prefix | Slack tokens |
| `AKIA` prefix | AWS access key id |
| base64 shape(≥32 chars, `[A-Za-z0-9+/=_-]` only)| 其他 opaque token |

**設計取捨**:

- **不做完整 secret scanner**(如 `detect-secrets` / `trufflehog`)—— 那是重 dep + false positive + 需持續更新的東西
- 只抓「top-5 embarrassing mistakes」,對 homelab 單人場景已足夠
- 想更嚴的 operator 該在 pre-commit / CI 層裝專業 scanner,不是 runtime

**log 訊息設計**:

- ✅ 印 key 名字(讓 operator 知道改哪一條)
- ✅ 印 matched pattern(讓 operator 知道為什麼被抓)
- ❌ **永遠不印 value 本身**(diagnostic line 若印 value,自己就變 leak channel)

---

## 5. Rate-limited BSP + traces_dropped 觀察機制

### 5.1 為什麼需要

`BatchSpanProcessor` 有兩種丟 span 的情境:

1. **queue 滿**:進 span 進得比 exporter 出 span 快,SDK 內部丟新進的 span
2. **export 失敗**:collector 掛了、網路不通、gRPC error

SDK 自己會 log WARNING,但:
- 沒有 metric 讓 operator 知道「掉了幾個 / 掉了多久」
- WARNING 頻率不受控 —— collector 掛半天可能噴幾百條 identical warning 淹掉正常 log

### 5.2 設計:`RateLimitedBSP` 頂層 class(D24)

**核心決定**:繼承 `BatchSpanProcessor` 不是 wrap-and-delegate。

**演進過程(三段)**:

1. **第一版是 wrap-and-delegate**:自己寫一個 class 持有 inner BSP,手動 delegate `on_start` / `on_end` / `shutdown` / `force_flush`。**結果炸了** —— SDK 新版加了 `_on_ending` 方法(在 `span.end()` 內部呼叫),我沒 delegate,`AttributeError` 讓每個 request 死。
2. **第二版改成 factory + class-in-function**:`_make_rate_limited_bsp(exporter)` 內部動態建一個繼承 `BatchSpanProcessor` 的 class。解決了 `_on_ending` 問題(subclass 自動繼承 SDK 新加的每個 hook),但 stack trace 變成 `..._make_rate_limited_bsp.<locals>._RateLimitedBSP`、tests 無法 `isinstance` 檢查、class-in-function 本身是 Python code smell。
3. **第三版(現況,D24)**:`RateLimitedBSP` 是**頂層 class**,住在 `observability/_rate_limited_bsp.py`,`configure_tracing()` 在 enabled 分支才 lazy-import 這個 module。同時保留 lazy import 契約 + 頂層 class 的所有好處。

**Lazy import 契約如何維持**:`_rate_limited_bsp.py` 頂端有 `from opentelemetry.sdk.trace.export import BatchSpanProcessor` —— **這行本身**就是 SDK import 的觸發點。但因為這個 module 只在 `configure_tracing()` 的 `otel_enabled=True` 分支被 import,`OTEL_ENABLED=0` 時**整個 module 沒被 load**,SDK 也不會被 pull in。

驗證:`sys.modules` 檢查(在 `docs/tracing-architecture.md §5.2` 頂端有 test script 可跑)顯示 `OTEL_ENABLED=0` 下 `zashiki_warasi.observability._rate_limited_bsp` 跟 `opentelemetry.sdk.trace.export` 都不在 `sys.modules`。

**現況 code 骨架**:

```python
# src/zashiki_warasi/observability/_rate_limited_bsp.py
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from zashiki_warasi.observability import traces_dropped_total

_WINDOW_SECONDS = 60.0

class RateLimitedBSP(BatchSpanProcessor):
    def __init__(self, exporter, *, log=None):
        super().__init__(exporter)
        self._exporter_ref = exporter
        self._log = log or logger
        self._last_warned = {}
        self._install_export_wrapper()

    def on_end(self, span):
        # 檢查 queue 是否已滿(意味 SDK 內部丟了)
        queue_size_before = len(getattr(self, "queue", []))
        super().on_end(span)
        max_size = getattr(self, "max_queue_size", None)
        if max_size is not None and queue_size_before >= max_size:
            traces_dropped_total.labels(reason="queue_full").inc()
            self._warn_rate_limited("queue_full")

    def shutdown(self):
        remaining = len(getattr(self, "queue", []))
        if remaining > 0:
            traces_dropped_total.labels(reason="shutdown").inc(remaining)
        super().shutdown()

    def _install_export_wrapper(self): ...
    def _warn_rate_limited(self, reason): ...


# src/zashiki_warasi/observability/tracing.py
def configure_tracing(settings, ...):
    ...
    if not settings.otel_enabled:
        return
    ...
    # 只有 enabled 才 import 這個 module — 連帶 SDK 也才 import
    from zashiki_warasi.observability._rate_limited_bsp import RateLimitedBSP
    processor = RateLimitedBSP(exporter, log=log)
    provider.add_span_processor(processor)
```

**成本 vs 收益**:refactor 花了 ~20 分鐘;換到 clean stack trace、`isinstance()` 可用、頂層 class 這個公認慣例。前一版(factory)也可以正常運作,只是每個未來 reader 都會皺眉一下。

### 5.3 counter 契約

| Metric | Label | 意義 |
|---|---|---|
| `zashiki_traces_dropped_total` | `reason=queue_full` | queue 滿,新進 span 被丟 |
| `zashiki_traces_dropped_total` | `reason=export_failed` | export 呼叫 raise |
| `zashiki_traces_dropped_total` | `reason=shutdown` | process 關閉時 queue 內未送出的 |

Operator dashboard 上加一個 `rate(zashiki_traces_dropped_total[5m])` panel + alert:非零就代表 trace 資料有損失,通常是 collector 端問題。

### 5.4 Rate-limit window 邏輯

```python
_WINDOW_SECONDS = 60.0

def _warn_rate_limited(self, reason):
    now = time.monotonic()
    prior = self._last_warned.get(reason, 0.0)
    if now - prior >= _WINDOW_SECONDS:
        self._last_warned[reason] = now
        self._log.warning(...)
```

**每個 reason 獨立**:queue_full 跟 export_failed 各自一個 window,不會互相 suppress。

**Time source 用 `time.monotonic()`**:對 wall-clock 校時免疫,測試也不用 mock 時間。

---

## 6. Testing 策略與踩過的雷

### 6.1 Test 層級

| 層級 | 檔案 | 涵蓋什麼 |
|---|---|---|
| **Unit(純函式)** | `tests/observability/test_tracing.py` | guards、parsers、Resource builder、BSP subclass 機制 |
| **Integration(with HTTP)** | `tests/observability/test_span_tree.py` | 真實 span 產生 + parent-child + 屬性 |

**35 個 unit tests + 7 個 integration tests = 42 個 tracing-specific tests**。

### 6.2 Integration test 的技術要點:`InMemorySpanExporter`

```python
@pytest.fixture
def in_memory_exporter():
    """安裝真的 TracerProvider + 記憶體 exporter,測試後 restore"""
    prior_provider = trace._TRACER_PROVIDER
    prior_flag = trace._TRACER_PROVIDER_SET_ONCE._done
    
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))  # ← 同步 export
    
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._TRACER_PROVIDER = None
    trace.set_tracer_provider(provider)
    
    try:
        yield exporter
    finally:
        provider.shutdown()
        trace._TRACER_PROVIDER_SET_ONCE._done = prior_flag
        trace._TRACER_PROVIDER = prior_provider
```

**兩個技術決策**:

1. **`SimpleSpanProcessor` 不 `BatchSpanProcessor`**:整合測試不希望等背景 thread flush,同步 export 直接落到記憶體 exporter,assertion 立刻有資料
2. **snapshot + restore 私有 `trace._TRACER_PROVIDER`**:OTel SDK 有「set once」保護,同一個 process 呼叫兩次 `set_tracer_provider` 會 warn 且不生效。整合測試需要跨 test 重複裝 provider,只能碰私有(這是 OTel 官方 test suite 也用的手法)

**assertion 範例**:

```python
def test_success_emits_tick_once_span_with_attributes(in_memory_exporter, ...):
    client.post("/poll")
    
    tick_span = next(
        (s for s in in_memory_exporter.get_finished_spans() 
         if s.name == "zashiki.tick_once"),
        None
    )
    assert tick_span is not None
    attrs = dict(tick_span.attributes or {})
    assert attrs["zashiki.messages_processed"] == 3
    assert attrs["zashiki.cursor_before"] == 100
    ...
```

### 6.3 踩過的雷 & 怎麼避開

#### 雷 1:`conftest` 用 `monkeypatch` 打亂 fixture 依賴圖

**症狀**:加 `tests/conftest.py` autouse fixture 用 `monkeypatch.setenv("OTEL_ENABLED", "0")` 之後,`tests/core/test_db.py` 的 4 個 test 突然 error(`AttributeError: 'function' object has no attribute 'cache_clear'`)。

**根因**:pytest fixture 依賴圖。當兩個 autouse fixture 都 request `monkeypatch`,teardown 順序會被影響。原本 test 內部的 `monkeypatch.setattr(db, "get_engine", lambda: engine)` 的 undo 是在 `_clear_lru_caches` teardown 之後,所以 teardown 呼叫 `db.get_engine.cache_clear()` 時 get_engine 還是 lru_cached。加了 conftest fixture 之後順序翻轉,teardown 拿到 lambda,`.cache_clear()` 就 AttributeError。

**修法**:conftest fixture **不用** `monkeypatch`,改用直接 `os.environ` + try/finally:

```python
@pytest.fixture(autouse=True)
def _default_otel_disabled():
    prior = os.environ.get("OTEL_ENABLED")
    os.environ["OTEL_ENABLED"] = "0"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("OTEL_ENABLED", None)
        else:
            os.environ["OTEL_ENABLED"] = prior
```

不 request `monkeypatch` fixture,不進依賴圖,不影響其他 fixture 順序。

**教訓**:autouse conftest fixture 需要極度保守 —— 只要碰到 pytest built-in fixture 就有機會打亂依賴圖。用 stdlib(os.environ + try/finally)最安全。

#### 雷 2:OTel-enabled test 污染其他 test

**症狀**:單獨跑 `test_request_id.py` 都過,但跟其他 test 一起跑就 fail,fail 訊息是 `AttributeError: '_RateLimitedBatchSpanProcessor' object has no attribute '_on_ending'`。

**根因**:`test_enabled_path_installs_provider` test 呼叫 `configure_tracing()` 真的 install 了 TracerProvider,test 結束 provider 沒 reset(SDK 「set once」保護),後面每個 test 的 `create_app()` 裡的 `FastAPIInstrumentor.instrument_app(app)` 都拿到那個真 provider,middleware 開真 span,`span.end()` 呼叫 SpanProcessor.\_on_ending 時撞到 wrap-and-delegate 版本沒 delegate 的 bug。

**修法**:兩層都做

1. **改 BSP 為 subclass**(見 §5.2)—— 徹底解決 delegate 遺漏問題
2. **加 `_reset_tracer_provider` fixture** 給 OTel-enabled test 用,snapshot + restore 私有 `trace._TRACER_PROVIDER`

**教訓**:全域 singleton 是 test 隔離的天敵。任何 test 動全域 state,都要有 fixture snapshot/restore。

#### 雷 3:Edit 誤把 helper 塞進 async function 中間

**症狀**:實作 `_set_tick_span_attributes` helper 時,Edit 誤把 helper 定義插進 `async def poll` body 中間,結果 `await asyncio.to_thread(_do_tick)` 那行變成落在 async function 外,`SyntaxError: 'await' outside async function` 讓全部 test error(import 期就爛)。

**教訓**:大範圍 Edit 之後**跑 full suite** 而不是只跑改動 file 的 test —— 這種 syntax error 只要 test module import 該 file 就會爆,只跑局部 test 未必抓得到。

### 6.4 已知的測試盲點

- **實際 OTLP export**:test 用 InMemoryExporter,沒有測「真的送出去到 collector 有沒有格式對」。信任 OTel SDK 本身的 test,而且我們的 exporter 是 `OTLPSpanExporter` 官方版
- **LangChain / LangGraph 內建 span 真的被 D23 擋掉**:test 是「vacuously true」(整個測試環境沒真的跑 langchain,所以自然沒 langchain span)。真正驗證要在 k3s smoke 那邊,`OTEL_ENABLED=1` 跑一次 real tick,看 Tempo 有沒有 `langchain.*` span 混進來
- **多 replica trace 的 `service.instance.id`**:單 test 環境只有一個 pod,只驗到 `HOSTNAME` fallback 到 uuid 這條;真正兩個 pod 分別產 trace 到同一個 Tempo 分得開,是 k3s smoke 才能驗

---

## 7. 運維手冊

### 7.1 Env vars 完整對照

| Env var | 預設 | 意義 |
|---|---|---|
| `OTEL_ENABLED` | `0` | 主開關。`1` 啟用整個 SDK |
| `OTEL_SERVICE_NAME` | `zashiki-warasi` | resource `service.name` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC endpoint |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | 傳遞給 SDK |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio` | sampler 名字 |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | sampler ratio(0.0~1.0)|
| `OTEL_RESOURCE_ATTRIBUTES` | (空) | `k=v,k=v` 額外 resource 屬性 |
| `WEB_CONCURRENCY` | (空) | uvicorn worker 數。**> 1 會 fail-fast** |
| `HOSTNAME` | (docker/k8s 自動注入) | `service.instance.id` 來源 |
| `LANGCHAIN_TRACING_V2` | `false`(強制)| D23 |
| `LANGSMITH_TRACING` | `false`(強制)| D23 |

### 7.2 Enable tracing:三步驟

1. **部署 OTel collector**(compose profile 或 k3s 自架,詳見 Group 6/7 產出的 compose 檔跟 helm chart)
2. **設 env**:
   ```
   OTEL_ENABLED=1
   OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
   ```
3. **重啟 app**(不用重 build image)

### 7.3 Debug 場景 walkthrough

#### 場景 A:「上一個 tick 為什麼慢?」

1. 打開 Grafana → Explore → Tempo
2. 搜 `{service.name="zashiki-warasi"}`,選最近的 `POST /poll` trace
3. 展開 span tree
4. 看哪個 child span 的 duration bar 最長 → 那就是瓶頸
5. 常見答案:LLM 呼叫、Gmail API rate limit、DB pool 拿不到 conn

#### 場景 B:「message xyz 處理發生錯誤,詳情?」

1. Tempo Search → filter `zashiki.message_id=xyz`
2. 找到那次處理的 span tree(可能跨多個 `/poll` invocation,因為 tick_once 是 idempotent)
3. 紅色 span = 出事的 node,exception 在 span events 裡

#### 場景 C:「trace 該有的資料 Tempo 沒看到」

1. 檢查 `/metrics` 的 `zashiki_traces_dropped_total{reason=...}` counter
2. `queue_full` → 進 span 太快,考慮調 sampler 降到 `0.1`
3. `export_failed` → collector 端問題,看 app log 有沒有 rate-limited WARNING 訊息點出 endpoint
4. `shutdown` → process 剛重啟,in-flight span 有損失,不用怕
5. 如果三個 counter 都 0 但 Tempo 沒 trace → 檢查 collector 那邊有沒有收到(可能是 collector → Tempo 那段問題,不在 app 側)

#### 場景 D:「操作誤設 secret 到 `OTEL_RESOURCE_ATTRIBUTES`」

App 直接 exit 不會 boot,log CRITICAL 訊息會指出 key 名字。修 env,重啟即可 —— **secret 沒有 leak**(secret guard 在 send 任何 span 之前擋住)。

#### 場景 E:「operator 想關 tracing 除錯」

`OTEL_ENABLED=0` 重啟。SDK 立刻回到未初始化狀態,不再 export、不再耗 memory。metrics 還會照常 emit(這兩者獨立)。

---

## 8. 已知限制 / defer 事項

### 8.1 這次刻意沒做的

| 項目 | 原因 |
|---|---|
| **Log↔Trace 連接**(log 帶 trace_id/span_id)| Group 5 才做,需要動 log formatter |
| **JSON log format** | 同上,Group 5 |
| **route-specific sampler**(`/healthz` 一律 off)| 過度優化,現在流量低,`/healthz` trace 也不吵 |
| **Exemplar**(metric spike 一鍵跳 trace)| Backlog,需要 collector 端配合 |
| **LLM prompt/completion span attribute**(記完整 prompt 內容)| PII 顧慮 + payload size,暫不做 |
| **Multi-worker 支援**(`PROMETHEUS_MULTIPROC_DIR`)| 現在不需要,`replicaCount` 已夠 |

### 8.2 依 SDK 版本可能變的部分

- `BatchSpanProcessor._on_ending` 這種新方法未來 SDK 可能再加更多 hook,我們的 subclass 自動繼承,理論上不受影響
- `trace._TRACER_PROVIDER_SET_ONCE` 是私有 API,若 OTel SDK 未來重寫這一段,我們的 test fixture 要跟著改
- `HOSTNAME` env 在某些 exotic 容器 runtime 可能不注入,我們有 uuid fallback,但這值就跟 `kubectl get pods` 對不起來

### 8.3 尚未 e2e 驗證的

- 真的送到 collector 的格式對不對(靠 OTel SDK 本身 test)
- LangChain / LangGraph 內建 span 真的被 D23 擋住(需要 k3s smoke)
- 多 replica trace 從同一個 Tempo 分得開(需要 k3s replicaCount=2 smoke)

以上都在 tasks.md 的 9.6 / 9.7 k3s smoke 中會實測。

---

## 9. 相關文件 & 決策紀錄

### 9.1 OpenSpec change 文件

- `openspec/changes/add-observability-stack/proposal.md` —— 為什麼要做這個 change
- `openspec/changes/add-observability-stack/design.md` —— 完整 23 條設計決策(D1~D23),這份 KT 文件是 design.md 的實作對照 + 運維視角
- `openspec/changes/add-observability-stack/specs/observability/spec.md` —— OTel tracing 契約(requirement 級)

### 9.2 相關 lesson learned 文件

- `docs/lessons/2026-08-11-basehttp-middleware-vs-otel-tracing.md` —— 為什麼必須 pure ASGI middleware(D17 的完整背景)
- `docs/lessons/2026-08-12-prometheus-multi-worker.md` —— 為什麼 `WEB_CONCURRENCY>1` 要 fail-fast(D18 的完整背景)

### 9.3 OpenTelemetry 官方 references

- [OpenTelemetry Python doc](https://opentelemetry.io/docs/languages/python/)
- [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) —— `gen_ai.*` 屬性命名依此
- [OTel Resource semantic conventions](https://opentelemetry.io/docs/specs/semconv/resource/) —— `service.*` 屬性命名依此
- [OTel Python SDK multiprocessing 章節](https://opentelemetry.io/docs/languages/python/instrumentation/#creating-spans) —— 為什麼 `trace._TRACER_PROVIDER` 有 set-once 保護
- [prometheus_client multiprocess mode](https://prometheus.github.io/client_python/multiprocess/) —— 我們**故意不用**的另一條路(D18 rejected alternative)

### 9.4 這份文件的維護

- **什麼時候該更新這份文件**:
  - Span 契約有變(加新 span、屬性契約變、tree 拓撲變)
  - `configure_tracing` 內部行為變(新加 guard、換 sampler default、換 processor)
  - test 隔離策略變(新的 fixture、新的私有 API 用法)
  - 踩到新的雷值得記下來
- **不需要更新的變化**:call site 加減(那是 spec 的事)、metric 契約變(那是 `docs/observability.md` 的事)、Grafana dashboard 變(那是 dashboard JSON 本身)
- **原則**:這份文件是**架構級**的 KT,不是 API reference。細節放 spec + docstring,這裡放「為什麼」跟「怎麼想通」。

---

## 附錄:一頁式 cheatsheet

**啟用 tracing**:`OTEL_ENABLED=1 OTEL_EXPORTER_OTLP_ENDPOINT=<collector>`

**關閉**:`OTEL_ENABLED=0`(default)

**Span 命名前綴**:`zashiki.` = 我們手埋 · `HTTP` = httpx auto · SQL family = psycopg auto · `POST /poll` = FastAPI SERVER auto

**新加 span 的三種方式**:
- `with node_trace(log, "name"):` —— LangGraph node 場合(自動附 message_id / request_id)
- `with zashiki_span("name", attributes={...}) as span:` —— 任意業務範圍
- `set_gen_ai_attributes(span, system=..., model=...)` —— LLM 呼叫加 GenAI 屬性

**Fail-fast 觸發**:
- `WEB_CONCURRENCY=4` → 開機崩(D18)
- `OTEL_RESOURCE_ATTRIBUTES=x=sk-abc123` → 開機崩(D22)
- `OTEL_TRACES_SAMPLER_ARG=1.5` → 開機崩(pydantic validator)

**觀察 trace 損失**:`rate(zashiki_traces_dropped_total[5m])` > 0 有問題

**Debug 起點**:`/healthz` → app 活嗎 · `/metrics` → 業務健康嗎 · Tempo → 個別 request 內部怎樣
