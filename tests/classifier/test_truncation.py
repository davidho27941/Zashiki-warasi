"""Pin the classifier input-truncation contract (v1.6.0 D3).

The stored `input_text` must be byte-identical to what the engine
receives, so this function's behavior IS the contract: deterministic,
capped, same body-fallback chain as the LLM analyze path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from zashiki_warasi.classifier import (
    CLASSIFIER_BODY_PREFIX_CHARS,
    build_classifier_input,
)
from zashiki_warasi.core.schemas import EmailMessage


def _email(**overrides) -> EmailMessage:
    defaults = dict(
        id="msg-trunc",
        thread_id="t",
        history_id=1,
        from_address="x@example.com",
        subject="測試主旨",
        snippet="",
        body_plain=None,
        body_html=None,
        received_at=datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return EmailMessage(**defaults)


class TestBuildClassifierInput:
    def test_deterministic(self):
        email = _email(body_plain="正文內容 " * 100)
        assert build_classifier_input(email) == build_classifier_input(email)

    def test_body_plain_preferred_over_html(self):
        email = _email(
            body_plain="純文字版本",
            body_html="<p>HTML 版本</p>",
        )
        assert "純文字版本" in build_classifier_input(email)
        assert "HTML 版本" not in build_classifier_input(email)

    def test_html_fallback_when_no_plain(self):
        email = _email(body_html="<p>只有 HTML 的信</p>")
        out = build_classifier_input(email)
        assert "只有 HTML 的信" in out
        assert "<p>" not in out  # converted, not raw

    def test_snippet_fallback_when_no_bodies(self):
        email = _email(snippet="snippet 內容")
        assert build_classifier_input(email) == "測試主旨\n\nsnippet 內容"

    def test_subject_only_when_chain_empty(self):
        email = _email()
        # No dangling separator when there is no body at all.
        assert build_classifier_input(email) == "測試主旨"

    def test_long_body_truncated_at_cap(self):
        body = "字" * (CLASSIFIER_BODY_PREFIX_CHARS + 5000)
        email = _email(body_plain=body)
        out = build_classifier_input(email)
        expected = f"測試主旨\n\n{body[:CLASSIFIER_BODY_PREFIX_CHARS]}"
        assert out == expected
        # The body portion is exactly the cap, not the cap ± separator.
        assert len(out) == len("測試主旨\n\n") + CLASSIFIER_BODY_PREFIX_CHARS

    def test_short_body_not_padded(self):
        email = _email(body_plain="短")
        assert build_classifier_input(email) == "測試主旨\n\n短"

    def test_unicode_slice_is_codepoint_safe(self):
        # Python str slicing operates on code points — a zh/emoji body
        # sliced at the cap must remain valid text (no exceptions on
        # encode, length exactly the cap).
        body = "🎉中文字元" * 1000
        email = _email(body_plain=body)
        out = build_classifier_input(email)
        prefix = out.split("\n\n", 1)[1]
        assert len(prefix) == CLASSIFIER_BODY_PREFIX_CHARS
        prefix.encode("utf-8")  # must not raise
