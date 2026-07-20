"""Prometheus metrics.  Names are fixed by docs/CONTRACTS.md — do not rename."""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

COLLECT_SUCCESS = Counter(
    "goldpred_collect_success_total",
    "Datapoints successfully collected and stored",
    ["provider", "symbol"],
)
COLLECT_FAILURE = Counter(
    "goldpred_collect_failure_total",
    "Provider fetch failures",
    ["provider"],
)
LAST_PRICE_TS = Gauge(
    "goldpred_last_price_timestamp_seconds",
    "observed_at of the most recent stored price, as unix seconds",
    ["symbol"],
)
PREDICTION_DURATION = Histogram(
    "goldpred_prediction_duration_seconds",
    "Wall time of a full prediction pass",
)
MODEL_SMAPE = Gauge(
    "goldpred_model_smape",
    "Walk-forward sMAPE of the last training run",
    ["horizon", "model"],
)
JOB_LAST_SUCCESS = Gauge(
    "goldpred_job_last_success_timestamp_seconds",
    "Unix time of the last successful job run",
    ["job"],
)


def render_metrics() -> tuple[bytes, str]:
    """Prometheus exposition payload + content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
