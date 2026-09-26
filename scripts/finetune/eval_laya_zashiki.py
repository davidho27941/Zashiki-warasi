"""Held-out evaluation for the Zashiki laya fine-tune — the D10 gate.

Runs the merged 15-label question over data/test.jsonl and reports
overall + per-class accuracy, the top confusion pairs, and confidence
stats for correct vs wrong predictions. The gate: **overall ≥85% with
no catastrophic class** → GO for Phase 0 deployment with this
checkpoint; below → iterate (criteria / more data) or NO-GO.

    python eval_laya_zashiki.py --model laya_zashiki_v1 \
        --test test.jsonl --questions questions.json
    python eval_laya_zashiki.py --model base ...   # zero-shot baseline

Uses the SAME question construction as training (zashiki_laya_common),
so eval == train == future serve.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict

from zashiki_laya_common import load_pairs, load_questions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="laya_zashiki_v1",
                    help="fine-tuned checkpoint dir, or 'base' for the "
                         "zero-shot multilingual checkpoint")
    ap.add_argument("--test", default="test.jsonl")
    ap.add_argument("--questions", default="questions.json")
    ap.add_argument("--report", default="eval_report.json")
    args = ap.parse_args()

    question, zh_by_en, en_by_zh = load_questions(args.questions)
    rows = load_pairs(args.test, en_by_zh)
    print(f"{len(rows)} test rows, "
          f"{len(question['criteria'])} effective labels")

    from laya.agent import load as load_agent
    if args.model == "base":
        agent = load_agent("convaiinnovations/laya",
                           subfolder="multilingual")
    else:
        agent = load_agent(args.model)

    questions = {"category": question}
    per_class = defaultdict(lambda: [0, 0])  # en_label -> [correct, total]
    confusion = Counter()
    conf_correct, conf_wrong = [], []
    latencies = []
    hits = 0
    preds = []  # (confidence, correct) per row, for the hybrid sweep

    for i, row in enumerate(rows):
        t0 = time.monotonic()
        result = agent.system_one(row["input_text"], questions)
        latencies.append((time.monotonic() - t0) * 1000)
        a = result["answers"]["category"]
        pred, gold = a["choice"], row["en_label"]
        ok = pred == gold
        hits += ok
        preds.append((a["confidence"], ok))
        per_class[gold][1] += 1
        if ok:
            per_class[gold][0] += 1
            conf_correct.append(a["confidence"])
        else:
            confusion[(gold, pred)] += 1
            conf_wrong.append(a["confidence"])
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(rows)} acc so far {hits / (i + 1):.1%}")

    overall = hits / len(rows)
    latencies.sort()
    print(f"\n===== RESULT ({args.model}) =====")
    print(f"overall accuracy: {overall:.1%}  ({hits}/{len(rows)})")
    print(f"latency p50 {latencies[len(latencies) // 2]:.0f}ms  "
          f"p95 {latencies[int(len(latencies) * .95)]:.0f}ms")

    print(f"\n{'class (zh)':14s} {'acc':>7s} {'n':>4s}")
    worst = 1.0
    for en, (c, t) in sorted(per_class.items(), key=lambda kv: -kv[1][1]):
        acc = c / t if t else 0.0
        worst = min(worst, acc) if t >= 5 else worst
        print(f"{zh_by_en[en]:14s} {acc:7.1%} {t:4d}")

    print("\ntop confusion (gold → pred):")
    for (g, p), n in confusion.most_common(10):
        print(f"  {zh_by_en[g]} → {zh_by_en[p]}: {n}")

    if conf_correct and conf_wrong:
        mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
        print(f"\nconfidence: correct mean {mean(conf_correct):.3f}  "
              f"wrong mean {mean(conf_wrong):.3f}")
        hi_wrong = sum(1 for c in conf_wrong if c >= 0.85)
        print(f"wrong-at-conf≥0.85: {hi_wrong} "
              f"({hi_wrong / len(rows):.1%} of all rows) — "
              "hybrid-threshold viability signal")

    # Hybrid sweep: if laya only answers when confidence ≥ T (else LLM
    # fallback), what precision does the laya-handled slice get, and how
    # much traffic does it cover (= LLM calls saved)?
    print("\nhybrid sweep (accept laya when conf ≥ T, else LLM fallback):")
    print(f"  {'T':>5s} {'coverage':>9s} {'precision':>10s} {'llm-saved/day@60':>17s}")
    sweep = []
    for t in (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        take = [ok for c, ok in preds if c >= t]
        cov = len(take) / len(preds)
        prec = (sum(take) / len(take)) if take else 0.0
        sweep.append({"threshold": t, "coverage": cov, "precision": prec})
        print(f"  {t:5.2f} {cov:9.1%} {prec:10.1%} {cov * 60:17.0f}")

    verdict = ("GO" if overall >= 0.85 and worst >= 0.60
               else "ITERATE/NO-GO")
    print(f"\nD10 gate (≥85% overall, no class <60% at n≥5): {verdict}")

    json.dump({
        "model": args.model,
        "overall": overall,
        "n": len(rows),
        "per_class": {zh_by_en[en]: {"correct": c, "total": t}
                      for en, (c, t) in per_class.items()},
        "confusion": [{"gold": zh_by_en[g], "pred": zh_by_en[p], "n": n}
                      for (g, p), n in confusion.most_common(20)],
        "latency_ms_p50": latencies[len(latencies) // 2],
        "hybrid_sweep": sweep,
        "verdict": verdict,
    }, open(args.report, "w", encoding="utf-8"),
        ensure_ascii=False, indent=2)
    print(f"report → {args.report}")


if __name__ == "__main__":
    main()
