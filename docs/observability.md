# Observability

Both services have always exported Prometheus metrics; nothing scraped them.
This document covers the optional `observability` compose profile that closes
that gap, and the metric-name migration that had to happen first.

## The name collision that forced a rename

`goldpred_job_last_success_timestamp_seconds{job=...}` was exported by **both**
services, with overlapping `job` label values — the Go scheduler owns
`collect`/`predict`/`train`/`cleanup`, the Python service owns
`collect`/`features`/`evaluate`/`news`/`cleanup`/`signals`/`backfill`. Scraping
both targets produced two unrelated series distinguishable only by the target
labels a rule normally aggregates away, so `max by (job) (...)` let one
process's healthy timestamp mask the other's dead job.

Metrics are now namespaced per service. Every metric is exported under **two**
names: the new one and the old one. The `goldpred_*` names are **deprecated**
and will be removed in a later release — do not use them in new dashboards or
rules.

| Deprecated name | Current name |
| --- | --- |
| `goldpred_http_request_duration_seconds` | `talapala_api_http_request_duration_seconds` |
| `goldpred_http_requests_total` | `talapala_api_http_requests_total` |
| `goldpred_job_last_success_timestamp_seconds` *(Go)* | `talapala_api_job_last_success_timestamp_seconds` |
| `goldpred_job_failure_total` | `talapala_api_job_failure_total` |
| `goldpred_job_duration_seconds` | `talapala_api_job_duration_seconds` |
| `goldpred_api_last_price_timestamp_seconds` | `talapala_api_last_price_timestamp_seconds` |
| `goldpred_api_last_prediction_timestamp_seconds` | `talapala_api_last_prediction_timestamp_seconds` |
| `goldpred_collect_success_total` | `talapala_prediction_collect_success_total` |
| `goldpred_collect_failure_total` | `talapala_prediction_collect_failure_total` |
| `goldpred_last_price_timestamp_seconds` | `talapala_prediction_last_price_timestamp_seconds` |
| `goldpred_prediction_duration_seconds` | `talapala_prediction_pass_duration_seconds` |
| `goldpred_model_smape` | `talapala_prediction_model_smape` |
| `goldpred_job_last_success_timestamp_seconds` *(Python)* | `talapala_prediction_job_last_success_timestamp_seconds` |

The dual write lives in `backend-go/internal/obs/obs.go` (the `Dual*` wrappers)
and `prediction-python/app/metrics.py` (the `_Dual` class). Call sites are
unchanged and write once; the wrapper fans out, so the two exports cannot drift
apart and removing the deprecated half later is a one-line change per service.

`docs/CONTRACTS.md` still lists the old Python names — it should be updated to
the current ones when the deprecated exports are dropped.

## Which freshness metric to use

Two families look interchangeable and are not:

* `talapala_api_last_price_timestamp_seconds{symbol}` is re-read from Postgres
  (`max(observed_at)`) by the Go freshness job every 5 minutes. It survives a
  restart and describes what is actually **stored**. Alert on this one.
* `talapala_prediction_last_price_timestamp_seconds{symbol}` is set in-process
  by a successful collect pass. It disappears on restart until the next
  success, which makes it useless for staleness but useful for "did this
  process collect anything".

The same asymmetry applies to the job gauges, which is why the alert rules pair
every "job stopped succeeding" (gauge staleness) with a "job is failing"
(counter increase) rule: after a restart, a job that never succeeds again never
publishes a gauge, and only the failure counter can see it.

## Starting the stack

The profile is opt-in. Plain `docker compose up -d` starts exactly the five
production services and nothing else:

```bash
docker compose --profile observability up -d prometheus   # start
docker compose --profile observability ps                 # status
docker compose --profile observability stop prometheus    # stop, keep data
docker compose --profile observability down               # stop; promdata volume survives
```

Prometheus stores its TSDB in the `promdata` volume with
`--storage.tsdb.retention.time=90d` and `--storage.tsdb.retention.size=2GB`.
The size cap is the one that matters: this host also carries Postgres and its
backups, so the TSDB must never be what fills the disk.

## Reaching the UI (no published port)

The service deliberately publishes **no** port — Prometheus has no
authentication, and a single-host deployment should not grow an open admin
surface. Reach it through an SSH tunnel, publishing on the host loopback only
for as long as you need it:

```bash
# on the server
cat > /tmp/prom-port.yml <<'YML'
services:
  prometheus:
    ports: ["127.0.0.1:9090:9090"]
YML
docker compose -f docker-compose.yml -f /tmp/prom-port.yml \
  --profile observability up -d prometheus

# from your workstation
ssh -N -L 9090:127.0.0.1:9090 ubuntu@<server>   # then open http://localhost:9090
```

Re-running `docker compose --profile observability up -d prometheus` without
the override file removes the published port again.

For a quick answer without a browser, query from inside the container:

