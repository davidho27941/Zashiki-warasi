"""Tests for the Prometheus metrics registry contract + label guards."""

from __future__ import annotations

import platform

import pytest
from prometheus_client import generate_latest

from zashiki_warasi.observability import (
    REGISTRY,
    gmail_api_calls_total,
    healthz_status,
    llm_calls_total,
    oauth_refresh_total,
    oauth_token_expires_in_seconds,
    telegram_send_total,
    tick_conflicts_total,
    tick_duration_seconds,
    tick_messages_processed_total,
    tick_rebaseline_total,
    traces_dropped_total,
)
from zashiki_warasi.observability.metrics import (
    _assert_no_forbidden_labels,
    all_metric_names,
)


class TestContract:
    """Pin the metric-family contract from
    openspec/specs/observability/spec.md so accidental renames break
    a test rather than silently break every dashboard downstream."""

    _REQUIRED_ZASHIKI_FAMILIES: set[str] = {
        "zashiki_tick_duration_seconds",
        "zashiki_tick_messages_processed",
        "zashiki_tick_conflicts",
        "zashiki_tick_rebaseline",
        "zashiki_gmail_api_calls",
        "zashiki_gmail_api_latency_seconds",
        "zashiki_llm_calls",
        "zashiki_llm_latency_seconds",
        "zashiki_telegram_send",
        "zashiki_oauth_refresh",
        "zashiki_oauth_token_expires_in_seconds",
        "zashiki_healthz_status",
        "zashiki_traces_dropped",
    }

    def test_all_contracted_zashiki_families_present(self):
        names = set(all_metric_names())
        missing = self._REQUIRED_ZASHIKI_FAMILIES - names
        assert not missing, f"missing metric families: {sorted(missing)}"

    def test_no_generic_http_requests_metric(self):
        # Per design D16 there is deliberately NO generic
        # zashiki_http_requests_total counter. This test pins the
        # decision — an accidental add-back trips it.
        names = set(all_metric_names())
        assert "zashiki_http_requests" not in names
        assert "zashiki_http_requests_total" not in names

    def test_python_gc_and_info_present(self):
        # GCCollector + PlatformCollector run on every OS.
        names = set(all_metric_names())
        assert "python_gc_objects_collected" in names
        assert "python_info" in names

    @pytest.mark.skipif(
        platform.system() != "Linux",
        reason="ProcessCollector reads /proc which is Linux-only",
    )
    def test_process_family_present_on_linux(self):
        names = set(all_metric_names())
        # process_cpu_seconds_total shows up under family name
        # 'process_cpu_seconds' — prometheus_client strips the _total
        # suffix from the family name.
        assert "process_cpu_seconds" in names


class TestForbiddenLabelGuard:
    """The guard fires at declaration time (module import), so the
    happy path is asserted implicitly by every other test in this
    file succeeding. Here we exercise the failure path directly."""

    @pytest.mark.parametrize(
        "labelname",
        ["message_id", "email", "email_address", "user_id"],
    )
    def test_declaration_with_forbidden_label_raises(self, labelname):
        with pytest.raises(ValueError) as exc_info:
            _assert_no_forbidden_labels(
                "zashiki_bogus_family", (labelname,)
            )
        assert labelname in str(exc_info.value)
        assert "zashiki_bogus_family" in str(exc_info.value)

    def test_declaration_with_only_allowed_labels_passes(self):
        # Explicit allow-list of labels we DO use in the contract.
        _assert_no_forbidden_labels(
            "zashiki_ok",
            ("operation", "outcome", "node", "reason"),
        )


class TestMetricEmission:
    """Sanity: instantiating + operating each metric doesn't blow up
    and produces output visible in the /metrics text exposition."""

    def test_tick_conflicts_total_increments_and_renders(self):
        before = _count_from_scrape("zashiki_tick_conflicts_total")
        tick_conflicts_total.inc()
        after = _count_from_scrape("zashiki_tick_conflicts_total")
        assert after == before + 1.0

    def test_healthz_status_gauge_set(self):
        healthz_status.set(1)
        assert _count_from_scrape("zashiki_healthz_status") == 1.0
        healthz_status.set(0)
        assert _count_from_scrape("zashiki_healthz_status") == 0.0

    def test_gmail_api_calls_total_labels_multiplex(self):
        gmail_api_calls_total.labels(
            operation="history", outcome="success"
        ).inc()
        gmail_api_calls_total.labels(
            operation="history", outcome="error"
        ).inc()
        body = generate_latest(REGISTRY).decode()
        # Both label combinations appear as distinct series.
        assert 'operation="history"' in body
        assert 'outcome="success"' in body
        assert 'outcome="error"' in body

    def test_traces_dropped_total_reason_labels(self):
        # reason label enumerated in the spec (queue_full / export_failed / shutdown)
        traces_dropped_total.labels(reason="queue_full").inc()
        traces_dropped_total.labels(reason="export_failed").inc(3)
        body = generate_latest(REGISTRY).decode()
        assert 'reason="queue_full"' in body
        assert 'reason="export_failed"' in body

    def test_oauth_token_expires_in_seconds_accepts_negative(self):
        # Negative is a legitimate operational state — token has
        # expired but refresh has not yet run. The gauge accepts it.
        oauth_token_expires_in_seconds.set(-30.5)
        assert (
            _count_from_scrape("zashiki_oauth_token_expires_in_seconds")
            == -30.5
        )

    def test_all_metric_families_can_be_incremented(self):
        # A cheap belt-and-braces: touch every family so nothing was
        # accidentally declared with a broken labelset that only
        # errors on first use.
        tick_duration_seconds.labels(outcome="success").observe(0.1)
        tick_messages_processed_total.inc()
        tick_rebaseline_total.inc()
        llm_calls_total.labels(node="analyze", outcome="success").inc()
        telegram_send_total.labels(outcome="success").inc()
        oauth_refresh_total.labels(outcome="success").inc()
        # If we got here, every family accepted its labelset.


# --- helpers ---


def _count_from_scrape(family_prefix: str) -> float:
    """Grep the /metrics text output for the first non-comment line
    whose metric name starts with `family_prefix` and return the
    parsed float value. For counters, `family_prefix` is typically
    the fully-qualified `<name>_total` (or the base gauge/histogram
    name); label suffixes are ignored."""
    body = generate_latest(REGISTRY).decode()
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        head = line.split()
        if not head:
            continue
        name_and_labels = head[0]
        # Split off `{labels}` suffix if present.
        bare_name = name_and_labels.split("{", 1)[0]
        if bare_name == family_prefix:
            return float(head[1])
    raise AssertionError(
        f"metric {family_prefix!r} not found in scrape body"
    )
