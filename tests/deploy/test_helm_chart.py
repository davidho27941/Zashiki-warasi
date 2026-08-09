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
