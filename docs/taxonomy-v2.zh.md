# 信件分類表 v2

> English version: [`taxonomy-v2.md`](taxonomy-v2.md).
>
> **這份文件是**:分類表切換的維運指南——改了什麼、分類歷史存在哪、
> 切換與回滾步驟。
>
> **這份文件不是**:設計理由(見
> `openspec/changes/adopt-email-taxonomy-v2/design.md` D1–D10),也不是
> 類別定義(正式定義在該 change 的 `specs/email-classification/spec.md`;
> 給 LLM 的版本在 `src/zashiki_warasi/classifier/analyze_prompts.py`)。

## 改了什麼

| | v1(16 類) | v2(14 類) |
|---|---|---|
| 帳務 vertical | 消費支出、帳單通知 | 交易收據、帳單通知 |
| 行事曆 vertical | 會議邀請、講座資訊 | 行事曆事件 |
| 其他 | 只通知 | 只通知 |

v2 把廣告+促銷合併為行銷、會議邀請+講座資訊合併為行事曆事件,新增
內容電子報、職涯、公共警報、投資帳戶通知、服務通知;交易收據維持四要素
(日期、店家、金額、**識別碼**)。獵頭來信若約了面試時間,歸行事曆事件,
不是職涯。

`TAXONOMY_VERSION`(預設 `v1`,可設 `v2`)決定**新**分析用的提示詞、
LLM 輸出格式與路由表。既有的分析一律依它產生時的版本路由。

## 分類存在哪

| 物件 | 角色 |
|---|---|
| `email_analyses.category` + `.taxonomy_version` | LLM 第一次判斷的原始紀錄,只寫一次。不要拿來做分析。 |
| `email_classifications` | 分類歷史,處理時間的 SCD type 2。目前版本 ⇔ `valid_to IS NULL`。每封信只有一筆目前版本、區間不重疊(資料庫強制)。 |
| `taxonomy_mappings` | 只增不改的 v1 → v2 對應表(`mapping_rev`、`is_exact`)。 |
| `email_category_current`(view) | **唯一**讀取入口:目前版本轉成最新分類表,附 `is_exact`。dashboard、訓練資料匯出、臨時 SQL 都用它。 |

writer 保證:只有 `(category, taxonomy_version, source)` 改變才開新版本;
`llm` / `laya` 不會蓋掉 `human`;分析失敗不留紀錄;migration 不會把歷史
轉成 v2 紀錄。

`is_exact = false` 表示該 v1 類別已知有信會落到別的 v2 類別(例如
新聞 → 內容電子報,但氣象特報屬公共警報)。訓練資料匯出會略過這些,
dashboard 顯示為約略值。職涯、公共警報在 v1 沒有對應類別。

## 第一次上線:維持 v1(行為不變)

```bash
helm upgrade zashiki -n zashiki deploy/helm/zashiki-warasi \
    -f deploy/helm/values.<cluster>.yaml \
    -f deploy/helm/values.<cluster>.secrets.yaml \
    --set image.tag=<sha>
```

entrypoint 會跑 alembic `0009`。接著在 pod 內:

```bash
POD=$(kubectl -n zashiki get pod -l app.kubernetes.io/name=zashiki-warasi \
      -o jsonpath='{.items[0].metadata.name}')
# 1. 補上 rollout 期間舊 pod 寫入的分析
kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- \
    python - < scripts/seed_classifications.py      # 預期 missing=0
# 2. 匯入 Phase 2 盲標結果(v1)
kubectl -n zashiki cp data/relabel/phase2_verified.jsonl \
    "$POD":/tmp/verdicts.jsonl -c zashiki-warasi
kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- python - \
    --input /tmp/verdicts.jsonl --taxonomy-version v1 --dry-run \
    < scripts/import_human_labels.py                # 確認後拿掉 --dry-run
```

## 切換到 v2

前提:LLM v2 已在純人工 test set 上評估過(tasks 第 8 組),並在
issue #12 記錄放行。

1. **關 shadow**:`values.<cluster>.yaml` 設 `LAYA_SHADOW_ENABLED: "0"`
   與 `observability.prometheusRule.alerts.shadowSilent.enabled: false`。
   laya v1 只懂 v1 類別,等 laya v2 通過門檻再開。
2. **排空**:部署上述變更(`helm upgrade`,同樣參數)。SIGTERM 會停止
   輪詢,並讓手上那封信在 `terminationGracePeriodSeconds`(120 秒)內
   處理完。
3. **未完成流程檢查**:
   ```bash
   kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- \
       python - < scripts/check_pending_threads.py
   ```
   exit 0 → 繼續。exit 1 會列出帶著 v1 分析、但沒有 `email_analyses`
   紀錄的流程;讓 poller 用 v1 把這些信處理完,重跑到清空為止。
4. **切換**:env 設 `TAXONOMY_VERSION: "v2"`,`helm upgrade`。pod log
   會出現 `classified as <v2 類別>`,Grafana 會出現 *Taxonomy cutover*
   標記。
5. **煙霧測試**:交易收據、帳單通知、行事曆事件、一個只通知的類別各寄
   一封。
6. **補灌**(`seed_classifications.py`),有 v2 人工標註就用
   `--taxonomy-version v2` 匯入。

## laya v2(切換之後)

用 v2 train split 訓練,在人工 test set 上單獨準確率 ≥ 85% 才放行,然後
一次 helm release:

```yaml
layaQuestionsFile: configs/laya-questions-v2.json
env:
  LAYA_SHADOW_ENABLED: "1"
  LAYA_SEMANTIC_VER: laya-zashiki-v2
observability.prometheusRule.alerts.shadowSilent.enabled: true
```

類別定義檔的分類版本和 `TAXONOMY_VERSION` 不一致時,shadow client
會在啟動時直接失敗——v2 下掛 v1 定義檔不會默默跑錯。

## 回滾

- **切換前**:直接退 image 即可;新欄位預設 `v1`,正是舊程式寫入的值。
- **切換後**:同樣先排空、跑未完成流程檢查,再設
  `TAXONOMY_VERSION: "v1"`。資料庫不用 downgrade,v2 紀錄留作歷史。
