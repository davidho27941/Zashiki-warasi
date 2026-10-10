"""Taxonomy registry: one entry per category vocabulary version.

Each entry bundles everything that must change together when the
vocabulary changes — the category tuple, the LLM structured-output
schema, the analyze prompt, and the category → vertical routing map.
`TAXONOMY_VERSION` selects the entry used for new analyses; routing
an existing analysis uses the entry named by its own
`taxonomy_version`, so a v1 analysis is always routed by v1 rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel

from zashiki_warasi.classifier.analyze_prompts import (
    CATEGORY_SECTION_V1,
    CATEGORY_SECTION_V2,
    IMPORTANCE_RULES_V1,
    IMPORTANCE_RULES_V2,
    build_analyze_prompt,
)
from zashiki_warasi.core.schemas import (
    CATEGORIES_BY_VERSION,
    AnalysisOutputV1,
    AnalysisOutputV2,
)

Vertical = Literal["expense", "calendar"]


@dataclass(frozen=True)
class Taxonomy:
    version: str
    categories: tuple[str, ...]
    output_model: type[BaseModel]
    system_prompt: str
    # Categories absent from the map route to notify.
    routes: Mapping[str, Vertical]

    def vertical_for(self, category: str) -> Vertical | None:
        return self.routes.get(category)


def _build(
    version: str,
    output_model: type[BaseModel],
    importance_rules: str,
    category_section: str,
    routes: dict[str, Vertical],
) -> Taxonomy:
    categories = CATEGORIES_BY_VERSION[version]
    unknown = set(routes) - set(categories)
    if unknown:
        raise ValueError(f"taxonomy {version}: routes name {unknown}")
    return Taxonomy(
        version=version,
        categories=categories,
        output_model=output_model,
        system_prompt=build_analyze_prompt(importance_rules, category_section),
        routes=MappingProxyType(routes),
    )


TAXONOMIES: Mapping[str, Taxonomy] = MappingProxyType({
    "v1": _build(
        "v1",
        AnalysisOutputV1,
        IMPORTANCE_RULES_V1,
        CATEGORY_SECTION_V1,
        {
            "消費支出": "expense",
            "帳單通知": "expense",
            "會議邀請": "calendar",
            "講座資訊": "calendar",
        },
    ),
    "v2": _build(
        "v2",
        AnalysisOutputV2,
        IMPORTANCE_RULES_V2,
        CATEGORY_SECTION_V2,
        {
            "交易收據": "expense",
            "帳單通知": "expense",
            "行事曆事件": "calendar",
        },
    ),
})


def get_taxonomy(version: str) -> Taxonomy:
    try:
        return TAXONOMIES[version]
    except KeyError:
        raise ValueError(
            f"unknown taxonomy version {version!r} "
            f"(known: {', '.join(TAXONOMIES)})"
        ) from None
