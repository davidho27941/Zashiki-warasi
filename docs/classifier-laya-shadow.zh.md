# Laya 影子分類器(v1.6.0)

> English version: [`classifier-laya-shadow.md`](classifier-laya-shadow.md).
>
> **這份文件是**:影子分類器的維運指南——怎麼開、怎麼讀 agreement
> 資料、解讀時要注意哪些坑。
>
> **這份文件不是**:設計理由(見
> `openspec/changes/add-laya-shadow-classifier/design.md` D1–D17,
> 含 D10 eval gate 結果);正式 contract(sync 後見
> `openspec/specs/classifier-shadow/spec.md`)。

## 這是什麼

一個 fine-tune 過的 [laya](https://pypi.org/project/laya/) encoder
(`laya-zashiki-email-v1`,以 ~4.8k 筆人工標註信件做 RLCD 微調)
以**影子模式**跟著 LLM 分類器一起跑:每封成功分析的信同時送到
CPU-only 的 `laya-serve` pod,兩邊的預測並排記錄下來。

**LLM 仍然是唯一的決策者。** 影子輸出不改變任何行為——不影響
Telegram、不影響 `email_analyses`、不影響 graph 走向。產物只有:

- 每 `(message_id, laya_model_ver)` 一筆 `laya_shadow_predictions`
  row——自包含的 `(input_text, prediction)` 樣本,未來可直接當
  訓練資料;
- 三個 Prometheus metric family(見下)。

Phase 2(issue #4)會在累積 ≥2 週實況 agreement 資料後,決定
laya 是否以 hybrid 形式接手部分流量。

## 啟用

兩個部件,都走 helm:

```bash
# 1. 分類器後端(獨立 chart,CPU-only)
helm install laya -n zashiki deploy/helm/laya-classifier

# 2. zashiki 開影子(values.<cluster>.yaml 的 env 區塊)
#    LAYA_SHADOW_ENABLED: "1"
#    LAYA_BASE_URL: http://laya-laya-classifier.zashiki.svc:8000
#    LAYA_QUESTIONS_PATH: /etc/zashiki/laya-questions.json
#    LAYA_SEMANTIC_VER: laya-zashiki-v1
#    LAYA_TIMEOUT_SECONDS: "5"
helm upgrade zashiki -n zashiki deploy/helm/zashiki-warasi \
    -f deploy/helm/values.<cluster>.yaml \
    -f deploy/helm/values.<cluster>.secrets.yaml \
    --set image.tag=<sha>
```

Kill switch:`LAYA_SHADOW_ENABLED=0`(預設)或 `LAYA_BASE_URL`
留空——client 會以惰性狀態建構,完全不碰網路、DB、metrics。

開機時 pod 會印一行確認 flag 生效:

```
laya shadow enabled: endpoint=... model_ver=laya-zashiki-v1-e44b310a
```

`-e44b310a` 後綴是 `sha1(questions dict 位元組)[:8]`——questions
ConfigMap 任何修改都會自動轉版 `laya_model_ver`,在
`UNIQUE(message_id, laya_model_ver)` 之下開一條新的比對序列。

### Timeout 為什麼是 5 秒

`laya-serve` 的 `/health` 不做推理,pod Ready 時模型是**冷的**:
重啟後第一個請求 ~2 秒,warm 之後 1.5k 字輸入約 0.7–1.0 秒
(叢集內實測)。程式碼預設 2 秒對穩態夠用,但保證 laya-serve
每次重啟後的第一封信必吃一筆 `timeout`——所以 production 設
`LAYA_TIMEOUT_SECONDS: "5"`。影子呼叫跑在有界背景 pool,timeout
放寬對主流程零成本。

## 讀資料

### Metrics

| Family | Labels | 讀法 |
|---|---|---|
| `zashiki_classifier_shadow_agreement_total` | `llm_category`, `laya_category`, `agreed` | 即時 confusion matrix。agreement rate = `sum(agreed="true") / sum()`。 |
| `zashiki_classifier_shadow_duration_seconds` | `engine` | 影子往返延遲(寬 bucket——CPU 推理)。 |
| `zashiki_classifier_shadow_error_total` | `reason` | `timeout` / `connection_refused` / `http_5xx` / `bad_response` / `dropped_saturated`。 |

### SQL

```sql
-- 目前模型版本的 agreement rate
SELECT laya_model_ver,
       count(*) FILTER (WHERE laya_error IS NULL)             AS scored,
       count(*) FILTER (WHERE llm_category = laya_category)   AS agreed
FROM laya_shadow_predictions GROUP BY 1;

-- 分歧下鑽(真正有價值的 rows)
SELECT llm_category, laya_category, count(*)
FROM laya_shadow_predictions
WHERE laya_error IS NULL AND llm_category <> laya_category
GROUP BY 1, 2 ORDER BY 3 DESC;
```

`laya_alternates` 存了每筆完整的 15 類機率向量——可以隨時對實況
資料重跑 D10 的 hybrid coverage/precision sweep,不用重呼叫模型。

## 解讀 agreement 時的坑

1. **Super-class agreement**:laya 預測合併後的「行事曆事件」
   (calendar_event);LLM 預測細分的「會議邀請」/「講座資訊」。
   agreement metric 把 laya=行事曆事件 vs LLM=任一細分標籤算作
   **agreed**(274 筆稽核顯示細分邊界連人都不穩定——83 筆判定
   翻轉)。直接用 SQL 字串相等會低估 agreement;請用 metric 或
   複製這條規則。
2. **截斷混淆因子**:LLM 看整封信;laya 只看 `subject + body`
   截到 1500 字(`classifier/truncation.py`,快照存在
   `input_text`)。長信上的分歧可能是資訊差,不是模型差。
3. **LangGraph resume 缺口**:分析失敗、事後 resume 的信**沒有**
   影子 row——hook 掛在 `_analyze` 的成功尾端。這是預期行為,
   不要去追少掉的 rows。
4. **Counter 重啟低估**:Grafana 的 agreement 面板用
   `increase(counter[range])`;pod 重啟後 Prometheus 看不到各序列
   「出生自帶」的那筆值,所以部署頻繁的日子 dashboard 會**高估**
   agreement(掉的多半是稀有的分歧序列)。穩態下沒問題;要做決策
   時一律用 `laya_shadow_predictions` 算(上面的 SQL)——DB 才是
   ground truth。
5. **定義同步義務**:
   `deploy/helm/zashiki-warasi/configs/laya-questions.json` 裡的
   英文 criteria 對映 `ANALYZE_SYSTEM_PROMPT`。prompt 的類別定義
   一改,dict 必須跟著改(反之亦然)——
   `tests/classifier/test_question.py` 的 byte-equality 測試釘住
   dict↔訓練腳本的一致性,但 prompt↔dict 的同步是人的責任。

## Phase 2 決策樹(啟用後至少 2 週才 review)

已於 **2026-09-26** 在 lab 叢集啟用。

| 實況 agreement(含 super-class 規則) | 動作 |
|---|---|
| ≥ 90% | Hybrid:`max(prob) ≥ 0.80` 時 laya 作答(held-out 上 96.2% precision @ 66% coverage——先用實況資料重新驗證這個閾值),其餘交 LLM。 |
| 85–90% | 用累積的實況樣本再 fine-tune 一輪,重新量測。 |
| < 85% | 先調查分歧群集再花任何力氣——confusion matrix 會說問題在哪。 |
