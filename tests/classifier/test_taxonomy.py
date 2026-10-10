"""Taxonomy registry (openspec adopt-email-taxonomy-v2, tasks 1.x)."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from zashiki_warasi.classifier.taxonomy import TAXONOMIES, get_taxonomy
from zashiki_warasi.core.config import TaxonomySettings
from zashiki_warasi.core.schemas import (
    AnalysisOutputV1,
    AnalysisOutputV2,
    EmailAnalysis,
)

# sha256 of ANALYZE_SYSTEM_PROMPT before the registry existed. v1 must
# stay byte-identical so TAXONOMY_VERSION=v1 changes nothing.
_V1_PROMPT_SHA256 = (
    "9c10cb52e5d978ca8c133dbbc38538fb729a35cd8b08add80c409c6777f30d89"
)


def test_v1_prompt_is_byte_identical_to_pre_registry_prompt():
    prompt = get_taxonomy("v1").system_prompt
    assert hashlib.sha256(prompt.encode()).hexdigest() == _V1_PROMPT_SHA256


def test_v2_prompt_lists_exactly_the_v2_categories():
    v2 = get_taxonomy("v2")
    assert len(v2.categories) == 14
    listing = v2.system_prompt.split("從以下列表選擇")[1].split("各類定義")[0]
    for category in v2.categories:
        assert category in listing
    # Retired v1 names must not appear as category labels 「…」 (the
    # 行銷 definition may still use 廣告 / 促銷 as plain words).
    section = v2.system_prompt.split("### 3. 分類")[1]
    for gone in ("消費支出", "消費資訊彙整", "點數資訊彙整", "講座資訊",
                 "會議邀請", "廣告", "促銷", "訂閱服務", "技術文章",
                 "新聞", "股票資訊"):
        assert f"「{gone}」" not in section


def test_output_models_keep_the_tool_name_and_field_order():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    for model, n in ((AnalysisOutputV1, 16), (AnalysisOutputV2, 14)):
        fn = convert_to_openai_tool(model)["function"]
        assert fn["name"] == "EmailAnalysis"
        props = fn["parameters"]["properties"]
        assert list(props) == [
            "importance", "urgency", "category", "summary", "keywords",
        ]
        assert len(props["category"]["enum"]) == n


def test_v2_output_model_rejects_v1_category():
    with pytest.raises(ValidationError):
        AnalysisOutputV2(
            importance=3, urgency="normal", category="消費支出", summary="s",
        )


@pytest.mark.parametrize(
    ("version", "category", "vertical"),
    [
        ("v1", "消費支出", "expense"),
        ("v1", "帳單通知", "expense"),
        ("v1", "會議邀請", "calendar"),
        ("v1", "講座資訊", "calendar"),
        ("v1", "廣告", None),
        ("v2", "交易收據", "expense"),
        ("v2", "帳單通知", "expense"),
        ("v2", "行事曆事件", "calendar"),
        ("v2", "帳務彙整", None),
        ("v2", "職涯", None),
        ("v2", "行銷", None),
    ],
)
def test_routing_map(version, category, vertical):
    assert get_taxonomy(version).vertical_for(category) == vertical


def test_routes_only_name_known_categories():
    for taxonomy in TAXONOMIES.values():
        assert set(taxonomy.routes) <= set(taxonomy.categories)


def test_unknown_version_is_rejected():
    with pytest.raises(ValueError, match="v3"):
        get_taxonomy("v3")


def test_settings_default_and_invalid_env(monkeypatch):
    monkeypatch.delenv("TAXONOMY_VERSION", raising=False)
    assert TaxonomySettings(_env_file=None).version == "v1"
    monkeypatch.setenv("TAXONOMY_VERSION", "v2")
    assert TaxonomySettings(_env_file=None).version == "v2"
    monkeypatch.setenv("TAXONOMY_VERSION", "v3")
    with pytest.raises(ValidationError):
        TaxonomySettings(_env_file=None)


class TestEmailAnalysisVersion:
    def test_defaults_to_v1(self):
        a = EmailAnalysis(
            importance=3, urgency="normal", category="消費支出", summary="s",
        )
        assert a.taxonomy_version == "v1"

    def test_pre_v2_checkpoint_payload_loads_as_v1(self):
        payload = {
            "importance": 3, "urgency": "normal", "category": "講座資訊",
            "summary": "s", "keywords": [],
        }
        assert EmailAnalysis.model_validate(payload).taxonomy_version == "v1"

    @pytest.mark.parametrize(
        ("version", "category"), [("v1", "交易收據"), ("v2", "消費支出")],
    )
    def test_category_must_belong_to_its_version(self, version, category):
        with pytest.raises(ValidationError, match="not in taxonomy"):
            EmailAnalysis(
                importance=3, urgency="normal", category=category,
                summary="s", taxonomy_version=version,
            )

    def test_same_name_differs_only_by_version(self):
        v1 = EmailAnalysis(
            importance=3, urgency="normal", category="安全通知", summary="s",
        )
        v2 = v1.model_copy(update={"taxonomy_version": "v2"})
        assert v1.category == v2.category
        assert v1.taxonomy_version != v2.taxonomy_version
