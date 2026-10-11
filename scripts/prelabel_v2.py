"""Pre-label stored mail text with the taxonomy-v2 analyze prompt.

openspec adopt-email-taxonomy-v2, task 8.1. Runs the production analyze
prompt + structured-output schema for v2 (from the taxonomy registry,
so it is exactly what the cutover will serve) over the stored
`input_text` of the training corpus, on the private LLM server. The
output seeds the v2 TRAIN split and the stratified sampling of the
human-only TEST split; it is never used as test-set truth.

    uv run python scripts/prelabel_v2.py \\
        --input data/training_pairs.jsonl \\
        --output data/relabel/v2_prelabel.jsonl \\
        --base-url http://192.168.1.254:8000/v1 --concurrency 4 --limit 50

Input rows need `message_id` and `input_text` (training_pairs /
shadow export shape); `deleted` tombstones are skipped. Output rows:

    {"message_id", "category_v2", "v1_category", "mojibake", "latency_s",
     "model", "prompt_sha8", "source": "training_pairs"|..., "error"?}

Resumable: message_ids already in --output are skipped; failed rows
are written with `error` and retried on the next run (they are not
counted as done).

`input_text` is the laya-side snapshot (subject + body, 1500-char cap),
not the full mail the production LLM sees — labels from long mails can
differ; that is acceptable for pre-labels and recorded in design D9.

The LLM server also serves the live agent. Run only in a scheduled
production pause (then `--concurrency 4` uses all llama.cpp slots);
the default 2 leaves slots free if the agent is still up.

PRIVACY: real mail text goes only to the private LLM server; input and
output stay in gitignored data/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import create_model

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.classifier.taxonomy import get_taxonomy
from zashiki_warasi.core.config import LLMSettings

MOJIBAKE_MIN_REPLACEMENTS = 10


def category_prefix_model(output_model):
    """The v2 output schema cut after `category`.

    Fields are generated in schema order (importance, urgency,
    category, summary, keywords), so `category` is decided before the
    summary exists: stopping after it leaves the category decision's
    context unchanged while skipping the 50-200 字 summary, the bulk of
    each call's latency. Same title and description as the full schema.
    """
    keep = ("importance", "urgency", "category")
    fields = {
        name: (output_model.model_fields[name].annotation,
               output_model.model_fields[name])
        for name in keep
    }
    model = create_model(
        output_model.__name__ + "CategoryPrefix",
        __config__=output_model.model_config,
        __doc__=output_model.__doc__,
        **fields,
    )
    return model


def load_done(path: Path) -> set[str]:
    done: set[str] = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                row = json.loads(line)
                if "error" not in row:
                    done.add(row["message_id"])
    return done


def load_inputs(paths: list[str], done: set[str]) -> list[dict]:
    rows: dict[str, dict] = {}
    for path in paths:
        source = Path(path).stem
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("deleted") or not r.get("input_text"):
                continue
            if r["message_id"] in done or r["message_id"] in rows:
                continue
            rows[r["message_id"]] = {
                "message_id": r["message_id"],
                "input_text": r["input_text"],
                "v1_category": r.get("category"),
                "source": source,
            }
    return list(rows.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", action="append", required=True,
                    help="JSONL with message_id + input_text; repeatable")
    ap.add_argument("--output", default="data/relabel/v2_prelabel.jsonl")
    ap.add_argument("--base-url", default=None,
                    help="override LLM_BASE_URL")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--category-only", action="store_true",
                    help="stop the schema after `category` (much faster); "
                         "validate against the full schema on a sample "
                         "before using it for a full run")
    args = ap.parse_args()

    taxonomy = get_taxonomy("v2")
    prompt_sha8 = hashlib.sha1(taxonomy.system_prompt.encode()).hexdigest()[:8]
    overrides = {"base_url": args.base_url} if args.base_url else {}
    settings = LLMSettings(**overrides)
    schema = (category_prefix_model(taxonomy.output_model)
              if args.category_only else taxonomy.output_model)
    model = get_chat_model(
        settings, max_tokens=settings.analyze_max_tokens,
    ).with_structured_output(schema)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    todo = load_inputs(args.input, load_done(out_path))
    if args.limit is not None:
        todo = todo[: args.limit]
    print(f"{len(todo)} to label with v2 prompt {prompt_sha8} "
          f"(concurrency {args.concurrency})", file=sys.stderr)

    def classify(row: dict) -> dict:
        started = time.monotonic()
        record = {
            "message_id": row["message_id"],
            "v1_category": row["v1_category"],
            "source": row["source"],
            "mojibake": row["input_text"].count("�")
            >= MOJIBAKE_MIN_REPLACEMENTS,
            "model": settings.model,
            "prompt_sha8": prompt_sha8,
            "schema": "category_prefix" if args.category_only else "full",
        }
        try:
            result = model.invoke([
                SystemMessage(content=taxonomy.system_prompt),
                HumanMessage(content=row["input_text"]),
            ])
            record["category_v2"] = result.category
        except Exception as exc:  # noqa: BLE001 — recorded, retried next run
            record["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        record["latency_s"] = round(time.monotonic() - started, 2)
        return record

    started = time.monotonic()
    ok = failed = 0
    with out_path.open("a", encoding="utf-8") as out, ThreadPoolExecutor(
        max_workers=args.concurrency,
    ) as pool:
        futures = [pool.submit(classify, row) for row in todo]
        for i, future in enumerate(as_completed(futures), 1):
            record = future.result()
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            if "error" in record:
                failed += 1
            else:
                ok += 1
            if i % 25 == 0 or i == len(futures):
                elapsed = time.monotonic() - started
                rate = i / elapsed if elapsed else 0.0
                eta_h = (len(futures) - i) / rate / 3600 if rate else 0.0
                print(f"{i}/{len(futures)} ok={ok} failed={failed} "
                      f"{rate * 60:.1f}/min eta={eta_h:.1f}h",
                      file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
