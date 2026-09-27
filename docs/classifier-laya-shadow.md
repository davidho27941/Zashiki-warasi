# Laya shadow classifier (v1.6.0)

> 中文版:[`classifier-laya-shadow.zh.md`](classifier-laya-shadow.zh.md).
>
> **What this doc is**: the operator-facing guide for the shadow
> classifier — how to enable it, how to read the agreement data, and
> the caveats that matter when interpreting it.
>
> **What this doc is NOT**: the design rationale (see
> `openspec/changes/add-laya-shadow-classifier/design.md`, D1–D17,
> including the D10 eval-gate outcome); the formal contract (see
> `openspec/specs/classifier-shadow/spec.md` after sync).

## What it is

A fine-tuned [laya](https://pypi.org/project/laya/) encoder
(`laya-zashiki-email-v1`, RLCD fine-tune on ~4.8k operator-labeled
mails) runs **in shadow** next to the LLM classifier: every
successfully analyzed email is also sent to a CPU-only `laya-serve`
pod, and the two predictions are recorded side by side.

**The LLM stays authoritative.** Shadow output changes nothing —
no Telegram message, no `email_analyses` row, no graph edge. The
only artifacts are:

- a `laya_shadow_predictions` row per `(message_id, laya_model_ver)`
  — self-contained `(input_text, prediction)` samples, reusable as
  future training pairs;
- three Prometheus metric families (below).

Phase 2 (issue #4) decides, after ≥2 weeks of live agreement data,
whether laya takes real traffic in a hybrid setup.

## Enabling

Two moving parts, both in helm:

```bash
# 1. The classifier backend (separate chart, CPU-only)
helm install laya -n zashiki deploy/helm/laya-classifier

# 2. Zashiki with shadow on (values.<cluster>.yaml env block)
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

Kill switch: `LAYA_SHADOW_ENABLED=0` (default) or an empty
`LAYA_BASE_URL` — the client constructs inert and never touches the
network, the DB, or the metrics registry.

On boot the pod logs one line proving the flag landed:

```
laya shadow enabled: endpoint=... model_ver=laya-zashiki-v1-e44b310a
```

The `-e44b310a` suffix is `sha1(questions-dict bytes)[:8]` — any edit
to the questions ConfigMap rotates `laya_model_ver` automatically and
starts a fresh comparison series under the
`UNIQUE(message_id, laya_model_ver)` constraint.

### Timeout: why 5 s

`laya-serve`'s `/health` does not run inference, so the pod turns
Ready with a **cold** model: the first real request costs ~2 s where
warm requests take ~0.7–1.0 s (measured in-cluster, 1.5k-char input).
The 2 s default in code is fine for steady state but guarantees one
`timeout` row after every laya-serve restart — production sets
`LAYA_TIMEOUT_SECONDS: "5"`. Shadow calls run on a bounded background
pool, so a generous timeout costs the main pipeline nothing.

## Reading the data

### Metrics

| Family | Labels | Reading |
|---|---|---|
| `zashiki_classifier_shadow_agreement_total` | `llm_category`, `laya_category`, `agreed` | The live confusion matrix. Agreement rate = `sum(agreed="true") / sum()`. |
| `zashiki_classifier_shadow_duration_seconds` | `engine` | Shadow round-trip latency (wide buckets — CPU inference). |
| `zashiki_classifier_shadow_error_total` | `reason` | `timeout` / `connection_refused` / `http_5xx` / `bad_response` / `dropped_saturated`. |

### SQL

```sql
-- agreement rate for the current model version
SELECT laya_model_ver,
       count(*) FILTER (WHERE laya_error IS NULL)             AS scored,
       count(*) FILTER (WHERE llm_category = laya_category)   AS agreed
FROM laya_shadow_predictions GROUP BY 1;

-- disagreement drill-down (the interesting rows)
SELECT llm_category, laya_category, count(*)
FROM laya_shadow_predictions
WHERE laya_error IS NULL AND llm_category <> laya_category
GROUP BY 1, 2 ORDER BY 3 DESC;
```

`laya_alternates` holds the full 15-way probability vector per row —
use it to re-run the hybrid coverage/precision sweep (D10) on live
data at any threshold without re-calling the model.

## Caveats when interpreting agreement

1. **Super-class agreement**: laya predicts the merged
   `行事曆事件` (calendar_event) class; the LLM predicts the fine
   labels `會議邀請` / `講座資訊`. The agreement metric counts
   laya=`行事曆事件` vs LLM=either fine label as **agreed** (the
   274-row audit showed the fine boundary is humanly unstable —
   83 verdict flips). Raw SQL string equality will under-count
   agreement; use the metric or replicate the rule.
2. **Truncation confound**: the LLM sees the full email; laya sees
   `subject + body` capped at 1500 chars (`classifier/truncation.py`,
   snapshotted in `input_text`). A disagreement on a long email may
   be an information gap, not a model gap.
3. **LangGraph-resume gap**: emails that fail analysis and are
   resumed later get **no shadow row** — the hook sits on the
   success tail of `_analyze`. Expected; do not chase the missing
   rows.
4. **Counter-reset undercount**: the Grafana agreement panels use
   `increase(counter[range])`; Prometheus loses each series' birth
   value after a pod restart, so on deploy-heavy days the dashboard
   OVER-reports agreement (it drops mostly the rare disagreement
   series). Fine in steady state; for any decision, compute the rate
   from `laya_shadow_predictions` (the SQL above) — the DB is ground
   truth.
5. **Definition-sync duty**: the English criteria in
   `deploy/helm/zashiki-warasi/configs/laya-questions.json` mirror
   `ANALYZE_SYSTEM_PROMPT`. Any category-definition change in the
   prompt MUST be mirrored in the dict (and vice versa) — the
   train/serve byte-equality test in `tests/classifier/test_question.py`
   pins dict↔training-script consistency, but prompt↔dict sync is a
   human duty.

## Phase 2 decision tree (review no earlier than 2 weeks after enable)

Enabled on the lab cluster **2026-09-26**.

| Live agreement (super-class rule) | Action |
|---|---|
| ≥ 90% | Hybrid: laya answers when `max(prob) ≥ 0.80` (96.2% precision @ 66% coverage on held-out — re-validate the threshold on live data first), LLM handles the rest. |
| 85–90% | One more fine-tune round with the accumulated live pairs, then re-measure. |
| < 85% | Investigate disagreement clusters before spending on anything — the confusion matrix says where. |
