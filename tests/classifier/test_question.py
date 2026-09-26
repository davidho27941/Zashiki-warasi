"""Pin the serve-time question construction (v1.6.0).

Two contracts:
1. Package construction == scripts/finetune construction, byte-level —
   the checkpoint was trained on the scripts version, the shadow client
   serves the package version; drift here is silent train/serve skew.
2. Validation failures abort loudly (QuestionsDictError).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from zashiki_warasi.classifier.question import (
    CALENDAR_ZH,
    QuestionsDictError,
    is_agreement,
    load_questions_file,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
DICT_PATH = str(REPO / "deploy/helm/laya-classifier/configs/questions.json")


def _load_scripts_variant():
    spec = importlib.util.spec_from_file_location(
        "zashiki_laya_common",
        REPO / "scripts/finetune/zashiki_laya_common.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTrainServeConsistency:
    def test_question_identical_to_finetune_construction(self):
        pkg_q, _, pkg_zh_by_en, pkg_en_by_zh = load_questions_file(DICT_PATH)
        scripts = _load_scripts_variant()
        s_q, s_zh_by_en, s_en_by_zh = scripts.load_questions(DICT_PATH)
        assert pkg_q == s_q, "serve question != training question"
        assert pkg_zh_by_en == s_zh_by_en
        assert pkg_en_by_zh == s_en_by_zh

    def test_fifteen_effective_labels_with_merged_calendar(self):
        q, _, zh_by_en, en_by_zh = load_questions_file(DICT_PATH)
        assert len(q["criteria"]) == 15
        assert "calendar_event" in q["criteria"]
        assert en_by_zh["講座資訊"] == "calendar_event"
        assert en_by_zh["會議邀請"] == "calendar_event"
        assert zh_by_en["calendar_event"] == CALENDAR_ZH

    def test_dict_hash_changes_on_edit(self, tmp_path):
        original = open(DICT_PATH, encoding="utf-8").read()
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(original, encoding="utf-8")
        b.write_text(original.replace(
            "Which category", "WHICH category"), encoding="utf-8")
        _, ha, _, _ = load_questions_file(str(a))
        _, hb, _, _ = load_questions_file(str(b))
        assert ha != hb


class TestValidation:
    def _dict(self):
        return json.load(open(DICT_PATH, encoding="utf-8"))

    def _write(self, tmp_path, d):
        p = tmp_path / "q.json"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_extra_label_in_map_aborts(self, tmp_path):
        d = self._dict()
        d["label_map"]["ghost"] = "幽靈類"
        with pytest.raises(QuestionsDictError):
            load_questions_file(self._write(tmp_path, d))

    def test_missing_criteria_key_aborts(self, tmp_path):
        d = self._dict()
        del d["question"]["criteria"]["survey"]
        with pytest.raises(QuestionsDictError):
            load_questions_file(self._write(tmp_path, d))

    def test_typoed_zh_value_aborts(self, tmp_path):
        d = self._dict()
        d["label_map"]["survey"] = "問卷調察"  # typo
        with pytest.raises(QuestionsDictError):
            load_questions_file(self._write(tmp_path, d))


class TestAgreementRule:
    def test_exact_match(self):
        assert is_agreement("廣告", "廣告")

    def test_superclass_matches_both_calendar_literals(self):
        assert is_agreement(CALENDAR_ZH, "講座資訊")
        assert is_agreement(CALENDAR_ZH, "會議邀請")

    def test_superclass_does_not_match_others(self):
        assert not is_agreement(CALENDAR_ZH, "廣告")

    def test_plain_mismatch(self):
        assert not is_agreement("促銷", "廣告")
