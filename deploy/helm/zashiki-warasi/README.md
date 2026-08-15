# zashiki-warasi Helm chart

Deploys the Zashiki-warasi FastAPI service on Kubernetes / k3s.
External Postgres (bring your own via `secrets.databaseUrl`),
external cron scheduling via a `CronJob` that `curl`s `POST /poll`.

## Prerequisites

- Kubernetes 1.24+ (tested on k3s)
- An existing Postgres reachable from the cluster
- OAuth client secrets JSON from Google Cloud Console

## Quickstart

```
helm install zashiki ./deploy/helm/zashiki-warasi \
    --namespace zashiki --create-namespace \
    --set secrets.databaseUrl='postgresql+psycopg://...' \
    --set secrets.httpApiKey="$(openssl rand -hex 32)" \
    --set-file oauth.credentialsJson=./credentials.json
```

See `docs/oauth-redirect-uri.md` for OAuth setup.

## Observability (v1.1, opt-in)

Three independent toggles under `observability.*` — all default OFF
so a plain `helm install` produces the same manifests as v1.0.

### ServiceMonitor

Enables kube-prometheus-stack (or any Prometheus Operator install)
to scrape the app's `/metrics`.

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi \
    --reuse-values \
    --set observability.serviceMonitor.enabled=true
```

**If your Prometheus CR's `serviceMonitorSelector` requires a
specific label** (kube-prometheus-stack usually needs
`release: kube-prometheus-stack`), add it:

```
    --set observability.serviceMonitor.additionalLabels.release=kube-prometheus-stack
```

The ServiceMonitor uses `relabelings` to stamp `job=<chart fullname>`
on every scraped series — so PrometheusRule alert queries below can
match on a predictable `job` label.

### PrometheusRule (starter alerts)

Ships 5 starter alerts as a `PrometheusRule` CRD. Each alert is
independently toggleable + threshold-tunable via values.

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi \
    --reuse-values \
    --set observability.prometheusRule.enabled=true
```

The 5 alerts:

| Alert | Severity | Default trip |
|---|---|---|
| `ZashikiTickNoSuccessfulTick` | warning | No successful tick in 15m |
| `ZashikiTickErrorRateHigh` | warning | Tick error rate > 0.5/min for 10m |
| `ZashikiTickConflictRateHigh` | info | 409 conflict rate > 5/min for 10m |
| `ZashikiHealthzUnhealthy` | critical | `zashiki_healthz_status=0` for 5m |
| `ZashikiOAuthRefreshFailing` | warning | Any refresh error in 15m |

Tune a single alert without editing PromQL:

```
    --set observability.prometheusRule.alerts.tickErrorRateHigh.ratePerMinute=0.1 \
    --set observability.prometheusRule.alerts.tickConflictRateHigh.enabled=false
```

Add custom alerts alongside starters (non-destructive):

```yaml
observability:
  prometheusRule:
    enabled: true
    additionalRules:
      - alert: MyExtraAlert
        expr: vector(1)
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: my custom check
```

### Alert severity → routing

This chart ships alert **definitions**, not delivery. Alertmanager
config is cluster-owned. Suggested routing to prevent day-one alert
fatigue:

- **`critical`** → immediate route (PagerDuty / on-call channel /
  phone). This severity means the operator MUST act now
  (`ZashikiHealthzUnhealthy` = app itself reporting broken).
- **`warning`** → batched digest (daily Slack summary, weekly email).
  These are recoverable anomalies the operator should be aware of but
  not woken up for (`ZashikiTickErrorRateHigh`,
  `ZashikiOAuthRefreshFailing`, `ZashikiTickNoSuccessfulTick`).
- **`info`** → log-only, no notification route
  (`ZashikiTickConflictRateHigh` is over-scheduling, not a bug).

Wire these into your Alertmanager `route.routes` matching on
`severity` label. Without a routing plan, all 5 alerts hit the
same channel and get muted by the operator on week two.

### Dashboards

Ships Grafana dashboards as `ConfigMap`s labeled `grafana_dashboard: "1"`
for the kube-prometheus-stack Grafana sidecar to auto-import.

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi \
    --reuse-values \
    --set observability.dashboards.enabled=true
```

Currently one dashboard bundled:

| Dashboard | Path in Grafana | Per-dashboard toggle |
|---|---|---|
| Zashiki-warasi overview | `Zashiki-warasi / Zashiki-warasi overview` | `observability.dashboards.overview.enabled` (default true) |

**Disabling a per-dashboard toggle removes the ConfigMap → the
sidecar auto-removes the panel from Grafana on next reconcile.**
No manual Grafana UI cleanup needed. Contrast with a hand-authored
dashboard which sticks around after the CM is deleted.

If your kube-prom-stack install uses a non-default sidecar label,
override:

```
    --set observability.dashboards.sidecarLabel.key=my_custom_label \
    --set observability.dashboards.sidecarLabel.value=on
```

### Turning it all on

```
helm upgrade zashiki ./deploy/helm/zashiki-warasi \
    --reuse-values \
    --set observability.serviceMonitor.enabled=true \
    --set observability.prometheusRule.enabled=true \
    --set observability.dashboards.enabled=true
```

### Enabling tracing (independent of the above)

Metrics work without OTel; tracing needs a collector. Point at your
in-cluster OTel Collector (deploy separately — the chart does not
ship a collector):

```
    --set env.OTEL_ENABLED=1 \
    --set env.OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc.cluster.local:4317
```

See `docs/tracing-architecture.md` for the span contract + full
config env reference.
