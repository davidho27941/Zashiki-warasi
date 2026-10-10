"""Canonical serve-time question construction for the laya shadow.

MUST stay semantically identical to `scripts/finetune/
zashiki_laya_common.py` — the checkpoint was trained on exactly this
construction (15 effective labels, calendar merged), so any drift here
is silent train/serve skew. `tests/classifier/test_question.py` pins
byte-level equality of the two constructions.

Taxonomy v1 dict: `event_info` + `meeting_invite` collapse into
`calendar_event`; the mapped-Chinese marker for the merged class is
`行事曆事件` (NOT a v1 category — it exists only in shadow
storage/metrics). Agreement applies the super-class rule via
`is_agreement`.

Taxonomy v2 dict (`"taxonomy_version": "v2"`): a plain bijection onto
the 14 v2 categories, no merge, agreement is equality. A dict without
the key is v1. The dict's version must equal TAXONOMY_VERSION.
"""

from __future__ import annotations

import hashlib
import json

from zashiki_warasi.core.schemas import CATEGORIES_BY_VERSION

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


def load_questions_file(
    path: str, taxonomy_version: str = "v1",
) -> tuple[dict, str, dict, dict]:
    """Load + validate the dict; return
    (question_spec, dict_hash8, zh_by_en, en_by_zh), with the calendar
    merge applied for a v1 dict.

    Validation (spec: bijection onto the active vocabulary):
    - the dict's taxonomy version == `taxonomy_version`
    - label_map keys == criteria keys of the RAW dict
    - label_map values == exactly that version's categories
    Raises QuestionsDictError naming the offending entries.
    """
    raw_bytes = open(path, "rb").read()
    d = json.loads(raw_bytes)
    dict_hash8 = hashlib.sha1(raw_bytes).hexdigest()[:8]

    dict_version = d.get("taxonomy_version", "v1")
    if dict_version != taxonomy_version:
        raise QuestionsDictError(
            f"questions dict is taxonomy {dict_version} but "
            f"TAXONOMY_VERSION is {taxonomy_version}"
        )
    if dict_version not in CATEGORIES_BY_VERSION:
        raise QuestionsDictError(f"unknown taxonomy {dict_version!r}")

    criteria_keys = set(d["question"]["criteria"])
    map_keys = set(d["label_map"])
    if criteria_keys != map_keys:
        raise QuestionsDictError(
            f"criteria/label_map key mismatch: {criteria_keys ^ map_keys}"
        )
    vocabulary = set(CATEGORIES_BY_VERSION[dict_version])
    map_values = set(d["label_map"].values())
    if map_values != vocabulary:
        raise QuestionsDictError(
            f"label_map image != taxonomy {dict_version} categories: "
            f"{map_values ^ vocabulary}"
        )
    if len(map_values) != len(d["label_map"]):
        raise QuestionsDictError("label_map is not injective")

    if dict_version != "v1":
        question = {
            "type": "choice",
            "instructions": d["question"]["instructions"],
            "criteria": dict(d["question"]["criteria"]),
        }
        zh_by_en = dict(d["label_map"])
        en_by_zh = {zh: en for en, zh in zh_by_en.items()}
        return question, dict_hash8, zh_by_en, en_by_zh

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


def is_agreement(
    laya_zh: str, llm_zh: str, taxonomy_version: str = "v1",
) -> bool:
    """Equality, plus under v1 the super-class rule: laya's merged
    行事曆事件 matches either of the two v1 calendar categories."""
    if laya_zh == llm_zh:
        return True
    return (
        taxonomy_version == "v1"
        and laya_zh == CALENDAR_ZH
        and llm_zh in CALENDAR_ZH_MEMBERS
    )
