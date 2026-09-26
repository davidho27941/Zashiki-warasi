"""Pinned input-truncation rule for encoder-based classify engines.

laya is an encoder with a bounded context (~512-token class); long
emails must be truncated BEFORE the request, and the truncated string
is what gets persisted in `laya_shadow_predictions.input_text`. Three
consumers depend on this being ONE function:

- the Phase 0 shadow client (stored text == sent text, by construction)
- the future Phase 2 classify engine (train/serve consistency: a
  fine-tuned checkpoint sees the same truncation at inference that its
  training pairs were built from)
- the issue-#6 audit CLI (the reviewer judges exactly what the model saw)

The body source follows the SAME fallback chain the LLM analyze path
uses (`body_plain` → html_to_text(body_html) → snippet → ""), so
disagreement analysis compares engines on comparable inputs — modulo
the documented confound that the LLM receives the full text while the
encoder receives this prefix.

Changing `CLASSIFIER_BODY_PREFIX_CHARS` is a comparison-series-breaking
event: bump the questions-dict / semantic version so `laya_model_ver`
rotates (see the shadow client), never tune it silently mid-observation.
"""

from __future__ import annotations

from zashiki_warasi.agents.verticals.html_text import html_to_text
from zashiki_warasi.core.schemas import EmailMessage

# Pinned by openspec/changes/add-laya-shadow-classifier (design D3).
# ~1500 chars sits comfortably inside typical encoder token limits for
# zh-heavy text (zh runs ≈1 char/token) while keeping the subject +
# opening paragraphs where category signal concentrates.
CLASSIFIER_BODY_PREFIX_CHARS: int = 1500


def build_classifier_input(email: EmailMessage) -> str:
    """Return the exact text a classify engine should see for `email`.

    `f"{subject}\\n\\n{body_prefix}"` — body via the analyze path's
    fallback chain, capped at `CLASSIFIER_BODY_PREFIX_CHARS`. When the
    chain yields nothing, the subject alone is returned (no dangling
    separator). Deterministic: same EmailMessage → same string.
    """
    body = (
        email.body_plain
        or html_to_text(email.body_html)
        or email.snippet
        or ""
    )
    prefix = body[:CLASSIFIER_BODY_PREFIX_CHARS]
    if not prefix:
        return email.subject
    return f"{email.subject}\n\n{prefix}"
