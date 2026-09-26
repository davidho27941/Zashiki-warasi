"""Canonical serve-time question construction for the laya shadow.

MUST stay semantically identical to `scripts/finetune/
zashiki_laya_common.py` — the checkpoint was trained on exactly this
construction (15 effective labels, calendar merged), so any drift here
is silent train/serve skew. `tests/classifier/test_question.py` pins
byte-level equality of the two constructions.

Level-1 taxonomy: `event_info` + `meeting_invite` collapse into
`calendar_event`; the mapped-Chinese marker for the merged class is
`行事曆事件` (NOT a `Category` Literal value — it exists only in shadow
storage/metrics). Agreement applies the super-class rule via
`is_agreement`.
"""

from __future__ import annotations

import hashlib
import json
import typing

from zashiki_warasi.core.schemas import Category

CALENDAR_MERGE = frozenset({"event_info", "meeting_invite"})
CALENDAR_LABEL = "calendar_event"
CALENDAR_ZH = "行事曆事件"
CALENDAR_ZH_MEMBERS = frozenset({"講座資訊", "會議邀請"})
CALENDAR_CRITERIA = (
    "a schedulable calendar event: a meeting invitation addressed to the "
    "recipient, or a lecture / seminar / community event announcement — "
    "in all cases with a concrete date/time and a venue, organizer, or "
    "online meeting link; recommendations without a scheduled session "
    "are marketing, past-event thank-you or feedback mails are not this"
)


class QuestionsDictError(ValueError):
    """The questions dict fails validation — refuse to boot."""


def load_questions_file(path: str) -> tuple[dict, str, dict, dict]:
    """Load + validate the dict; return
    (question_spec, dict_hash8, zh_by_en, en_by_zh) with the calendar
    merge applied.

    Validation (spec: bijection onto the Category Literal):
    - label_map keys == criteria keys of the RAW dict
    - label_map values == exactly the Category Literal values
    Raises QuestionsDictError naming the offending entries.
    """
    raw_bytes = open(path, "rb").read()
    d = json.loads(raw_bytes)
    dict_hash8 = hashlib.sha1(raw_bytes).hexdigest()[:8]

    criteria_keys = set(d["question"]["criteria"])
    map_keys = set(d["label_map"])
    if criteria_keys != map_keys:
        raise QuestionsDictError(
            f"criteria/label_map key mismatch: {criteria_keys ^ map_keys}"
        )
    literal = set(typing.get_args(Category))
    map_values = set(d["label_map"].values())
    if map_values != literal:
        raise QuestionsDictError(
            f"label_map image != Category Literal: {map_values ^ literal}"
        )
    if len(map_values) != len(d["label_map"]):
        raise QuestionsDictError("label_map is not injective")

    criteria: dict[str, str] = {
        en: desc for en, desc in d["question"]["criteria"].items()
        if en not in CALENDAR_MERGE
    }
    criteria[CALENDAR_LABEL] = CALENDAR_CRITERIA

    en_by_zh = {
        zh: (CALENDAR_LABEL if en in CALENDAR_MERGE else en)
        for en, zh in d["label_map"].items()
    }
    zh_by_en = {en: zh for zh, en in en_by_zh.items()
                if en != CALENDAR_LABEL}
    zh_by_en[CALENDAR_LABEL] = CALENDAR_ZH

    question = {
        "type": "choice",
        "instructions": d["question"]["instructions"],
        "criteria": criteria,
    }
    return question, dict_hash8, zh_by_en, en_by_zh


def is_agreement(laya_zh: str, llm_zh: str) -> bool:
    """Super-class rule: laya's merged 行事曆事件 matches either of the
    two Literal calendar categories the LLM still emits."""
    if laya_zh == llm_zh:
        return True
    return laya_zh == CALENDAR_ZH and llm_zh in CALENDAR_ZH_MEMBERS
