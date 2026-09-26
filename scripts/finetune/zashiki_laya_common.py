"""Shared pieces for the Zashiki laya fine-tune (train + eval MUST agree).

Level-1 taxonomy (openspec add-laya-shadow-classifier, two-level design):
the classifier trains and serves on a MERGED calendar super-class —
`event_info` + `meeting_invite` collapse into `calendar_event` — because
(a) both route to calendar_sg identically and (b) the human audit showed
the fine boundary is unstable even for the operator (83/274 flips).
Fine-grained display labels are a Phase-1 `summarize` concern, not ours.

Everything here derives from `deploy/helm/laya-classifier/configs/
questions.json` so the wire dict stays the single source of truth.
"""

from __future__ import annotations

import json

# en labels merged into the calendar super-class.
CALENDAR_MERGE = {"event_info", "meeting_invite"}
CALENDAR_LABEL = "calendar_event"
CALENDAR_CRITERIA = (
    "a schedulable calendar event: a meeting invitation addressed to the "
    "recipient, or a lecture / seminar / community event announcement — "
    "in all cases with a concrete date/time and a venue, organizer, or "
    "online meeting link; recommendations without a scheduled session "
    "are marketing, past-event thank-you or feedback mails are not this"
)


def load_questions(path: str) -> dict:
    """Return (question_spec, zh_by_en, en_by_zh) with the calendar merge
    applied. zh labels 講座資訊/會議邀請 BOTH map to `calendar_event` on
    the way in; `calendar_event` maps back to the super-class marker
    `行事曆事件` on the way out (storage/reporting only)."""
    d = json.load(open(path, encoding="utf-8"))
    criteria: dict[str, str] = {}
    for en, desc in d["question"]["criteria"].items():
        if en in CALENDAR_MERGE:
            continue
        criteria[en] = desc
    criteria[CALENDAR_LABEL] = CALENDAR_CRITERIA

    en_by_zh: dict[str, str] = {}
    for en, zh in d["label_map"].items():
        en_by_zh[zh] = CALENDAR_LABEL if en in CALENDAR_MERGE else en

    zh_by_en = {en: zh for zh, en in en_by_zh.items()
                if en != CALENDAR_LABEL}
    zh_by_en[CALENDAR_LABEL] = "行事曆事件"

    question = {
        "type": "choice",
        "instructions": d["question"]["instructions"],
        "criteria": criteria,
    }
    return question, zh_by_en, en_by_zh


def load_pairs(path: str, en_by_zh: dict[str, str]) -> list[dict]:
    """Load train/test jsonl rows → [{input_text, en_label, zh_label}]."""
    rows = []
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("deleted"):
            continue
        en = en_by_zh.get(r["category"])
        if en is None:
            raise SystemExit(
                f"category {r['category']!r} not in label_map — "
                "questions.json and the dataset are out of sync"
            )
        rows.append({
            "input_text": r["input_text"],
            "en_label": en,
            "zh_label": r["category"],
        })
    return rows
