"""Fail if deploy/compose/.env.example and deploy/helm/.../values.yaml
declare a divergent set of env vars.

Run standalone:

    uv run python scripts/check_env_parity.py

Called by `tests/deploy/test_env_parity.py` too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / "deploy" / "compose" / ".env.example"
HELM_VALUES = REPO_ROOT / "deploy" / "helm" / "zashiki-warasi" / "values.yaml"

# Compose-only knobs: they configure host-side behavior (bind mounts,
# host paths) and have no equivalent in the Helm chart, where the
# equivalent lives in `persistence.*` / secret data. Whitelisted so
# the check doesn't fail on legitimate compose-only concerns.
COMPOSE_ONLY: frozenset[str] = frozenset(
    {
        # Host-side path knobs used by docker-compose.yml volume binds.
        "GMAIL_CREDENTIALS_HOST_PATH",
        "DATA_DIR",
        # v1.1 observability-profile knobs. Prometheus retention drives
        # a `command:` flag on the compose Prometheus service; Helm
        # deploys don't ship Prometheus (kube-prometheus-stack owns
        # retention via its own `prometheus.prometheusSpec.retention`).
        # Grafana admin password is likewise only relevant when the
        # compose profile bundles Grafana; k3s uses kube-prom-stack's
        # existing Grafana with its own auth story.
        "PROMETHEUS_RETENTION_TIME",
        "GRAFANA_ADMIN_PASSWORD",
        # v1.2 add-log-aggregation-loki: Loki retention drives compose
        # loki service via `-config.expand-env=true` + config-file
        # substitution. Helm deploys don't ship Loki (operator installs
        # `grafana/loki` chart separately and owns retention via the
        # chart's `limits_config.retention_period` values override).
        "LOKI_RETENTION_PERIOD",
    }
)

# Helm-side we can't put in .env.example without inventing a "Helm
# only" section. Add here if the chart grows knobs that don't map to
# a container env var.
HELM_ONLY: frozenset[str] = frozenset(set())


# ---------- parsers ----------

_ENV_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def parse_env_example(path: Path) -> set[str]:
    """Extract every KEY=... line — commented (`# KEY=`) or not."""
    text = path.read_text()
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            # Look for `# KEY=value` — treat commented-out defaults as
            # documented env vars (they'll be uncommented by operators
            # who need them).
            m = _ENV_LINE.match(stripped.lstrip("#").lstrip())
        else:
            m = _ENV_LINE.match(line)
        if m:
            keys.add(m.group(1))
    return keys


# Matches an uncommented OR commented UPPER_SNAKE key inside a YAML
# map, e.g. `  KEY: "value"` or `  # KEY: "value"`. Mirrors how
# .env.example accounts for `# KEY=` documentation lines.
_HELM_ENV_KEY = re.compile(
    r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)\s*:", re.MULTILINE
)


def parse_helm_values(path: Path) -> set[str]:
    """Union of:
      - keys under `env:` (rendered into ConfigMap when uncommented),
        INCLUDING commented documentation entries under the same block,
      - env var names implied by the `secrets:` section (mapped by
        the secret template).
    Commented keys count as documented — parity is about "is the
    knob visible?" not "is it currently set?".
    """
    data = yaml.safe_load(path.read_text())
    keys: set[str] = set()

    env_block = data.get("env") or {}
    keys.update(env_block.keys())

    # Line-scan the `env:` block for commented keys the YAML parser
    # ignored. Bounded to lines inside the block by simple indentation.
    text = path.read_text()
    in_env_block = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("env:"):
            in_env_block = True
            continue
        if in_env_block:
            # A new top-level key ends the env block.
            if stripped and not stripped[0].isspace() and not stripped.startswith("#"):
                in_env_block = False
                continue
            m = _HELM_ENV_KEY.match(line)
            if m:
                keys.add(m.group(1))

    # `secrets:` maps camelCase Helm keys → UPPER_SNAKE env names. The
    # template renders exactly these names into the Secret.
    secrets_map = {
        "databaseUrl": "DATABASE_URL",
        "httpApiKey": "HTTP_API_KEY",
        "telegramBotToken": "TELEGRAM_BOT_TOKEN",
        "telegramChatId": "TELEGRAM_CHAT_ID",
        "notionToken": "NOTION_TOKEN",
        "notionExpenseDatabaseId": "NOTION_EXPENSE_DATABASE_ID",
    }
    for camel_key, env_key in secrets_map.items():
        if camel_key in (data.get("secrets") or {}):
            keys.add(env_key)

    return keys


# ---------- diff ----------


def diff() -> tuple[set[str], set[str]]:
    compose_keys = parse_env_example(ENV_EXAMPLE) - COMPOSE_ONLY
    helm_keys = parse_helm_values(HELM_VALUES) - HELM_ONLY
    missing_in_helm = compose_keys - helm_keys
    missing_in_compose = helm_keys - compose_keys
    return missing_in_helm, missing_in_compose


def main() -> int:
    missing_in_helm, missing_in_compose = diff()
    if not missing_in_helm and not missing_in_compose:
        print("env parity OK")
        return 0
    if missing_in_helm:
        print(
            "env parity FAIL: keys in .env.example but NOT in values.yaml:",
            file=sys.stderr,
        )
        for key in sorted(missing_in_helm):
            print(f"  - {key}", file=sys.stderr)
    if missing_in_compose:
        print(
            "env parity FAIL: keys in values.yaml but NOT in .env.example:",
            file=sys.stderr,
        )
        for key in sorted(missing_in_compose):
            print(f"  - {key}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
