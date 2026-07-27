"""Prometheus metrics for the prediction service.

Every metric is exported under TWO names.  The reason is a real collision:
``goldpred_job_last_success_timestamp_seconds{job=...}`` was exported by this
service *and* by the Go API, with overlapping ``job`` label values (both sides
own a job called ``collect``/``predict`` in operators' heads).  A Prometheus
scraping both targets therefore held two unrelated series that differ only in
the target labels a rule usually aggregates away — ``max by (job) (...)`` let
one process's healthy timestamp mask the other's dead job.

So metric names are namespaced per service: ``talapala_prediction_*`` here,
``talapala_api_*`` in backend-go.  The old ``goldpred_*`` names keep being
written alongside them (see :class:`_Dual`) so existing dashboards, alerts and
docs/CONTRACTS.md consumers do not break on the day this lands.

The ``goldpred_*`` names are DEPRECATED and will be removed in a later
release; new dashboards and alert rules must use ``talapala_prediction_*``.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Sequence

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class _Dual:
    """Fans one write out to a metric and its deprecated twin.

    Wrapping the pair (rather than writing twice at every call site) is what
    keeps the two exports in lockstep: a call site cannot update one name and
    forget the other, and dropping the deprecated export later is a one-line
    change in this module instead of an audit of every job.

    Only the operations the call sites actually use are exposed — ``labels``,
    ``inc``, ``set``, ``observe`` and the ``time`` context manager.
    """

    __slots__ = ("_metrics",)

    def __init__(self, *metrics: object) -> None:
        self._metrics = metrics

    def labels(self, *args: object, **kwargs: object) -> "_Dual":
        return _Dual(*(m.labels(*args, **kwargs) for m in self._metrics))

    def inc(self, amount: float = 1) -> None:
        for m in self._metrics:
            m.inc(amount)

    def set(self, value: float) -> None:
        for m in self._metrics:
            m.set(value)

    def observe(self, value: float) -> None:
        for m in self._metrics:
            m.observe(value)

    @contextmanager
    def time(self) -> Iterator[None]:
        """``Histogram.time`` across both twins, measuring the block once."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(time.perf_counter() - started)


def _deprecated_help(replacement: str, documentation: str) -> str:
    return (
        f"{documentation} DEPRECATED name — use {replacement}; "
        "this series is removed in a later release."
    )


def _dual(
    factory: type,
    name: str,
    deprecated_name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
) -> _Dual:
    """Register ``name`` and its ``goldpred_*`` predecessor as one metric."""
    return _Dual(
        factory(name, documentation, labelnames),
        factory(deprecated_name, _deprecated_help(name, documentation), labelnames),
    )


COLLECT_SUCCESS = _dual(
    Counter,
    "talapala_prediction_collect_success_total",
    "goldpred_collect_success_total",
    "Datapoints successfully collected and stored",
    ["provider", "symbol"],
)
COLLECT_FAILURE = _dual(
    Counter,
    "talapala_prediction_collect_failure_total",
    "goldpred_collect_failure_total",
    "Provider fetch failures",
    ["provider"],
)
LAST_PRICE_TS = _dual(
    Gauge,
    "talapala_prediction_last_price_timestamp_seconds",
    "goldpred_last_price_timestamp_seconds",
    "observed_at of the most recent stored price, as unix seconds",
    ["symbol"],
)
# The namespace already says "prediction", so the new name spells out what is
# being timed (a full prediction pass) instead of repeating the word.
PREDICTION_DURATION = _dual(
    Histogram,
    "talapala_prediction_pass_duration_seconds",
    "goldpred_prediction_duration_seconds",
    "Wall time of a full prediction pass",
)
MODEL_SMAPE = _dual(
    Gauge,
    "talapala_prediction_model_smape",
    "goldpred_model_smape",
    "Walk-forward sMAPE of the last training run",
    ["horizon", "model"],
)
JOB_LAST_SUCCESS = _dual(
    Gauge,
    "talapala_prediction_job_last_success_timestamp_seconds",
    "goldpred_job_last_success_timestamp_seconds",
    "Unix time of the last successful job run",
    ["job"],
)


def render_metrics() -> tuple[bytes, str]:
    """Prometheus exposition payload + content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
