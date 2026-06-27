"""EmailAnalysis schema validators (importance coercion regression)."""

from __future__ import annotations

import pytest

from zashiki_warasi.core.schemas import EmailAnalysis


def _make(importance) -> EmailAnalysis:
    return EmailAnalysis(
        importance=importance,
        urgency="normal",
        category="其他",
        summary="x",
    )


class TestImportanceCoercion:
    """llama.cpp + ChatOpenAI.with_structured_output() sometimes lets
    the model return importance as a string. Pydantic should still
    yield a valid int 1-5 rather than blowing up the agent."""

    def test_int_passes_through(self):
        assert _make(4).importance == 4

    @pytest.mark.parametrize(
        "raw,expected",
        [("1", 1), ("3", 3), ("5", 5)],
    )
    def test_digit_string_coerced(self, raw, expected):
        assert _make(raw).importance == expected

    def test_digit_with_trailing_label(self):
        # Model sometimes returns "4 (重要)" or "4 重要".
        assert _make("4 (重要)").importance == 4
        assert _make("4 重要").importance == 4

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("非常不重要", 1),
            ("不重要", 2),
            ("普通", 3),
            ("重要", 4),
            ("非常重要", 5),
        ],
    )
    def test_chinese_labels_coerced(self, label, expected):
        assert _make(label).importance == expected

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("very low", 1),
            ("low", 2),
            ("medium", 3),
            ("high", 4),
            ("very high", 5),
        ],
    )
    def test_english_labels_coerced(self, label, expected):
        assert _make(label).importance == expected

    def test_out_of_range_rejected(self):
        # ge/le bounds still enforced after coercion.
        with pytest.raises(Exception):
            _make(0)
        with pytest.raises(Exception):
            _make(6)
        with pytest.raises(Exception):
            _make("7")

    def test_garbage_string_rejected(self):
        # Unmappable labels surface as validation error so we know.
        with pytest.raises(Exception):
            _make("definitely not a level")