```bash
docker compose --profile observability exec prometheus \
  wget -qO- 'http://127.0.0.1:9090/api/v1/alerts'
docker compose --profile observability exec prometheus \
  wget -qO- 'http://127.0.0.1:9090/api/v1/query?query=up'
```

## Scrape targets

| Job | Target | Path |
| --- | --- | --- |
| `api` | `api:8080` | `/metrics` |
| `prediction-service` | `prediction-service:8500` | `/internal/metrics` |
| `prometheus` | `127.0.0.1:9090` | `/metrics` |

Neither application path needs a credential: `/metrics` is mounted before the
auth middleware, and `/internal/metrics` is one of the two token-exempt paths.
That is safe only because neither service publishes a port.

Both application scrape configs set `honor_labels: true`. This is required, not
cosmetic: both services label scheduler metrics with `job` (`job="collect"`,
`job="train"`, …), which is also the target label Prometheus attaches from
`job_name`. With the default the application's value is silently renamed to
`exported_job` and every rule matching `job="collect"` matches nothing. The
scrape target stays identifiable through `instance` and the explicit `service`
label in `observability/prometheus.yml`.

## Alert rules

`observability/alerts.yml`. Every rule carries a `for:`, a `severity` label and
a description saying what to check.

| Alert | Severity | Fires when |
| --- | --- | --- |
| `IranGold18kPriceStale` | critical | no IR_GOLD_18K price for 45 min on a Tehran trading day |
| `UsdIrtPriceStale` | critical | no USD_IRT price for 45 min inside the Tehran session |
| `GlobalGoldPriceStale` | warning | no XAUUSD price for 3 h on a weekday |
| `MetricsTargetDown` | critical | a service stops being scrapeable for 5 min |
| `ScheduledJobFailing` | warning | collect/predict/train/cleanup failed in the last 30 min |
| `CollectJobNotSucceeding` | critical | no successful collect for 45 min |
| `PredictJobNotSucceeding` | critical | no successful predict for 3 h |
| `TrainJobNotSucceeding` | warning | no successful train for 30 h |
| `CleanupJobNotSucceeding` | warning | no successful cleanup for 30 h |
| `DatabaseBackupStale` | critical | no successful backup for 30 h |
| `ActiveModelArtifactMissing` | critical | an active model version has no loadable artifact |
| `PredictionIntervalCoverageDegraded` | warning | live coverage of the 90% interval below 0.75 for 2 h |
| `NewsCollectionFailing` | warning | no successful news ingest for 6 h |
| `NewsSourceStale` | warning | one approved news source silent for 24 h |

Market calendars are encoded in the expressions rather than left to the
operator: Tehran trades Sat–Wed with USD_IRT confined to the session window,
and global metals close Friday evening UTC. Without those guards every
freshness rule would fire each weekend and be muted within a month.
`day_of_week()`/`hour()` are evaluated against `time() + 12600` (UTC+3:30)
wherever a Tehran-local calendar is needed.

`GlobalGoldPriceStale`'s 3-hour threshold is load-bearing: it is what stops the
Friday 21:00 UTC close from tripping the rule before the weekend suppression
takes over. Do not lower it without adding a proper session guard.

### Pending instrumentation

Four rules reference metrics that nothing writes yet. They evaluate to an empty
vector and stay **silent** — chosen deliberately over an `absent()` formulation,
which would fire continuously and train the operator to ignore the file. Each
becomes live the moment its producer exists:

| Metric | Producer that needs to write it |
| --- | --- |
| `talapala_backup_last_success_timestamp_seconds` | `scripts/backup.sh` — it runs as a host cron outside the compose network, so it needs a node_exporter textfile collector or a Pushgateway |
| `talapala_prediction_active_model_artifact{symbol,horizon}` | prediction service — 1 when the active `model_versions` row has a readable artifact under `MODELS_DIR`, 0 when it does not |
| `talapala_prediction_interval_coverage{horizon}` | `app/jobs/evaluate.py` — it already computes this into `app_settings['live_calibration']`; the gauge only has to mirror it |
| `talapala_prediction_news_source_last_success_timestamp_seconds{source}` | `app/jobs/news.py` — it already updates `news_sources.last_success_at` per source code |

## No Alertmanager yet

`observability/prometheus.yml` has no `alerting:` block because no Alertmanager
is deployed. Rules evaluate and are visible under `/alerts` and via
`/api/v1/alerts`, but nothing routes them to a human. Adding an Alertmanager
service to the same profile and one `alerting:` block is the only change needed
to turn these into pages.

## Editing the configuration

Both files are bind-mounted read-only, so an edit on the host needs a reload
rather than a rebuild (`--web.enable-lifecycle` is enabled):

```bash
docker compose --profile observability exec prometheus \
  wget -qO- --post-data='' http://127.0.0.1:9090/-/reload
```

Validate before reloading — a bad rule file makes Prometheus refuse to start:

```bash
docker run --rm -v "$PWD/observability:/o" prom/prometheus:v3.5.0 \
  promtool check rules /o/alerts.yml
```
