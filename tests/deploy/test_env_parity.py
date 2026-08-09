"""Guard against env-var drift between compose .env.example and Helm
values.yaml. See scripts/check_env_parity.py for the source of truth."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_env_parity.py"


def test_env_parity_script_passes():
    """Running the check script exits 0 on the current repo state.
    Any divergence between compose .env.example and Helm values.yaml
    must be either fixed or explicitly whitelisted in
    scripts/check_env_parity.py."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"env parity check failed:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_check_env_parity_detects_divergence(monkeypatch, tmp_path):
    """Import-and-call the diff() helper against a controlled repo
    layout to prove the check actually catches drift (not just always
    returns OK)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_env_parity as module
    finally:
        sys.path.pop(0)

    fake_env = tmp_path / ".env.example"
    fake_env.write_text("DATABASE_URL=x\nEXTRA_IN_COMPOSE=y\n")
    fake_values = tmp_path / "values.yaml"
    fake_values.write_text(
        "env:\n  DATABASE_URL: x\n  EXTRA_IN_HELM: y\nsecrets: {}\n"
    )

    monkeypatch.setattr(module, "ENV_EXAMPLE", fake_env)
    monkeypatch.setattr(module, "HELM_VALUES", fake_values)

    missing_in_helm, missing_in_compose = module.diff()
    assert "EXTRA_IN_COMPOSE" in missing_in_helm
    assert "EXTRA_IN_HELM" in missing_in_compose
