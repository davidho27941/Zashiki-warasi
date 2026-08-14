# 觀察性工具選型:OTel/Tempo vs Langfuse(以及其他)

> **這份文件是什麼**:v1.1 `add-observability-stack` change 選了
> OTel + Tempo + Prometheus 這條「通用型」路徑。這份文件記錄
> **選型當時漏掉的候選(Langfuse)**、以及**為什麼選型結果最後仍
> 然合理**,並抽出一套「未來再遇到類似 domain-specific vs
> general-purpose 工具比較」的評估框架。
>
> **寫這份文件的原因**:2026-08 conversation 期間 operator 提問
> 「Langfuse 也有類似功能為何當初沒考慮」,揭露 proposal 階段
> 的一個評估盲點 —— proposal 只框在 operator 已知的工具名字
> (OTel/Prom/Grafana)裡,沒主動掃 domain-specific 選項。這是
> **過程 bug**,值得留下來提醒未來的自己(跟未來的 reviewer)。
>
> **這份文件的定位**:reference / decision rationale。**不是**
> tutorial(如何用 Langfuse 或 OTel 是各自官方 doc 的事),**不是**
> lesson learned(那是 `docs/lessons/` 存個別工程反省)。這裡的
> 核心是「兩類工具本質差異」+「什麼場景該怎麼選」。

## 目錄

