"""Regression pins for the Helm chart's replica-count contract.

The whole point of D17's multi-replica readiness is that operators
can bump `replicaCount` without a code change. Guard against a
future refactor that accidentally hardcodes 1 in the template.

Skipped when `helm` is not on PATH (CI without k8s tooling).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "zashiki-warasi"

HELM_MISSING_REASON = "helm CLI not on PATH — skipping chart render tests"


pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason=HELM_MISSING_REASON
)


def _render(**set_overrides) -> str:
    cmd = ["helm", "template", "zashiki", str(CHART_DIR)]
    for key, value in set_overrides.items():
        cmd += ["--set", f"{key}={value}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


class TestReplicaCount:
    def test_default_renders_1(self):
        out = _render()
        assert "  replicas: 1" in out

    def test_override_to_3_flows_through(self):
        out = _render(replicaCount=3)
        assert "  replicas: 3" in out
        # Sanity: only ONE Deployment spec — the CronJob's `spec:` uses
        # `schedule`, not `replicas`, so no false match.
        assert out.count("  replicas: 3") == 1


class TestRollingUpdateStrategy:
    def test_strategy_is_rolling_update(self):
        out = _render()
        assert "    type: RollingUpdate" in out
        assert "type: Recreate" not in out

    def test_max_surge_and_max_unavailable(self):
        out = _render()
        assert "maxSurge: 1" in out
        assert "maxUnavailable: 0" in out


class TestExpectedKinds:
    """The chart must render all the manifests the deploy README
    promises. Prevents accidentally deleting a template."""

    def test_all_kinds_present(self):
        out = _render()
        for kind in (
            "kind: Deployment",
            "kind: Service",
            "kind: ConfigMap",
            "kind: Secret",
            "kind: CronJob",
        ):
            assert kind in out, f"missing manifest: {kind}"

    def test_ingress_off_by_default(self):
        out = _render()
        assert "kind: Ingress" not in out

    def test_ingress_on_when_enabled(self):
        out = _render(**{"ingress.enabled": "true"})
        assert "kind: Ingress" in out


# --- v1.1 observability manifests (Group 7) -----------------------
#
# Three independent flags:
#   observability.serviceMonitor.enabled
#   observability.prometheusRule.enabled
#   observability.dashboards.enabled
#
# Table-driven over flag combinations + spot-checks on the shape
# of the rendered manifests (per-alert toggle, threshold override,
# additionalRules append, per-dashboard toggle).


class TestObservabilityAllOff:
    """Default (all three flags false) renders NONE of the
    observability manifests."""

    def test_no_service_monitor(self):
        out = _render()
        assert "kind: ServiceMonitor" not in out

    def test_no_prometheus_rule(self):
        out = _render()
        assert "kind: PrometheusRule" not in out

    def test_no_dashboard_configmap(self):
        out = _render()
        # Existing app ConfigMap is fine — filter by the grafana
        # sidecar label to isolate dashboard CMs.
        assert "grafana_dashboard" not in out


class TestServiceMonitorRender:
    def test_renders_when_enabled(self):
        out = _render(**{"observability.serviceMonitor.enabled": "true"})
        assert "kind: ServiceMonitor" in out
        assert out.count("kind: ServiceMonitor") == 1

    def test_selector_matches_service_labels(self):
        """The SM's spec.selector.matchLabels must match the labels
        the chart's Service carries — else the SM finds no target
        and Prometheus scrapes nothing."""
        out = _render(**{"observability.serviceMonitor.enabled": "true"})
        # Both Service and ServiceMonitor use `zashiki-warasi.selectorLabels`
        # so they share `app.kubernetes.io/name` and `.../instance`.
        assert "app.kubernetes.io/name: zashiki-warasi" in out
        assert "app.kubernetes.io/instance: zashiki" in out

    def test_job_relabeling_present(self):
        """SM must set `job` label to a stable value (chart fullname)
        so PrometheusRule alert exprs can match on `job=` predictably.
        Without this, Prometheus Operator's default job label is
        `{namespace}/{sm-name}` which is awkward for regex matching."""
        out = _render(**{"observability.serviceMonitor.enabled": "true"})
        assert "relabelings:" in out
        assert "targetLabel: job" in out
        # Chart fullname: release name `zashiki` + chart name
        # `zashiki-warasi` → `zashiki-zashiki-warasi` (release name
        # does not contain chart name, so the else-branch in
        # _helpers.tpl `fullname` fires).
        assert "replacement: zashiki-zashiki-warasi" in out


class TestPrometheusRuleRender:
    _ALL_ALERTS = (
        "ZashikiTickNoSuccessfulTick",
        "ZashikiTickErrorRateHigh",
        "ZashikiTickConflictRateHigh",
        "ZashikiHealthzUnhealthy",
        "ZashikiOAuthRefreshFailing",
    )

    def test_renders_when_enabled(self):
        out = _render(**{"observability.prometheusRule.enabled": "true"})
        assert "kind: PrometheusRule" in out

    def test_all_five_starter_alerts_present(self):
        out = _render(**{"observability.prometheusRule.enabled": "true"})
        for alert in self._ALL_ALERTS:
            assert alert in out, f"missing starter alert: {alert}"

    def test_per_alert_disable_removes_only_that_rule(self):
        out = _render(**{
            "observability.prometheusRule.enabled": "true",
            "observability.prometheusRule.alerts.tickConflictRateHigh.enabled": "false",
        })
        assert "ZashikiTickConflictRateHigh" not in out
        # Other four alerts still present.
        for alert in self._ALL_ALERTS:
            if alert == "ZashikiTickConflictRateHigh":
                continue
            assert alert in out, f"peer alert {alert} should not be removed"

    def test_threshold_override_flows_into_expr(self):
        out = _render(**{
            "observability.prometheusRule.enabled": "true",
            "observability.prometheusRule.alerts.tickErrorRateHigh.ratePerMinute": "0.1",
        })
        # The expr contains `> 0.1`, not `> 0.5` (default).
        assert "> 0.1" in out
        assert "> 0.5" not in out

    def test_additional_rules_appended_alongside_starters(self):
        out = _render(**{
            "observability.prometheusRule.enabled": "true",
            "observability.prometheusRule.additionalRules[0].alert": "MyCustomAlert",
            "observability.prometheusRule.additionalRules[0].expr": "vector(1)",
            "observability.prometheusRule.additionalRules[0].for": "1m",
            "observability.prometheusRule.additionalRules[0].labels.severity": "warning",
            "observability.prometheusRule.additionalRules[0].annotations.summary": "test",
        })
        # Starter alerts still there
        assert "ZashikiTickNoSuccessfulTick" in out
        # Custom appended
        assert "MyCustomAlert" in out

    def test_job_label_matches_servicemonitor_relabeling(self):
        """Alert exprs filter on `job="<fullname>"` — must match what
        the ServiceMonitor stamps via relabelings. Both are derived
        from `zashiki-warasi.fullname` so they stay in sync."""
        out = _render(**{"observability.prometheusRule.enabled": "true"})
        assert 'job="zashiki-zashiki-warasi"' in out


class TestDashboardConfigMapRender:
    def test_renders_when_master_and_per_dashboard_enabled(self):
        out = _render(**{
            "observability.dashboards.enabled": "true",
        })
        # overview.enabled defaults to true — should get one dashboard CM.
        assert "grafana_dashboard" in out
        assert "zashiki-warasi-overview.json" in out

    def test_absent_when_master_flag_off(self):
        out = _render(**{
            "observability.dashboards.enabled": "false",
            # per-dashboard enabled=true is overridden by master off
            "observability.dashboards.overview.enabled": "true",
        })
        assert "grafana_dashboard" not in out

    def test_per_dashboard_toggle_removes_cm(self):
        out = _render(**{
            "observability.dashboards.enabled": "true",
            "observability.dashboards.overview.enabled": "false",
        })
        # Master on, per-dashboard off → no overview CM.
        assert "grafana_dashboard" not in out
        assert "zashiki-warasi-overview.json" not in out

    def test_sidecar_label_customizable(self):
        out = _render(**{
            "observability.dashboards.enabled": "true",
            "observability.dashboards.sidecarLabel.key": "my_custom_dashboard_label",
            "observability.dashboards.sidecarLabel.value": "on",
        })
        assert "my_custom_dashboard_label" in out
        # default `grafana_dashboard` should NOT appear as a label
        # (may still appear in the dashboard's own JSON — check just
        # the ConfigMap metadata region by looking for the : "on"
        # assignment on our custom key)
        assert 'my_custom_dashboard_label: "on"' in out


class TestObservabilityAllOn:
    def test_all_three_manifests_appear_together(self):
        out = _render(**{
            "observability.serviceMonitor.enabled": "true",
            "observability.prometheusRule.enabled": "true",
            "observability.dashboards.enabled": "true",
        })
        assert "kind: ServiceMonitor" in out
        assert "kind: PrometheusRule" in out
        assert "grafana_dashboard" in out
        # Sanity: still exactly one of each observability manifest.
        assert out.count("kind: ServiceMonitor") == 1
        assert out.count("kind: PrometheusRule") == 1
