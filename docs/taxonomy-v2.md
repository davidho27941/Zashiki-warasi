# Email taxonomy v2

> 中文版:[`taxonomy-v2.zh.md`](taxonomy-v2.zh.md).
>
> **What this doc is**: the operator guide for the category vocabulary
> switch — what changes, where history lives, and the cutover /
> rollback runbook.
>
> **What this doc is NOT**: the rationale (see
> `openspec/changes/adopt-email-taxonomy-v2/design.md`, D1–D10) or the
> category definitions (normative in that change's
> `specs/email-classification/spec.md`; rendered for the LLM in
> `src/zashiki_warasi/classifier/analyze_prompts.py`).

## What changes

| | v1 (16) | v2 (14) |
|---|---|---|
| Expense vertical | 消費支出, 帳單通知 | 交易收據, 帳單通知 |
| Calendar vertical | 會議邀請, 講座資訊 | 行事曆事件 |
| Everything else | notify | notify |

v2 merges 廣告 + 促銷 → 行銷 and 會議邀請 + 講座資訊 → 行事曆事件, adds
內容電子報, 職涯, 公共警報, 投資帳戶通知, 服務通知, and keeps the
four-field rule (date, merchant, amount, **identifier**) for 交易收據.
A recruiter mail proposing an interview time is 行事曆事件, not 職涯.

`TAXONOMY_VERSION` (`v1` default | `v2`) selects the prompt, the LLM
output schema and the routing map for **new** analyses. An analysis is
always routed by the version it was produced under.

## Where classifications live

| Object | Role |
|---|---|
| `email_analyses.category` + `.taxonomy_version` | Write-once record of the LLM's first judgement. Do not analyse from it. |
| `email_classifications` | History, transaction-time SCD type 2. Current row ⇔ `valid_to IS NULL`. One current row per message, no overlaps (DB-enforced). |
| `taxonomy_mappings` | Append-only v1 → v2 map (`mapping_rev`, `is_exact`). |
| `email_category_current` (view) | **The** read path: current row mapped to the newest taxonomy, with `is_exact`. Dashboards, training export, ad-hoc SQL. |

Rules the writer enforces: a new version opens only when
`(category, taxonomy_version, source)` changes; a `human` row is never
closed by `llm` / `laya`; analysis failures get no row; migrations never
convert history into v2 rows.

`is_exact = false` marks a v1 class that is known to leak into other v2
classes (e.g. 新聞 → 內容電子報, though weather alerts are 公共警報).
Training export skips those rows; dashboards show them as approximate.
職涯 and 公共警報 have no v1 predecessor.

## Release N: ship with v1 (no behaviour change)

```bash
helm upgrade zashiki -n zashiki deploy/helm/zashiki-warasi \
    -f deploy/helm/values.<cluster>.yaml \
    -f deploy/helm/values.<cluster>.secrets.yaml \
    --set image.tag=<sha>
```

The entrypoint runs alembic `0009`. Then, inside the pod:

```bash
POD=$(kubectl -n zashiki get pod -l app.kubernetes.io/name=zashiki-warasi \
      -o jsonpath='{.items[0].metadata.name}')
# 1. history for analyses the old pod wrote during the rollout
kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- \
    python - < scripts/seed_classifications.py      # expect missing=0
# 2. Phase 2 blind labels as human history (v1)
kubectl -n zashiki cp data/relabel/phase2_verified.jsonl \
    "$POD":/tmp/verdicts.jsonl -c zashiki-warasi
kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- python - \
    --input /tmp/verdicts.jsonl --taxonomy-version v1 --dry-run \
    < scripts/import_human_labels.py                # then without --dry-run
```

## Cutover to v2

Prerequisite: LLM v2 scored on the human-only test set (tasks §8) and
the go recorded in issue #12.

1. **Shadow off** in `values.<cluster>.yaml`: `LAYA_SHADOW_ENABLED: "0"`
   and `observability.prometheusRule.alerts.shadowSilent.enabled: false`.
   laya v1 speaks the v1 vocabulary; it stays off until laya v2 passes
   its gate.
2. **Drain**: deploy that change (`helm upgrade`, same flags). SIGTERM
   stops polling and lets the email in hand finish within
   `terminationGracePeriodSeconds` (120 s).
3. **Pending-thread gate**:
   ```bash
   kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- \
       python - < scripts/check_pending_threads.py
   ```
   Exit 0 → continue. Exit 1 lists threads holding a v1 analysis with
   no `email_analyses` row; let the poller re-handle those messages
   under v1, re-run until empty.
4. **Flip**: `TAXONOMY_VERSION: "v2"` in the env block, `helm upgrade`.
   The pod logs `classified as <v2 category>`; Grafana shows the
   *Taxonomy cutover* annotation.
5. **Smoke**: one mail each for 交易收據, 帳單通知, 行事曆事件 and a
   notify-only class.
6. **Re-seed** (`seed_classifications.py`) and import any v2 human
   labels with `--taxonomy-version v2`.

## laya v2 (after the cutover)

Train on the v2 train split, gate at ≥ 85 % standalone on the human
test set, then one helm release:

```yaml
layaQuestionsFile: configs/laya-questions-v2.json
env:
  LAYA_SHADOW_ENABLED: "1"
  LAYA_SEMANTIC_VER: laya-zashiki-v2
observability.prometheusRule.alerts.shadowSilent.enabled: true
```

The shadow client refuses to boot when the dict's taxonomy differs from
`TAXONOMY_VERSION` — a v1 dict under v2 fails at startup, not silently.

## Rollback

- **Before the flip**: image rollback is safe; the new columns default
  to `v1`, which is what old code writes.
- **After the flip**: repeat the drain and pending-thread gate, then
  `TAXONOMY_VERSION: "v1"`. No DB downgrade; v2 rows stay as history.
