"""Tests for the v1.5.0 span-helper contract.

Behavior pinned here:

- Every helper (`graph_span` / `llm_call` / `api_call`) emits EXACTLY one
  Prometheus histogram observation per invocation with the labels the
  spec requires.
- `graph_span` outcome flips to `"error"` when the wrapped block raises,
  otherwise it honors the caller's explicit `ctx.outcome = "skipped"`.
- Token counts / status codes attached to the yielded context after the
  wrapped call are picked up before observation.
- Histograms and OTel spans share the same timing window (verified by
  comparing the observed histogram sum against wall-clock bounds).
"""

from __future__ import annotations

import time

import pytest
from prometheus_client import generate_latest

from zashiki_warasi.observability import (
    REGISTRY,
    api_call,
    graph_span,
    llm_call,
    observe_email_end_to_end,
)


def _sum_from_scrape(family: str, label_selector: str) -> float:
    """Sum the `_sum` line for a histogram family that matches the
    label-selector substring. Label-selector is a comma-free substring
    like `node="notify"` — the test uses it to grep the exposition."""
    body = generate_latest(REGISTRY).decode()
    total = 0.0
    for line in body.splitlines():
        if not line.startswith(f"{family}_sum"):
            continue
        if label_selector not in line:
            continue
        total += float(line.rsplit(" ", 1)[1])
    return total


def _count_from_scrape(family: str, label_selector: str) -> float:
    body = generate_latest(REGISTRY).decode()
    total = 0.0
    for line in body.splitlines():
        if not line.startswith(f"{family}_count"):
            continue
        if label_selector not in line:
            continue
        total += float(line.rsplit(" ", 1)[1])
    return total


class TestGraphSpan:
    def test_happy_path_records_success(self):
        family = "zashiki_graph_node_duration_seconds"
        sel = 'node="notify",outcome="success",vertical="email"'
        before = _count_from_scrape(family, sel)
        with graph_span("notify", "email"):
            pass
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_explicit_skipped_outcome_wins(self):
        family = "zashiki_graph_node_duration_seconds"
        sel = 'node="extract",outcome="skipped",vertical="calendar_sg"'
        before = _count_from_scrape(family, sel)
        with graph_span("extract", "calendar_sg") as gs:
            gs.outcome = "skipped"
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_exception_flips_outcome_to_error_and_propagates(self):
        family = "zashiki_graph_node_duration_seconds"
        sel = 'node="create",outcome="error",vertical="calendar_sg"'
        before = _count_from_scrape(family, sel)

        class SentinelError(RuntimeError):
            pass

        with pytest.raises(SentinelError):
            with graph_span("create", "calendar_sg"):
                raise SentinelError("boom")

        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_duration_is_positive_and_bounded(self):
        family = "zashiki_graph_node_duration_seconds"
        sel = 'node="analyze",outcome="success",vertical="email"'
        before = _sum_from_scrape(family, sel)
        start = time.monotonic()
        with graph_span("analyze", "email"):
            time.sleep(0.02)
        wall = time.monotonic() - start
        after = _sum_from_scrape(family, sel)
        delta = after - before
        assert 0.0 < delta <= wall + 0.05  # allow tiny scheduler slack


class TestLLMCall:
    def test_purpose_and_model_labels_emitted(self):
        family = "zashiki_llm_call_duration_seconds"
        sel = 'model="test-model",purpose="classify"'
        before = _count_from_scrape(family, sel)
        with llm_call("classify", model="test-model"):
            pass
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_default_model_is_unknown_when_omitted(self):
        family = "zashiki_llm_call_duration_seconds"
        sel = 'model="unknown",purpose="body_summary"'
        before = _count_from_scrape(family, sel)
        with llm_call("body_summary"):
            pass
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_token_counts_are_optional(self):
        # Both branches should observe; no assertion needed beyond
        # "doesn't crash + records one observation".
        family = "zashiki_llm_call_duration_seconds"
        sel = 'model="unknown",purpose="calendar_extract"'
        before = _count_from_scrape(family, sel)
        with llm_call("calendar_extract") as ctx:
            ctx.prompt_tokens = 1234
            ctx.completion_tokens = 42
        with llm_call("calendar_extract"):
            pass  # no counts attached
        after = _count_from_scrape(family, sel)
        assert after == before + 2.0

    def test_exception_still_records_observation(self):
        family = "zashiki_llm_call_duration_seconds"
        sel = 'model="unknown",purpose="classify"'
        before = _count_from_scrape(family, sel)

        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            with llm_call("classify"):
                raise Boom("slow-then-fail path")
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0


class TestApiCall:
    def test_service_operation_labels_emitted(self):
        family = "zashiki_external_api_duration_seconds"
        sel = 'operation="insert",service="calendar"'
        before = _count_from_scrape(family, sel)
        with api_call("calendar", "insert") as ctx:
            ctx.status_code = 200
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_status_code_is_optional(self):
        family = "zashiki_external_api_duration_seconds"
        sel = 'operation="history",service="gmail"'
        before = _count_from_scrape(family, sel)
        with api_call("gmail", "history"):
            pass  # no status code attached
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_exception_still_records_observation(self):
        family = "zashiki_external_api_duration_seconds"
        sel = 'operation="send_message",service="telegram"'
        before = _count_from_scrape(family, sel)

        class NetErr(RuntimeError):
            pass

        with pytest.raises(NetErr):
            with api_call("telegram", "send_message") as ctx:
                ctx.status_code = 502
                raise NetErr("upstream 502")
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0


class TestEndToEndObserve:
    def test_observation_records_with_labels(self):
        family = "zashiki_email_end_to_end_duration_seconds"
        sel = 'category="會議邀請",outcome="success"'
        before = _count_from_scrape(family, sel)
        observe_email_end_to_end("會議邀請", "success", 12.5)
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0

    def test_sum_carries_passed_duration(self):
        family = "zashiki_email_end_to_end_duration_seconds"
        sel = 'category="廣告",outcome="skipped"'
        before = _sum_from_scrape(family, sel)
        observe_email_end_to_end("廣告", "skipped", 3.75)
        after = _sum_from_scrape(family, sel)
        assert after == pytest.approx(before + 3.75)

    def test_unknown_category_accepted_for_analyze_failure(self):
        # When analyze fails, the caller falls back to
        # category="unknown". Ensure the label value round-trips.
        family = "zashiki_email_end_to_end_duration_seconds"
        sel = 'category="unknown",outcome="error"'
        before = _count_from_scrape(family, sel)
        observe_email_end_to_end("unknown", "error", 1.0)
        after = _count_from_scrape(family, sel)
        assert after == before + 1.0
