"""html_to_text: HTML → LLM-friendly plain text conversion."""

from __future__ import annotations

import pytest

from zashiki_warasi.agents.verticals.html_text import html_to_text


# --- empty / falsy inputs ---


class TestEmptyInputs:
    def test_none_returns_empty(self):
        assert html_to_text(None) == ""

    def test_empty_string_returns_empty(self):
        assert html_to_text("") == ""

    @pytest.mark.parametrize("blank", [" ", "\n", "\t", "   \n  \t"])
    def test_whitespace_only_returns_empty(self, blank):
        assert html_to_text(blank) == ""


# --- normal HTML content survives ---


class TestContentPreserved:
    def test_plain_text_inside_div_kept(self):
        text = html_to_text("<div>Hello, world</div>")
        assert "Hello, world" in text

    def test_amount_with_currency_symbol_kept(self):
        text = html_to_text("<p>Total: <b>¥1,198</b></p>")
        assert "¥1,198" in text

    def test_chinese_content_kept(self):
        text = html_to_text(
            "<p>合計: <strong>1198 円</strong></p>"
            "<p>店舗: スターバックス Olive LOUNGE 渋谷店</p>"
        )
        assert "合計" in text
        assert "1198 円" in text
        assert "スターバックス" in text
        assert "渋谷店" in text

    def test_table_structure_preserved(self):
        # Tables matter for receipts with line items.
        html = (
            "<table>"
            "<tr><th>Item</th><th>Price</th></tr>"
            "<tr><td>Kindle</td><td>¥17,980</td></tr>"
            "</table>"
        )
        text = html_to_text(html)
        assert "Kindle" in text
        assert "¥17,980" in text


# --- noise stripped ---


class TestNoiseStripped:
    def test_images_dropped(self):
        text = html_to_text(
            "<p>before</p>"
            "<img src='tracker.png' alt='hidden tracker'/>"
            "<p>after</p>"
        )
        # No alt text, no markdown image syntax.
        assert "tracker.png" not in text
        assert "hidden tracker" not in text
        assert "![" not in text  # markdown image opener
        assert "before" in text
        assert "after" in text

    def test_link_url_dropped_but_visible_text_kept(self):
        text = html_to_text(
            '<a href="https://example.com/unsubscribe?token=xxx">Unsubscribe</a>'
        )
        assert "https://example.com" not in text
        assert "unsubscribe?token=xxx" not in text
        # Visible anchor text is kept so meaningful link labels aren't lost.
        assert "Unsubscribe" in text

    def test_bold_and_italic_markers_stripped(self):
        text = html_to_text(
            "<p><b>Important</b> and <i>maybe</i> stuff</p>"
        )
        assert "Important" in text
        assert "maybe" in text
        # No markdown emphasis markers cluttering LLM context.
        assert "**" not in text
        assert "_Important_" not in text

    def test_long_lines_not_rewrapped(self):
        # body_width=0 means we keep the source's line structure
        # rather than rewrapping at the html2text default of 78 chars.
        long_sentence = (
            "This is a very long sentence that would normally be wrapped at "
            "78 characters by html2text but we disabled that behaviour."
        )
        text = html_to_text(f"<p>{long_sentence}</p>")
        # The whole sentence should still be on a single line.
        assert long_sentence in text


# --- defensive: malformed / surprising input ---


class TestRobustness:
    def test_malformed_html_does_not_raise(self):
        # html2text is liberal — this should not crash even with
        # unbalanced angle brackets.
        text = html_to_text("<p>start <unclosed and <<< noise")
        # Whatever comes out, it must be a string.
        assert isinstance(text, str)

    def test_html_entities_decoded(self):
        text = html_to_text("<p>Caf&eacute; &amp; Co.</p>")
        assert "Café" in text
        assert "&" in text
        assert "&amp;" not in text

    def test_script_and_style_blocks_removed(self):
        text = html_to_text(
            "<style>.x { color: red; }</style>"
            "<p>visible</p>"
            "<script>alert(1)</script>"
        )
        assert "color: red" not in text
        assert "alert(1)" not in text
        assert "visible" in text
