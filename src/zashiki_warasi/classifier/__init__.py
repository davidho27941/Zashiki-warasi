"""Shadow / candidate classify engines (v1.6.0 add-laya-shadow-classifier).

Phase 0 scope: laya runs observationally beside the authoritative LLM.
Nothing in this package is allowed to influence the email pipeline's
decisions — see `openspec/specs/classifier-shadow/spec.md` once synced
(until then: the change's spec delta) for the non-interference contract.
"""

from __future__ import annotations

from zashiki_warasi.classifier.truncation import (
    CLASSIFIER_BODY_PREFIX_CHARS,
    build_classifier_input,
)

__all__ = [
    "CLASSIFIER_BODY_PREFIX_CHARS",
    "build_classifier_input",
]