1. [背景與選型結果](#1-背景與選型結果)
2. [兩類工具的本質差異](#2-兩類工具的本質差異)
3. [詳細對照表](#3-詳細對照表)
4. [Langfuse ↔ Grafana 整合為什麼會斷](#4-langfuse--grafana-整合為什麼會斷)
5. [Zashiki-warasi 為何最後仍選 OTel/Tempo](#5-zashiki-warasi-為何最後仍選-oteltempo)
6. [什麼場景該優先考慮 Langfuse](#6-什麼場景該優先考慮-langfuse)
7. [OTel + Langfuse 共存的可行方案](#7-otel--langfuse-共存的可行方案)
8. [Meta:未來評估 observability 工具的思考框架](#8-meta未來評估-observability-工具的思考框架)
9. [References](#9-references)

---

## 1. 背景與選型結果

**v1.1 `add-observability-stack` change 最終選擇**:

- Metrics:`prometheus-client` + `/metrics` endpoint,scraper 是 kube-prometheus-stack 或 compose Prometheus
- Traces:OpenTelemetry SDK + OTLP/gRPC → OTel Collector → Grafana Tempo
- Log-trace correlation(Group 5 才做):OTel context 帶 `trace_id`/`span_id` 到 log

**當時 proposal 只討論到的工具**:OTel SDK、Prometheus、Grafana、Tempo、Loki(defer)、Alloy(backlog)。

**當時**沒**進入討論的類別**:LLM 專屬觀察平台(Langfuse、LangSmith、Helicone、Arize、Braintrust ...)。

**這一漏之所以重要**:Zashiki-warasi 的核心就是 LangGraph email agent,一個 tick 有 2-4 個 LLM 呼叫。LLM 專屬觀察平台**表面上**是這類 workload 的首選,proposal 完全不提就直接跳過,是評估上的失分。

## 2. 兩類工具的本質差異

觀察性工具在 2026 大致分兩類:

### 2.1 General-purpose observability(通用型)

**代表**:OpenTelemetry SDK + 任意 backend(Grafana Tempo / Jaeger / Honeycomb / New Relic / Datadog / SigNoz ...)

**設計哲學**:「所有應用都有 span,LLM 只是其中一種 client call」

**特徵**:
- CNCF 標準,vendor-neutral
- 一個 SDK / 一個資料 pipeline / 一個 UI 覆蓋整個 stack(HTTP / DB / cache / queue / LLM / anything)
- LLM 不是特殊公民,靠**約定俗成的 semantic conventions**(如 `gen_ai.*` 屬性)標示
- UI 的重點是「span tree + parent-child 關係」,LLM 的 prompt/response/tokens **要靠 dashboard 自己設**才顯眼

### 2.2 Domain-specific observability(LLM 專屬)

**代表**:Langfuse、LangSmith、Helicone、Arize、Braintrust、Uptrace GenAI mode

**設計哲學**:「LLM 應用有獨特的 debug / eval / cost / prompt-management 需求,值得專屬工具」

**特徵**:
- LLM 是 first-class primitive,UI 就是為 prompt/response/token/cost 設計
- 額外功能:prompt versioning、evaluation harness、A/B testing、dataset regression、LLM-as-judge、session replay
- 廠商中立性差:綁自家 SDK 或自家 backend
- 非 LLM 部分(DB、HTTP、queue)**支援有限**或**根本不覆蓋**
- 通常有自己完整 web UI,不融入既有 monitoring stack

### 2.3 為什麼這是「兩類」不是「兩個工具」

**通用型的 backend 是可替換的**:OTel SDK 是標準,你可以 export 到 Tempo、Jaeger、Honeycomb,換 backend 不改 app code。

**Domain-specific 通常是完整平台**:Langfuse 有 SDK、有儲存(Postgres + ClickHouse)、有 web UI、有 evaluation runtime,一組全包。要離開它成本高。

這個差異是很多討論繞不開的核心。

## 3. 詳細對照表

| 維度 | OTel + Tempo + Grafana | Langfuse |
|---|---|---|
| **架構定位** | 通用型 observability standard + backend | LLM 專屬完整平台 |
| **標準** | CNCF OpenTelemetry(vendor-neutral) | Langfuse 自家 API,同時支援 OTLP 接收 |
| **典型部署形態** | 4 個 container(collector + prom + tempo + grafana) | 6 個 container(含 ClickHouse + Postgres + Redis + web + worker) |
| **License** | Apache 2.0(SDK)+ AGPL(Tempo)/ Apache(Prometheus)/ AGPL(Grafana) | MIT(core),雲端服務另計 |
| **LLM call tracing UI** | 需 dashboard 自訂,GenAI semconv 屬性要自己讀 | 一等公民,直接展開 prompt/response/tokens/cost |
| **Non-LLM tracing**(HTTP / DB / queue / node-to-node) | 一等公民,所有 span 一視同仁 | 支援但邊緣,UI 主打 LLM chain |
| **Metric collection**(業務 counter / gauge / histogram) | Prometheus 是主場,原生無縫 | 有限,主要是 LLM-derived metric |
| **Prompt versioning** | 無 | 內建,含 A/B、rollback |
| **LLM cost tracking**(依 model / user / session)| 無 | 一等公民 |
| **LLM-as-judge evaluations** | 無 | 內建,含 hallucination / toxicity / relevance templates |
| **Dataset regression testing** | 無 | 內建,可 CI 跑 |
| **Session replay**(對話式 agent)| 無 | 內建 |
| **與 kube-prometheus-stack 整合** | Prom / Grafana 直接吃,加 Tempo 只是新增一個 datasource | 沒直接關係,是另一個獨立 web app |
| **Grafana 內能否原生查 Langfuse 資料** | N/A(Tempo 本身是 Grafana datasource) | **不能**(見 §4) |
| **`/metrics` Prometheus endpoint** | 有(app 自己 expose) | **沒有**(從 2024 一直是 open issue,2026 尚未 ship) |
| **可否 export 到其他 backend** | ✓(換 collector export target 即可) | Langfuse 通常是 sink,export 出去有限;可 OTLP forward 到其他 backend |
| **UI 數量** | 一個(Grafana,cover metrics + trace + log)| 兩個(Grafana + Langfuse UI)|
| **Login 數量** | 一組(Grafana) | 至少兩組(Grafana + Langfuse) |
| **對操作者的認知負擔** | 一套 query language(PromQL / TraceQL)+ 一組 UI 慣例 | 兩套查詢方式 + 兩組 UI 慣例 |

## 4. Langfuse ↔ Grafana 整合為什麼會斷

這是 operator 提問中最關鍵的觀察 —— 也是這份文件寫的直接動機。

### 4.1 Langfuse **不是** Grafana 的原生 datasource

Grafana 的 datasource 選單有 Prometheus / Loki / Tempo / Jaeger / ClickHouse / PostgreSQL 等等 —— **沒有 Langfuse**。這意味著:

- Grafana UI 內**無法**打 Langfuse 特有的查詢
- Grafana panel **無法**直接顯示 Langfuse 的 trace tree
- Grafana Explore **無法**跳到 Langfuse 的 trace UI

### 4.2 常見的「整合」方式都是 workaround

**方式 A:讀 Langfuse 內部 Postgres**

- 把 Langfuse 的 Postgres 當作 Grafana 的 PostgreSQL datasource(read-only)
- SQL query `traces`、`observations`、`scores` 等 internal table
- **問題**:那是 Langfuse **內部 schema,不是 public API**。Langfuse 升級可能改 schema,你的 dashboard query 就爛。Postgres 對 time-series query 也不 optimize,dashboard 慢

**方式 B:Grafana Data Links 跳到 Langfuse UI**

```
URL template: https://langfuse.myhost/traces?from=${__from}&to=${__to}&status=error
```

- Grafana panel 顯示 latency,操作者點 data point,**新開分頁**到 Langfuse UI,帶時間範圍
- **不是整合,是超連結**。體驗上是「兩個 UI 中間開新分頁 + 再登入一次」

**方式 C:社群自寫 Prometheus scrape wrapper**

- Langfuse 沒有 `/metrics` endpoint(2024 至今 open issue [#2508](https://github.com/orgs/langfuse/discussions/2508))
- 有人自己寫 wrapper 打 Langfuse API 轉 Prometheus format
- 個別解法,官方不 endorse,升級不保證相容

### 4.3 相比之下 Tempo 的原生程度

- Tempo **由 Grafana Labs 開發**,設計就是 Grafana ecosystem 一員
- Grafana 內 add Tempo datasource 是一等公民選項
- Grafana Explore UI 內 Tempo trace 展示是原生格式
- Prometheus exemplar 可以直接 linked 到 Tempo trace(**metric spike → 一鍵跳 trace**)
- 未來 Loki(log)↔ Tempo(trace)也是 derived field 一鍵跳
- 一個 Grafana instance、一個 login、metric / trace / log 都在同一個 UI 內

### 4.4 為什麼這個差異重要

**在事故發生時,operator 的注意力有限**。理想是:「Alert 響 → Grafana dashboard 看到異常 → 點一下跳到相關 trace → 展開看細節」全程一個 tab、一個 login。

Langfuse 的整合模式強迫 operator 開兩個 tab、切換兩個心智模型。**平時 debug 忍得住,事故凌晨 3 點忍不住**。

## 5. Zashiki-warasi 為何最後仍選 OTel/Tempo

即使 Langfuse 存在盲點,重新評估後**選 OTel/Tempo 仍然合理**,以下四個具體原因:

### 5.1 LLM 只是 pipeline 的一環,不是主體

一個 `/poll` 產生 15-30 個 span,其中 LLM span 只有 **2-4 個**。剩下的是:

- Gmail API 呼叫(get_profile / list_history / get_message,每次一個 span)
- LangGraph node 切換(analyze / expense_extract / expense_persist / notify)
- Postgres 讀寫(checkpointer / advisory lock / oauth_flow_store)
- Telegram 通知
- Redis / cache(現在沒有,未來可能有)

Langfuse 專攻的那 2-4 個 span 是重點沒錯,但如果整個 debug 時 **80-90% 的 span 是「有記錄但 UI 不方便看」**,反而不划算。

### 5.2 Operator 已有 kube-prometheus-stack

Grafana + Prometheus 在 k3s 已存在且熟悉。加 Tempo 是**同個 Grafana 多加一個 datasource**;加 Langfuse 是**另開一整個 web app + 另一組 login + 另一組 dashboard 路徑**,還要處理它的 Postgres + ClickHouse + Redis 依賴。

### 5.3 Prompt versioning / evaluations 對這個 project 邊際效益低

- 單人使用,prompt 已經穩定,不常改
- 用 local llamacpp,沒 vendor cost 需要細追
- 沒有多 model A/B 比對的需求
- Evaluation 目前靠人工看 log + Telegram 訊息,量小夠用

Langfuse 的殺手級功能對我們**確實有價值但不緊急**,現在花心力導入不如先解決基礎觀察性缺口。

### 5.4 Vendor 中立性

我們的 tracing 契約是**標準 OTel + GenAI semconv**。這意味著未來的選擇空間最大:

- 想試 Jaeger?換 collector export 一行
- 想試 Honeycomb?同上
- 想試 SigNoz?同上
- **想加 Langfuse 當 additional sink?也可以**(見 §7)

反之,若一開始選 Langfuse SDK 綁死,要換 backend 就要改十幾個 call site,`with zashiki_span(...)` 這種抽象要重寫。

## 6. 什麼場景該優先考慮 Langfuse

以下 profile 符合任一項,值得優先評估 Langfuse:

- **產品是 chatbot / AI agent 為主**,LLM 呼叫佔整體 latency 80%+
- **有多 model / 多 provider 需要成本比對**(OpenAI 跟 Anthropic 跟 Claude 各用一部分)
- **有 prompt engineering 團隊**,需要版本化管理跟 A/B testing
- **需要 evaluation harness**:hallucination 檢測、toxicity 過濾、regression test
- **需要 session replay**:多輪對話要重播每次交互
- **有 LLM cost budget 需要每日 tracking**
- **team 有專職 LLM ops / MLOps 角色**願意維護另一套系統

**Zashiki-warasi 剛好 profile 都不符** —— 是個人 homelab,LLM 是 pipeline 一段,無 team、無 cost pressure、無多 model 需求。

## 7. OTel + Langfuse 共存的可行方案

因為我們已經是 OTel 標準,**未來想加 Langfuse 幾乎零 app 端成本**。做法:

### 7.1 架構

```
zashiki-warasi app
  │
  │ OTLP/gRPC
  ▼
OTel Collector
  ├─── OTLP export → Grafana Tempo(現有)
  └─── OTLP export → Langfuse(新加)
```

OTel Collector 支援 multi-exporter fan-out —— 同一批 span 可以同時往多個 backend 送。

### 7.2 需要做的事

1. **部署 Langfuse**(docker-compose 或 k3s helm),準備 Postgres + ClickHouse + Redis
2. **OTel Collector config 加一個 exporter**:
   ```yaml
   exporters:
     otlp/tempo:
       endpoint: tempo:4317
     otlp/langfuse:
       endpoint: langfuse-otel-endpoint:443
       headers:
         authorization: "Bearer <langfuse-project-key>"
   
   service:
     pipelines:
       traces:
         exporters: [otlp/tempo, otlp/langfuse]
   ```
3. **App 端**:一行都不用改。GenAI semconv 屬性(`gen_ai.system` / `gen_ai.request.model` / `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`)Langfuse 直接看得懂 —— 我們在 Group 4b 已經 spec + 實作了

### 7.3 這個共存方案的取捨

**優點**:
- App code 零改動
- 可以同時保留 Grafana 全景 UI 跟 Langfuse LLM-specific UI
- 兩邊各自有各自的 alert / dashboard

**代價**:
- 多一套系統要維護(Langfuse 本身)
- Trace 資料儲存 2 份,storage 成本近乎 2x
- Operator 心智模型多一套(切換 UI 的認知成本)
- 兩邊 data 可能有時序差(collector fan-out 是同時 send 但 backend 處理速度可能不同)

**推薦時機**:當 §6 的 profile 真的變得符合(例如 WebUI epic 上線後每天有幾百次多輪對話),再認真評估。

### 7.4 已加到 backlog

`add-observability-stack` proposal 的 Backlog 應該加一條:

> **Evaluate Langfuse as an additional OTLP sink for LLM-specific analysis** — 現有 OTel Collector 可以 fan-out 到 Tempo + Langfuse,app code 零改動。觸發評估的條件:LLM 呼叫量顯著上升(> 每日千次),或需要 prompt versioning / evaluation harness / multi-model cost tracking 其中一項。

## 8. Meta:未來評估 observability 工具的思考框架

這次的過程 bug 是「proposal 只框在 operator 已知名字內」。以下是**未來寫類似 proposal 時該問自己的問題清單**,避免同樣盲點:

### 8.1 三軸掃描

任何 observability(或其他 tooling)proposal,候選工具應該至少掃三個軸:

1. **General-purpose vs domain-specific**:通用型(OTel/Prom/Grafana、ELK)有沒有考慮?domain-specific(Langfuse、SigNoz、Datadog APM、New Relic)有沒有考慮?
2. **Open source vs commercial**:MIT/Apache 的自架版本、AGPL 的自架版本、SaaS 商業服務,各有沒有評估?
3. **Standard-compliant vs proprietary**:是走 CNCF / OpenTelemetry / Prometheus 標準的,還是廠商自訂 API?標準的長期換手成本低,proprietary 的短期 UX 通常好。

**若某一軸只填了一格,就是評估盲點**。這次 Langfuse 屬於「domain-specific + open-source-selfhostable + proprietary-plus-OTLP」,我們填了「general-purpose + open-source + standard」那一格,另一格空著。

### 8.2 決策 checklist

Proposal 的候選工具比較該回答:

- [ ] 我們的 workload 特徵是什麼?(哪類 span 佔多數 / 什麼查詢模式最頻繁)
- [ ] Operator 現有 stack 是什麼?加這個工具要「插進去」還是「另開一套」?
- [ ] 未來 3-5 年可能需要什麼還沒需要的功能?這個工具擋不擋路?
- [ ] 換掉這個工具的成本?(app 改多少 code、資料遷移、operator 重學)
- [ ] Vendor 中立性?若廠商倒閉或商業條款變差,退場路徑是什麼?
- [ ] Total cost of ownership:license + infra + operator time + 學習曲線

### 8.3 邀 domain expert review 的時機

當 proposal 進入 LLM / ML / 特定 domain 的 tool selection 時,主動 review 那個 domain 的 landscape 一次(google "top X observability platforms 2026" 之類),即使只掃 10 分鐘也能發現盲點。

**這次的教訓**:proposal review 時應該問「這個 workload 有沒有 domain-specific 工具?」而不是接受「operator 提到的 stack 就是答案」。

## 9. References

### 9.1 選型時實際查閱的資料

- [Top LLM Observability and Evaluation Platforms in 2026](https://www.marktechpost.com/2026/08/09/top-llm-observability-and-evaluation-platforms-in-2026-langfuse-langsmith-braintrust-arize-and-more-compared/) — 泛掃 Langfuse / LangSmith / Braintrust / Arize
- [Langfuse for LLM Observability: Tracing & Evals (2026 Guide)](https://qaskills.sh/blog/langfuse-llm-observability-guide-2026) — Langfuse 功能總覽
- [Langfuse: Self-Host LLM Observability for Free — 2026 Guide](https://effloow.com/articles/langfuse-llm-observability-self-host-guide-2026) — 自架部署 walkthrough
- [Mastering LLM Observability: A Hands-On Guide to Langfuse and OpenTelemetry Comparison](https://oleg-dubetcky.medium.com/mastering-llm-observability-a-hands-on-guide-to-langfuse-and-opentelemetry-comparison-33f63ce0a636) — 頭對頭 tech 比較
- [LLM Observability in Production: Comparison of Langfuse, LangSmith, and OpenTelemetry](https://explore.n1n.ai/blog/llm-observability-langfuse-langsmith-opentelemetry-2026-05-17) — 三方比較,含 production trade-off

### 9.2 Grafana ↔ Langfuse 整合的資料(方式 A/B/C 的根據)

- [Langfuse + Grafana: agentic AI monitoring | learnwithparam](https://www.learnwithparam.com/blog/langfuse-grafana-agentic-ai-monitoring) — 方式 A(讀 Postgres)+ 方式 B(data links)的實作範例
- [Enable Langfuse to expose metrics in Prometheus format · Discussion #2508](https://github.com/orgs/langfuse/discussions/2508) — 官方 feature request,顯示 `/metrics` endpoint 一直沒 ship
- [Prometheus/Grafana · Issue #8344 · langfuse/langfuse](https://github.com/langfuse/langfuse/issues/8344) — 相關 issue

### 9.3 相關內部文件

- `openspec/changes/add-observability-stack/proposal.md` — v1.1 這次 change 的原始 proposal
- `openspec/changes/add-observability-stack/design.md` — D8(compose 選 Tempo 不選 Loki)、D14(GenAI semconv)、D23(disable LangChain internal tracing)等相關決策
- `docs/tracing-architecture.md` — Group 4 tracing 子系統的完整架構(這份文件的姐妹篇)

### 9.4 未來若真的加 Langfuse

到時候該起一個 openspec change,建議名字 `add-langfuse-sink`,重點內容:

- OTel Collector config 加 fan-out
- 部署 Langfuse(compose profile 加、helm chart 加)
- Grafana 加 Langfuse data links(方式 B,不追求方式 A)
- Dashboard 加 LLM cost / prompt version 相關 panel
- Backfill 舊 trace(選擇性)
- Docs + operator runbook

---

## 附錄:一頁式決策速查

**要 general-purpose observability(metric + trace + log 一套)?** → OTel + Prom + Tempo + (未來 Loki)

**要 LLM-specific(prompt / eval / cost / session replay)?** → Langfuse(或 LangSmith / Braintrust,依商業條件挑)

**兩者都要?** → OTel 做 base,Collector fan-out 到 Tempo + Langfuse。App code 零改動。

**已有 kube-prometheus-stack 想快速上 tracing?** → 直接加 Tempo,一個 datasource 搞定

**不確定該選哪個?** → 三軸掃描(§8.1),把候選填滿再決定
