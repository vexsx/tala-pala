"""Training orchestration: walk-forward validation, model selection, persistence.

Rules (docs/CONTRACTS.md):

* horizons ``1h``/``4h`` use the hourly series and are enabled only with
  >= 14 days of hourly coverage; ``eod``/``1d``/``3d``/``7d``/``30d`` use the
  daily series and need >= 120 daily points;
* walk-forward = expanding window, minimum 60 training points, folds strictly
  forward in time, never shuffled;
* a candidate is activated ONLY if it beats the naive baseline's sMAPE on the
  same folds — otherwise naive wins.
"""
from __future__ import annotations

import functools
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import model_versions, prices, training_runs, utcnow
from ..features.engineering import (
    FRAME_SPACING_KEY,
    MIN_HISTORY_KEY,
    SPACING_DAILY,
    SPACING_INTRADAY,
    daily_close,
    hourly_close,
)
from ..metrics import MODEL_SMAPE
from .analogue import KNNAnalogueModel  # noqa: F401  (registers 'knn_analogue')
from .arima import ARIMAModel  # noqa: F401  (registers 'arima')
from .base import ForecastModel, ModelUnavailable, make
from .baselines import NaiveModel  # noqa: F401  (registers baselines)
from .classical import ThetaForecastModel  # noqa: F401  (registers 'theta', 'holt_damped')
from .ensemble import EnsembleModel, combine, inverse_smape_weights
from .intervals import relative_residuals, walk_forward_coverage
from .ml import EXOG_KEYS, PREBUILT_FRAME_KEY, _feature_matrix
from .ml import TabularModel  # noqa: F401  (registers ml models)
from .sarimax_exog import SarimaxExogModel  # noqa: F401  (registers 'sarimax_exog')
from .tvinspired import LorentzianKNNModel  # noqa: F401  (registers 'lorentzian_knn', 'kalman_llt')

log = logging.getLogger(__name__)

MIN_TRAIN_POINTS = 60
MIN_DAILY_POINTS = 120
MIN_HOURLY_DAYS = 14
MAX_FOLDS = 40

# Ceiling on the worker processes an interactive walk-forward may spread its
# folds over (see :func:`_worker_count`). The nightly run stays sequential:
# it is a background job with no one waiting on it, and it already has both
# symbols and seven horizons to fill a machine with.
#
# This is the SPEED ceiling — the point past which another worker buys nothing
# (measured below) — and it is no longer the binding one. Memory is: each
# worker is a whole interpreter, so the count has to be answered against the
# memory this process is allowed to use, not only against the core count.
MAX_PARALLEL_FOLD_WORKERS = 8

# Resident set of ONE loky fold worker: a fresh interpreter with numpy, pandas,
# scikit-learn and statsmodels imported plus a copy of the series and feature
# frame. Measured on the production shape (1224 daily points, 9
# FAST_CANDIDATES, 40 folds, 7-day horizon) by sampling the whole process tree
# through a cold pool -- total RSS is linear in the worker count, 260 MB of
# service plus ~184 MB per worker:
#
#   workers   latency   peak tree RSS      workers   latency   peak tree RSS
#   1         19.38s      260 MB           5          4.70s     1175 MB
#   2         11.14s      595 MB           6          4.64s     1352 MB
#   3          6.18s      865 MB           7          4.88s     1546 MB
#   4          5.99s      992 MB           8          4.79s     1734 MB
#
# Rounded up to 200 MB so the derived count errs low. Note the last four rows:
# past ~5 workers the latency curve is flat and every extra worker is pure
# memory, which is why an unbounded count is all cost and no benefit.
FOLD_WORKER_RSS_BYTES = 200 * 1024 * 1024

# Share of this process's memory budget the fold pool may claim.
#
# The pool is not alone in the container: the service's own resident set is the
# ~260 MB above before a single worker starts, and every request in flight
# holds its own series and feature frame. Half the budget for the pool leaves
# the other half -- roughly twice the idle footprint -- to absorb concurrent
# requests and allocator slack. At the deployed limit (docker-compose caps
# prediction-service at 1500m) this resolves to 3 workers and a measured peak
# of 865 MB, 55% of the cap; the unbounded rule resolved to 8 on the same host
# and peaked at 1734 MB, i.e. over it. The trade is deliberate and cheap:
# 6.18s against 4.79s, both against 19.38s sequential. A forecast that is
# three times faster is worth a great deal less than one that does not get the
# service OOM-killed, and the OOM killer does not negotiate.
FOLD_POOL_MEMORY_SHARE = 0.5

# Worker cap when the memory budget cannot be read at all (no cgroup limit, no
# ``sysconf`` page counts). Two workers still halve the wait and cannot exceed
# ~650 MB, which is survivable in any container small enough for the question
# to matter. Guessing high here is the one mistake with an unbounded cost.
FOLD_WORKERS_WITHOUT_A_BUDGET = 2

# cgroup v2 then v1. In a container ``/sys/fs/cgroup`` is the container's own
# root, so these files hold ITS limit rather than the host's.
_CGROUP_MEMORY_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",                    # v2
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # v1
)

# Above this, a "limit" is a no-limit sentinel rather than a budget: 256 TiB
# is far past any container and far below cgroup v1's page-aligned 2**63-1.
_IMPLAUSIBLE_MEMORY_LIMIT_BYTES = 1 << 48

# Bar spacing per series frequency, stated to the feature layer instead of
# left to be guessed from a fold (see engineering.infer_bar_spacing): the
# hourly series is one bucket per DAY for the whole pre-2026-07-19 era and one
# per hour after it, so any inference from its own timestamps calls it daily
# for ~50 more days and grows the 1h/4h models a nine-day "EMA220".
FRAME_SPACING: dict[str, str] = {"daily": SPACING_DAILY, "hourly": SPACING_INTRADAY}

# horizon -> (series frequency, steps ahead)
HORIZON_SPECS: dict[str, tuple[str, int]] = {
    "1h": ("hourly", 1),
    "4h": ("hourly", 4),
    "eod": ("daily", 1),
    "1d": ("daily", 1),
    "3d": ("daily", 3),
    "7d": ("daily", 7),
    "30d": ("daily", 30),
}

CANDIDATES = (
    "naive", "sma", "ses", "arima", "theta", "holt_damped", "sarimax_exog",
    "linear", "rf", "gbr", "quantile_gbr", "hist_gb", "knn_analogue",
    "lorentzian_knn", "kalman_llt", "extra_trees", "huber", "hist_gb_tuned",
)

# auxiliary symbols made available to exog-aware models via set_context
CONTEXT_SYMBOLS: dict[str, str] = {
    "usd_irt": "USD_IRT",
    "xau_usd": "XAUUSD",
    # Tehran-exchange gold funds (Addendum 7): exchange price + retail flow
    "gold_fund": "IR_GOLD_FUND_AYAR",
    "fund_flow": "IR_GOLD_FUND_FLOW",
}


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold, in time order."""

    t_index: int          # index of the 'now' point in the series
    t_time: datetime
    base: float           # value at t (for direction accounting)
    pred: float
    actual: float


def load_series(engine: Engine, symbol: str, freq: str) -> pd.Series:
    """Good-quality price series for one symbol at daily or hourly resolution."""
    stmt = (
        select(prices.c.observed_at, prices.c.value)
        .where(prices.c.symbol == symbol, prices.c.quality == "ok")
        .order_by(prices.c.observed_at)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["observed_at", "value"])
    df["symbol"] = symbol
    return hourly_close(df, symbol) if freq == "hourly" else daily_close(df, symbol)


def horizon_enabled(freq: str, series: pd.Series) -> tuple[bool, str]:
    """Coverage gate per contract; returns (enabled, reason)."""
    if freq == "hourly":
        if series.empty:
            return False, "no hourly data"
        span_days = (series.index.max() - series.index.min()).total_seconds() / 86400.0
        # density guard: sparse (e.g. daily-only) data must not enable hourly
        min_points = MIN_HOURLY_DAYS * 12
        if span_days < MIN_HOURLY_DAYS or len(series) < min_points:
            return False, (
                f"hourly coverage insufficient: span {span_days:.1f}d with "
                f"{len(series)} points (need >={MIN_HOURLY_DAYS}d and "
                f">={min_points} points)"
            )
        return True, "ok"
    if len(series) < MIN_DAILY_POINTS:
        return False, f"daily points {len(series)} < {MIN_DAILY_POINTS} required"
    return True, "ok"


def _prediction_context(series: pd.Series, context: Optional[dict]) -> Optional[dict]:
    """``context`` plus a feature frame built once for the whole series.

    Only when the run carries no exog: with exog present each fold cuts its
    auxiliary series at its own last gold timestamp, and a shared frame would
    quietly decide that question for every fold at once.  Returns ``context``
    unchanged when the shortcut does not apply, so a caller can always use the
    result in place of the original.
    """
    if any((context or {}).get(key) is not None for key in EXOG_KEYS):
        return context
    if len(series) == 0:
        return context
    try:
        frame = _feature_matrix(series, context)
    except Exception as exc:  # a cache is never worth failing a run over
        log.warning("feature-frame prebuild skipped: %s", exc)
        return context
    return {**(context or {}), PREBUILT_FRAME_KEY: frame}


def _fold_indices(
    series: pd.Series, horizon_steps: int, min_train: int, max_folds: int
) -> list[int]:
    """The ``now`` positions walk-forward fits at, in time order."""
    last_now = len(series) - 1 - horizon_steps
    first_now = min_train - 1
    if last_now < first_now:
        return []
    step = max(1, (last_now - first_now) // max_folds + 1)
    return list(range(first_now, last_now + 1, step))


def _fit_fold_chunk(
    series: pd.Series,
    model_name: str,
    horizon_steps: int,
    indices: Sequence[int],
    context: Optional[dict],
) -> tuple[list[tuple[int, float]], Optional[str], list[tuple[int, str]]]:
    """Fit ``model_name`` at each index; return ``(preds, unavailable, failures)``.

    Split out of :func:`walk_forward` because this is the unit that gets handed
    to a worker PROCESS, and a worker must not do the reporting: its logger is
    not this process's, so anything it logged would vanish rather than reach the
    Issues tab.  Both abnormal outcomes are therefore returned as data and
    logged by the parent, in the same order and with the same wording as when
    the loop ran inline.
    """
    # Only the tabular family reads the feature frame; building it for `naive`
    # would be pure cost.  A future reader that is not a TabularModel simply
    # misses the reuse and rebuilds, which is the safe direction.
    ctx = (
        _prediction_context(series, context)
        if isinstance(make(model_name), TabularModel)
        else context
    )
    preds: list[tuple[int, float]] = []
    failures: list[tuple[int, str]] = []
    for i in indices:
        try:
            model = make(model_name)
            model.set_context(ctx)
            model.fit(series.iloc[: i + 1], horizon_steps)
            pred = float(model.predict_point())
        except ModelUnavailable as exc:
            return [], str(exc), failures
        except Exception as exc:  # a fold failure should not sink the run
            failures.append((i, str(exc)))
            continue
        if np.isfinite(pred):
            preds.append((i, pred))
    return preds, None, failures


def _chunks(indices: Sequence[int], n_chunks: int) -> list[list[int]]:
    """``indices`` split into at most ``n_chunks`` contiguous, near-equal parts."""
    n_chunks = max(1, min(n_chunks, len(indices)))
    size = math.ceil(len(indices) / n_chunks)
    return [list(indices[k:k + size]) for k in range(0, len(indices), size)]


def _read_int_file(path: str) -> Optional[int]:
    """First line of ``path`` as an int, or None if it is absent/unreadable."""
    try:
        with open(path, "r", encoding="ascii") as handle:
            return int(handle.readline().strip())
    except (OSError, ValueError):
        return None  # not Linux, no cgroup fs, or the sentinel "max"


def _physical_memory_bytes() -> Optional[int]:
    """Total RAM from ``sysconf``, or None where the page counts are absent."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size


@functools.lru_cache(maxsize=1)
def memory_budget_bytes() -> Optional[int]:
    """Memory this process may actually use, or None if that is unknowable.

    The cgroup limit when one is set, otherwise the machine's RAM, and the
    SMALLER of the two when both are readable — a cgroup limit above physical
    memory is a formality, not a budget.

    This is the number ``joblib.cpu_count()`` has an analogue of for CPU and
    nothing had for memory, which is the whole defect: the previous rule
    reasoned only about cores, ``prediction-service`` has a
    ``memory: 1500m`` limit and no ``cpus`` limit, so ``joblib.cpu_count()``
    returned the HOST's core count and the pool sized itself against hardware
    the container was never allowed to fill. Cached because a cgroup limit
    cannot change under a running process; tests call ``.cache_clear()``.
    """
    limits = [_read_int_file(path) for path in _CGROUP_MEMORY_LIMIT_FILES]
    candidates = [
        value for value in limits
        # "unlimited" is spelled differently by every cgroup version and none
        # of the spellings is a budget: v2 writes "max" (which _read_int_file
        # already drops), v1 a page-aligned 2**63-1. Read as a number the
        # latter affords twenty billion workers, so any value past a quarter
        # of a petabyte is a sentinel rather than a limit — checked here and
        # not only against physical memory, because a host where sysconf is
        # unavailable is exactly where an unfiltered sentinel would survive.
        if value is not None and 0 < value < _IMPLAUSIBLE_MEMORY_LIMIT_BYTES
    ]
    physical = _physical_memory_bytes()
    if physical:
        # and a limit above the hardware is not a limit either
        candidates = [value for value in candidates if value <= physical]
        candidates.append(physical)
    return min(candidates) if candidates else None


def _memory_bounded_worker_cap() -> int:
    """How many fold workers fit in this process's memory budget.

    ``FOLD_POOL_MEMORY_SHARE`` of :func:`memory_budget_bytes`, divided by the
    measured :data:`FOLD_WORKER_RSS_BYTES`; floored at one worker (sequential)
    and ceilinged at :data:`MAX_PARALLEL_FOLD_WORKERS`, past which a container
    with memory to spare would keep adding interpreters for no latency.
    """
    budget = memory_budget_bytes()
    if budget is None:
        return FOLD_WORKERS_WITHOUT_A_BUDGET
    affordable = int(budget * FOLD_POOL_MEMORY_SHARE // FOLD_WORKER_RSS_BYTES)
    return max(1, min(affordable, MAX_PARALLEL_FOLD_WORKERS))


def _worker_count(n_jobs: Optional[int]) -> int:
    """Resolve a requested worker count against this machine.

    Follows joblib's convention: a positive count is taken as given, anything
    else (None, -1) means "decide for me" — and the decision is the smallest
    of three ceilings, because a worker costs a core AND a container's worth
    of memory AND stops paying for itself at some point:

    * one less than the core count, so an interactive forecast cannot take the
      whole box away from the API process serving it;
    * what the memory budget affords (:func:`_memory_bounded_worker_cap`) —
      the binding constraint in the deployed container, and the one whose
      absence let a single request peak at 1734 MB against a 1500m limit;
    * :data:`MAX_PARALLEL_FOLD_WORKERS`, where the latency curve goes flat.
    """
    if n_jobs is not None and n_jobs > 0:
        return n_jobs
    # joblib's count, not os.cpu_count(): this runs in a container, and
    # os.cpu_count() reports the HOST's cores through a cgroup CPU quota —
    # which is how a 2-CPU container ends up starting eight workers.
    cores = joblib.cpu_count() or 1
    return max(1, min(cores - 1, _memory_bounded_worker_cap()))


def walk_forward(
    series: pd.Series,
    model_name: str,
    horizon_steps: int,
    min_train: int = MIN_TRAIN_POINTS,
    max_folds: int = MAX_FOLDS,
    context: Optional[dict] = None,
    n_jobs: int = 1,
) -> list[Fold]:
    """Expanding-window walk-forward validation (no shuffling).

    Fold ``i``: fit on ``series[:i+1]`` (data known at time i), predict the
    value at ``i + horizon_steps``, compare with the realized value.  A fresh
    model instance is created per fold except models flagged
    ``reuse_across_folds`` (ARIMA/SARIMAX), which reuse their order selection
    from the earliest window (train-only information).  ``context`` carries
    auxiliary point-in-time series for exog-aware models; a model raising
    :class:`ModelUnavailable` (e.g. exog missing) is skipped entirely.

    ``n_jobs`` > 1 spreads the folds over worker processes.  The folds are
    independent by construction — each one fits a fresh model on a prefix of
    the same series — so this changes only WHERE each fit runs, never what it
    sees or what it returns; the folds come back in time order either way.
    Models that ``reuse_across_folds`` are exempt: their whole point is that
    fold n inherits fold 0's order selection, which is a sequential dependency.
    """
    indices = _fold_indices(series, horizon_steps, min_train, max_folds)
    if not indices:
        return []

    reusable = make(model_name)  # ARIMA/SARIMAX benefit from cached order selection
    if reusable.reuse_across_folds:
        return _walk_forward_sequential_reused(
            series, reusable, model_name, horizon_steps, indices, context)

    workers = _worker_count(n_jobs) if n_jobs != 1 else 1
    chunks = _chunks(indices, workers)
    if len(chunks) > 1:
        results = joblib.Parallel(n_jobs=len(chunks))(
            joblib.delayed(_fit_fold_chunk)(
                series, model_name, horizon_steps, chunk, context)
            for chunk in chunks
        )
    else:
        results = [_fit_fold_chunk(series, model_name, horizon_steps, chunks[0], context)]

    preds: list[tuple[int, float]] = []
    for chunk_preds, unavailable, failures in results:
        for i, exc in failures:
            log.warning("walk_forward %s fold@%d failed: %s", model_name, i, exc)
        if unavailable is not None:
            log.info("walk_forward %s skipped: %s", model_name, unavailable)
            return []
        preds.extend(chunk_preds)
    preds.sort()
    return [
        Fold(
            t_index=i,
            t_time=series.index[i].to_pydatetime(),
            base=float(series.iloc[i]),
            pred=pred,
            actual=float(series.iloc[i + horizon_steps]),
        )
        for i, pred in preds
    ]


def _walk_forward_sequential_reused(
    series: pd.Series,
    reusable: ForecastModel,
    model_name: str,
    horizon_steps: int,
    indices: Sequence[int],
    context: Optional[dict],
) -> list[Fold]:
    """The ``reuse_across_folds`` path, which cannot be split across workers."""
    ctx = (
        _prediction_context(series, context)
        if isinstance(reusable, TabularModel)
        else context
    )
    folds: list[Fold] = []
    for i in indices:
        try:
            reusable.set_context(ctx)
            reusable.fit(series.iloc[: i + 1], horizon_steps)
            pred = float(reusable.predict_point())
        except ModelUnavailable as exc:
            log.info("walk_forward %s skipped: %s", model_name, exc)
            return []
        except Exception as exc:  # a fold failure should not sink the run
            log.warning("walk_forward %s fold@%d failed: %s", model_name, i, exc)
            continue
        if not np.isfinite(pred):
            continue
        folds.append(
            Fold(
                t_index=i,
                t_time=series.index[i].to_pydatetime(),
                base=float(series.iloc[i]),
                pred=pred,
                actual=float(series.iloc[i + horizon_steps]),
            )
        )
    return folds


def _fold_step(series: pd.Series, horizon_steps: int, max_folds: int = MAX_FOLDS) -> int:
    """Fold spacing used by :func:`walk_forward` (kept in one place so the
    embargo in :func:`split_folds` matches the actual fold geometry)."""
    last_now = len(series) - 1 - horizon_steps
    first_now = MIN_TRAIN_POINTS - 1
    if last_now < first_now:
        return 1
    return max(1, (last_now - first_now) // max_folds + 1)


def fold_metrics(
    folds: Sequence[Fold], coverage_folds: Optional[Sequence[Fold]] = None
) -> dict:
    """mae, rmse, smape, mase, directional_accuracy, interval coverage.

    ``mase`` (Hyndman & Koehler 2006) scales MAE by the in-sample naive error
    of the SAME folds: < 1 means "better than repeating the last observation".
    It is scale-free and comparable across symbols and horizons, unlike sMAPE.

    Interval coverage measures something different from every other metric
    here, on a different fold set, so it is published under its own name as
    ONE nested block — ``interval_coverage_walk_forward`` — carrying
    ``rate`` (``None`` unless the denominator supports one), ``hits``,
    ``scored_folds``, ``total_folds``, ``residual_warmup_folds``,
    ``min_scored_folds`` and ``status``. No bare float sits in the flat
    namespace where it could be read next to ``n_folds`` and attributed to the
    wrong fold set; run 37 shipped ``interval_coverage = 1.0`` from a single
    scored fold that way.

    ``coverage_folds`` is the fold set the coverage walk runs over, defaulting
    to ``folds``. The error metrics belong to the embargoed HOLDOUT (12 folds,
    the point of Addendum 14), but interval coverage is a property of the
    interval CONSTRUCTION, measured walk-forward with no peeking — and
    ``walk_forward_coverage`` spends its first ten folds building the residual
    pool, so a 12-fold tail can score at most 2 against a bar of 20. Scored
    from the tail it was structurally unpublishable for every candidate at
    every horizon forever; scored from the full walk-forward set the same
    honest denominator clears comfortably (40 folds -> 30 scored). The
    residuals are still strictly causal per fold; what the full set costs is
    that the selection portion carries the winner's selection optimism, which
    is why the field is named for the walk it came from and reports
    ``total_folds`` next to ``n_folds``.
    """
    if not folds:
        return {}
    preds = np.array([f.pred for f in folds])
    actuals = np.array([f.actual for f in folds])
    bases = np.array([f.base for f in folds])
    errors = actuals - preds
    denom = np.abs(actuals) + np.abs(preds)
    smape = float(np.mean(np.where(denom > 0, 2.0 * np.abs(errors) / denom, 0.0)) * 100.0)
    dir_hits = np.sign(preds - bases) == np.sign(actuals - bases)
    mae = float(np.mean(np.abs(errors)))
    naive_mae = float(np.mean(np.abs(actuals - bases)))  # naive = carry the base forward
    cov_folds = list(folds if coverage_folds is None else coverage_folds)
    cov = walk_forward_coverage(
        [f.pred for f in cov_folds], [f.actual for f in cov_folds]
    )
    return {
        "n_folds": len(folds),
        "mae": mae,
        "rmse": float(np.sqrt(np.mean(errors**2))),
        "smape": smape,
        "mase": float(mae / naive_mae) if naive_mae > 0 else None,
        "directional_accuracy": float(np.mean(dir_hits)),
        "interval_coverage_walk_forward": cov.as_published(),
    }


def detect_regime(series: pd.Series, window: int = 20) -> str:
    """trending_up / trending_down / ranging / high_volatility / unknown."""
    values = series.astype(float).to_numpy()
    if len(values) < window + 5:
        return "unknown"
    returns = pd.Series(values).pct_change().dropna()
    vol_series = returns.rolling(window).std().dropna()
    if vol_series.empty:
        return "unknown"
    current_vol = float(vol_series.iloc[-1])
    vol_p90 = float(np.quantile(vol_series.to_numpy(), 0.90))
    if current_vol > vol_p90 and current_vol > 0:
        return "high_volatility"
    tail = np.log(values[-window:])
    x = np.arange(window, dtype=float)
    slope = float(np.polyfit(x, tail, 1)[0])  # log-return per step
    resid = tail - np.polyval(np.polyfit(x, tail, 1), x)
    strength = abs(slope) * window / (float(np.std(resid)) + 1e-12)
    if strength > 2.0:
        return "trending_up" if slope > 0 else "trending_down"
    return "ranging"


# Holdout split (Addendum 14): the LAST ~30% of folds (chronologically, at
# least HOLDOUT_MIN) are excluded from candidate selection and ensemble
# weight fitting, then used to score the chosen winner. The minimum of ~18
# noisy sMAPE estimates is optimistically biased; scoring on folds the
# winner was not selected on removes that bias from the stored metrics and
# the interval residuals. With fewer than HOLDOUT_MIN_TOTAL folds there is
# not enough data to split and the legacy all-fold behavior applies.
HOLDOUT_FRACTION = 0.3
HOLDOUT_MIN = 5
HOLDOUT_MIN_TOTAL = 15


def split_folds(
    folds: list[Fold], horizon_steps: int = 1, step: int = 1
) -> tuple[list[Fold], list[Fold]]:
    """Chronological (selection, holdout) split with an EMBARGO gap.

    Walk-forward folds are spaced ``step`` index positions apart while each
    fold's target lies ``horizon_steps`` ahead, so adjacent folds share future
    data whenever ``step < horizon_steps`` (measured: 30x target overlap at
    n=120,h=30). Without a gap, the last selection folds and the first holdout
    folds resolve into the SAME future window — the holdout is then not truly
    out-of-sample.

    The embargo drops ``ceil(horizon_steps / step)`` folds between the two
    blocks (Lopez de Prado's purging/embargo idea applied to the split point).
    Holdout is empty when too few folds remain to be meaningful.
    """
    if len(folds) < HOLDOUT_MIN_TOTAL:
        return folds, []
    n_hold = max(HOLDOUT_MIN, int(len(folds) * HOLDOUT_FRACTION))
    embargo = max(0, math.ceil(max(1, horizon_steps) / max(1, step)) - 1)
    # Never let the embargo eat so much that selection loses its majority.
    embargo = min(embargo, max(0, len(folds) - n_hold - HOLDOUT_MIN))
    holdout = folds[-n_hold:]
    selection = folds[: len(folds) - n_hold - embargo]
    if len(selection) < HOLDOUT_MIN:
        return folds, []
    return selection, holdout


def evaluate_candidates(
    series: pd.Series,
    horizon_steps: int,
    candidates: Optional[Sequence[str]] = None,
    context: Optional[dict] = None,
    max_folds: int = MAX_FOLDS,
    n_jobs: int = 1,
) -> dict[str, dict]:
    """Walk-forward all candidates on the same folds; add the ensemble.

    Returns ``{model_name: {"folds": [...], "metrics": {...},
    "sel_metrics": {...}, "holdout_metrics": {...}|None}}``:

    * ``sel_metrics`` — the SELECTION folds (first ~70%): what the
      tournament may look at;
    * ``holdout_metrics`` — the held-out tail (last ~30%): unbiased scoring
      for whichever candidate wins; ``None`` when too few folds to split;
    * ``metrics`` — all folds (legacy consumers, display).

    Ensemble membership and weights come from selection folds only, so the
    ensemble candidate enters the tournament with no in-sample advantage.
    ``candidates`` defaults to the module-level ``CANDIDATES`` (resolved at
    call time so tests can narrow the set); ``context`` feeds exog-aware
    models (see :func:`walk_forward`); ``max_folds`` lets interactive callers
    (custom horizons) trade validation depth for latency; ``n_jobs`` spreads
    each candidate's folds over worker processes, which changes latency only
    (see :func:`walk_forward`) and defaults to the sequential behaviour the
    nightly run has always had.
    """
    if candidates is None:
        candidates = CANDIDATES
    results: dict[str, dict] = {}
    for name in candidates:
        folds = walk_forward(series, name, horizon_steps, context=context,
                             max_folds=max_folds, n_jobs=n_jobs)
        if folds:
            sel, hold = split_folds(folds, horizon_steps, _fold_step(series, horizon_steps, max_folds))
            results[name] = {
                "folds": folds,
                "metrics": fold_metrics(folds),
                # interval coverage always walks the FULL fold set: the
                # selection and holdout blocks are far too short to score it
                # (see fold_metrics), and it is a property of the interval
                # construction rather than of the selection split.
                "sel_metrics": fold_metrics(sel, coverage_folds=folds),
                "holdout_metrics": fold_metrics(hold, coverage_folds=folds) if hold else None,
            }

    naive = results.get("naive")
    if naive:
        naive_smape = naive["sel_metrics"]["smape"]
        member_smapes = {
            name: r["sel_metrics"]["smape"]
            for name, r in results.items()
            if name != "naive" and r["sel_metrics"]["smape"] < naive_smape
        }
        if len(member_smapes) >= 2:
            # Deduplicate members whose fold predictions are identical (e.g. a
            # tuned variant that degenerated to its untuned twin): otherwise
            # inverse-sMAPE weighting hands that single model double weight.
            member_smapes = _drop_duplicate_members(member_smapes, results)
        if len(member_smapes) >= 2:
            weights = inverse_smape_weights(member_smapes)
            fold_maps = {
                name: {f.t_index: f for f in results[name]["folds"]}
                for name in member_smapes
            }
            common = set.intersection(*(set(m) for m in fold_maps.values()))
            ens_folds = [
                Fold(
                    t_index=i,
                    t_time=fold_maps[next(iter(member_smapes))][i].t_time,
                    base=fold_maps[next(iter(member_smapes))][i].base,
                    pred=combine(
                        {name: fold_maps[name][i].pred for name in member_smapes}, weights
                    ),
                    actual=fold_maps[next(iter(member_smapes))][i].actual,
                )
                for i in sorted(common)
            ]
            if ens_folds:
                sel, hold = split_folds(
                    ens_folds, horizon_steps, _fold_step(series, horizon_steps, max_folds)
                )
                results["ensemble"] = {
                    "folds": ens_folds,
                    "metrics": fold_metrics(ens_folds),
                    "sel_metrics": fold_metrics(sel, coverage_folds=ens_folds),
                    "holdout_metrics": (
                        fold_metrics(hold, coverage_folds=ens_folds) if hold else None
                    ),
                    "weights": weights,
                }
    return results


def report_metrics(r: dict) -> dict:
    """The honest metrics for a candidate: holdout when available, else all."""
    return r.get("holdout_metrics") or r["metrics"]


# --- significance gating (Addendum 15) --------------------------------------
# argmin over ~18 candidates is a multiple-comparison machine: with 18 coin
# flips against naive, the best one looks good by luck alone. Three guards,
# all cheap and all documented in docs/CONTRACTS.md:
#   1. MIN_EDGE_PCT   - the winner must beat naive by a material margin, not
#                       a rounding difference;
#   2. bootstrap CI   - resampling the per-fold sMAPE differences must put the
#                       improvement's upper bound below zero (one-sided);
#   3. holdout confirm - the same edge must survive on embargoed folds the
#                       winner was never selected on.
# Failing any of them means naive wins, which is a legitimate outcome.
MIN_EDGE_PCT = 0.02        # relative sMAPE improvement required (2%)
BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_CONF = 0.90      # one-sided confidence for the paired difference


def _paired_fold_smapes(folds: Sequence[Fold]) -> dict[int, float]:
    """Per-fold sMAPE keyed by t_index, so candidates pair on identical folds."""
    out: dict[int, float] = {}
    for f in folds:
        denom = abs(f.actual) + abs(f.pred)
        out[f.t_index] = (2.0 * abs(f.actual - f.pred) / denom * 100.0) if denom > 0 else 0.0
    return out


def bootstrap_beats(
    candidate: Sequence[Fold],
    baseline: Sequence[Fold],
    rounds: int = BOOTSTRAP_ROUNDS,
    conf: float = BOOTSTRAP_CONF,
    seed: int = 12345,
) -> Optional[bool]:
    """True when the candidate's paired sMAPE edge over naive is significant.

    Pairs folds by ``t_index`` (never compares different fold sets), resamples
    the per-fold differences with replacement, and requires the upper bound of
    the one-sided ``conf`` interval on the MEAN difference to be < 0 (i.e. the
    candidate is better even in the unlucky tail). ``None`` when there are too
    few paired folds to say anything.
    """
    cand = _paired_fold_smapes(candidate)
    base = _paired_fold_smapes(baseline)
    common = sorted(set(cand) & set(base))
    if len(common) < 8:
        return None
    diffs = np.array([cand[i] - base[i] for i in common], dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diffs), size=(rounds, len(diffs)))
    means = diffs[idx].mean(axis=1)
    upper = float(np.quantile(means, conf))
    return bool(upper < 0.0)


# A candidate whose fold predictions are (nearly) identical to naive was not
# actually exercised: tabular models fall back to naive until they have enough
# clean feature rows (measured: a tabular model is bit-identical to naive until
# n_train >= 89 at h=30, and sarimax_exog until n_train >= 69). Such a
# candidate's selection sMAPE describes NAIVE, not the model that later ships
# refit on the full series where it IS active.
DEGENERATE_FOLD_TOLERANCE = 1e-9
MAX_DEGENERATE_SHARE = 0.5


def degenerate_share(candidate: Sequence[Fold], naive: Sequence[Fold]) -> float:
    """Fraction of paired folds where the candidate merely reproduced naive."""
    naive_by_idx = {f.t_index: f.pred for f in naive}
    paired = [(f.pred, naive_by_idx[f.t_index]) for f in candidate
              if f.t_index in naive_by_idx]
    if not paired:
        return 0.0
    same = sum(1 for p, n in paired if abs(p - n) <= DEGENERATE_FOLD_TOLERANCE * max(1.0, abs(n)))
    return same / len(paired)


def _drop_duplicate_members(
    member_smapes: dict[str, float], results: dict[str, dict]
) -> dict[str, float]:
    """Keep one representative per identical prediction vector."""
    kept: dict[str, float] = {}
    seen: list[tuple[str, np.ndarray]] = []
    for name in sorted(member_smapes, key=lambda n: member_smapes[n]):
        vec = np.array([f.pred for f in results[name]["folds"]], dtype=float)
        if any(v.shape == vec.shape and np.allclose(v, vec, rtol=0, atol=1e-12)
               for _, v in seen):
            log.info("ensemble: dropping %s (identical predictions to an existing member)", name)
            continue
        seen.append((name, vec))
        kept[name] = member_smapes[name]
    return kept


# Explicit, persisted activation states (Addendum 16). "naive won" is a
# legitimate outcome and must stay visible with its reason.
ACTIVATION_CONFIRMED = "confirmed"
ACTIVATION_INSUFFICIENT_HOLDOUT = "insufficient_holdout"
ACTIVATION_REJECTED_HOLDOUT = "rejected_by_holdout"
ACTIVATION_REJECTED_MASE = "rejected_by_mase"
ACTIVATION_REJECTED_MATERIALITY = "rejected_by_materiality"
ACTIVATION_REJECTED_INSTABILITY = "rejected_by_instability"
ACTIVATION_CANDIDATE_FAILED = "candidate_failed"
ACTIVATION_NAIVE_NO_EDGE = "naive_no_proven_edge"


def _state_for(reasons: list[str]) -> str:
    """Map the first (most fundamental) rejection reason to a state code."""
    joined = "; ".join(reasons)
    if "no embargoed holdout" in joined:
        return ACTIVATION_INSUFFICIENT_HOLDOUT
    if "MASE" in joined:
        return ACTIVATION_REJECTED_MASE
    if "holdout edge" in joined:
        return ACTIVATION_REJECTED_HOLDOUT
    if "bootstrap" in joined or "merely reproduced naive" in joined:
        return ACTIVATION_REJECTED_INSTABILITY
    if "edge" in joined:
        return ACTIVATION_REJECTED_MATERIALITY
    return ACTIVATION_NAIVE_NO_EDGE


def select_winner(results: dict[str, dict]) -> str:
    """Significance-gated selection (see MIN_EDGE_PCT block above).

    Ranking happens on selection folds; the chosen candidate must then clear a
    material edge, a paired bootstrap test, and the embargoed holdout. Any
    failure falls back to ``naive``.
    """
    if not results:
        return ""
    if "naive" not in results:
        return min(results, key=lambda n: results[n]["sel_metrics"]["smape"])
    naive_sel = results["naive"]["sel_metrics"]["smape"]
    best_name, best_smape = "naive", naive_sel
    for name, r in results.items():
        if name == "naive":
            continue
        smape = r["sel_metrics"]["smape"]
        if smape < best_smape and smape < naive_sel:
            best_name, best_smape = name, smape
    if best_name == "naive":
        return "naive"

    reasons: list[str] = []
    # 0) the candidate must actually have been exercised on the selection folds
    # Selection folds only — the comment always claimed this but the code
    # passed every fold, letting holdout behavior influence the gate.
    best_sel = results[best_name].get("sel_folds") or results[best_name].get("folds", [])
    naive_sel_folds = results["naive"].get("sel_folds") or results["naive"].get("folds", [])
    share = degenerate_share(best_sel, naive_sel_folds)
    if share > MAX_DEGENERATE_SHARE:
        reasons.append(f"{share:.0%} of folds merely reproduced naive (model not exercised)")
    # 1) material edge on selection folds
    if naive_sel > 0 and (naive_sel - best_smape) / naive_sel < MIN_EDGE_PCT:
        reasons.append(
            f"edge {(naive_sel - best_smape) / naive_sel:.3%} < {MIN_EDGE_PCT:.0%}"
        )
    # 2) paired bootstrap on the same selection folds
    sig = bootstrap_beats(best_sel, naive_sel_folds)
    if sig is False:
        reasons.append("bootstrap CI includes no improvement")
    # 3) embargoed holdout confirmation, held to the SAME materiality bar as
    #    selection. A bare "<" let XAUUSD/1d extra_trees activate on a 0.0004
    #    sMAPE edge over 12 folds (1.47963 vs 1.47999 — 0.02% relative, and
    #    MASE 1.004, i.e. worse than naive in absolute-error terms). Winning by
    #    a rounding difference is not evidence of skill.
    winner_hold = results[best_name].get("holdout_metrics")
    naive_hold = results["naive"].get("holdout_metrics")
    if not (winner_hold and naive_hold):
        # No embargoed holdout means no out-of-sample confirmation exists.
        # Activating here would ship a model certified only on the folds that
        # selected it — exactly the optimism the holdout was added to remove.
        reasons.append(
            f"no embargoed holdout (needs >= {HOLDOUT_MIN_TOTAL} folds); "
            "cannot confirm out-of-sample"
        )
    else:
        nh = naive_hold["smape"]
        edge = (nh - winner_hold["smape"]) / nh if nh > 0 else 0.0
        if edge < MIN_EDGE_PCT:
            reasons.append(f"holdout edge {edge:.3%} < {MIN_EDGE_PCT:.0%}")
    # 4) a MASE >= 1 means it did not beat "carry the last value forward" in
    #    absolute-error terms, whatever sMAPE says.
    winner_mase = (winner_hold or {}).get("mase")
    if isinstance(winner_mase, (int, float)) and winner_mase >= 1.0:
        reasons.append(f"holdout MASE {winner_mase:.3f} >= 1 (no gain over naive)")
    if reasons:
        log.info("candidate %s rejected -> naive (%s)", best_name, "; ".join(reasons))
        results[best_name]["rejection_reason"] = "; ".join(reasons)
        results[best_name]["activation_state"] = _state_for(reasons)
        return "naive"
    results[best_name]["activation_state"] = ACTIVATION_CONFIRMED
    return best_name


def _build_final_model(
    name: str,
    results: dict[str, dict],
    series: pd.Series,
    horizon_steps: int,
    context: Optional[dict] = None,
    selection_end: Optional[int] = None,
) -> ForecastModel:
    """Refit the chosen model on the full series for artifact persistence.

    ``selection_end`` is the index of the last SELECTION fold. Models that
    tune their own hyperparameters get :meth:`prepare_params` called on the
    prefix ending there, so the deployed artifact's configuration is chosen
    without ever seeing the embargoed holdout it was certified on. (Previously
    the artifact re-tuned on the full series, whose tuning-validation tail
    overlapped the holdout by ~198 rows, and then shipped carrying metrics
    measured for a *different* configuration.)
    """
    if name == "ensemble":
        weights = results["ensemble"]["weights"]
        members = {member: make(member) for member in weights}
        model: ForecastModel = EnsembleModel(members, weights)
    else:
        model = make(name)
    model.set_context(context)
    prepare = getattr(model, "prepare_params", None)
    if callable(prepare) and selection_end is not None and selection_end > MIN_TRAIN_POINTS:
        try:
            prepare(series.iloc[: selection_end + 1], horizon_steps)
        except Exception as exc:  # noqa: BLE001 — tuning must never sink a run
            log.warning("prepare_params failed for %s: %s", name, exc)
    model.fit(series, horizon_steps)
    return model


# Symbols trained by the scheduled pipeline (Addendum 8). The first entry is
# the "primary" whose results also fill the legacy flat summary keys.
FORECAST_SYMBOLS: tuple[str, ...] = ("IR_GOLD_18K", "XAUUSD")

# Exogenous context per forecast symbol. Global gold gets none: its own
# series carries the signal and the Iranian exog (USD/IRT, funds) is noise
# for it; exog-aware models simply skip themselves via ModelUnavailable.
SYMBOL_CONTEXTS: dict[str, dict[str, str]] = {
    "IR_GOLD_18K": CONTEXT_SYMBOLS,
    "XAUUSD": {},
}


def train_all(
    engine: Engine,
    settings: Settings,
    horizons: Optional[Sequence[str]] = None,
    symbols: Optional[Sequence[str]] = None,
) -> dict:
    """Full training pass: per-symbol, per-horizon evaluation + persistence."""
    requested = [h for h in (horizons or list(HORIZON_SPECS)) if h in HORIZON_SPECS]
    req_symbols = [s for s in (symbols or FORECAST_SYMBOLS) if s in FORECAST_SYMBOLS]
    primary = req_symbols[0] if req_symbols else "IR_GOLD_18K"
    started = utcnow()
    version = started.strftime("%Y-%m-%dT%H:%M:%SZ")

    with engine.begin() as conn:
        # Reap runs stranded in 'running' by a container kill (OOM/redeploy):
        # only this process ever finalizes a run, so anything still "running"
        # after 3 hours is dead and would otherwise show as in-flight forever.
        conn.execute(
            training_runs.update()
            .where(
                training_runs.c.status == "running",
                training_runs.c.started_at < started - timedelta(hours=3),
            )
            .values(status="failed", finished_at=started,
                    error="stale run reaped: service restarted mid-training")
        )
        run_id = conn.execute(
            training_runs.insert().values(
                started_at=started, status="running", horizons=list(requested),
                models_evaluated=[], selected={},
            )
        ).inserted_primary_key[0]

    summary: dict = {"run_id": int(run_id), "horizons": {}, "selected": {}, "symbols": {}}
    models_evaluated: list[dict] = []
    selected_by_symbol: dict[str, dict[str, str]] = {}
    notes: list[str] = []
    any_trained = False
    error_msg: Optional[str] = None

    try:
        for sym in req_symbols:
            sym_summary: dict = {"horizons": {}, "selected": {}}
            summary["symbols"][sym] = sym_summary
            selected_by_symbol[sym] = {}
            series_cache: dict[str, pd.Series] = {}
            context_cache: dict[str, dict] = {}
            sym_context_map = SYMBOL_CONTEXTS.get(sym, {})

            for horizon in requested:
                freq, steps = HORIZON_SPECS[horizon]
                if freq not in series_cache:
                    series_cache[freq] = load_series(engine, sym, freq)
                series = series_cache[freq]

                enabled, reason = horizon_enabled(freq, series)
                if not enabled:
                    notes.append(f"{sym}/{horizon}: disabled ({reason})")
                    sym_summary["horizons"][horizon] = {"enabled": False, "reason": reason}
                    continue

                if freq not in context_cache:
                    ctx: dict = {
                        key: load_series(engine, ctx_sym, freq)
                        for key, ctx_sym in sym_context_map.items()
                    }
                    # Declare what this run knows and a single fold cannot:
                    # the bar spacing, and the shortest frame any stage will
                    # build (fold 0 fits on series[:MIN_TRAIN_POINTS]). Both
                    # are constant for the run, which is what keeps the feature
                    # column set identical across selection folds, holdout
                    # folds and the final refit.
                    ctx[FRAME_SPACING_KEY] = FRAME_SPACING[freq]
                    ctx[MIN_HISTORY_KEY] = MIN_TRAIN_POINTS
                    context_cache[freq] = ctx
                context = context_cache[freq]

                results = evaluate_candidates(series, steps, context=context)
                if not results:
                    notes.append(f"{sym}/{horizon}: no candidate produced folds")
                    sym_summary["horizons"][horizon] = {"enabled": False, "reason": "no folds"}
                    continue

                winner = select_winner(results)
                selected_by_symbol[sym][horizon] = winner
                any_trained = True
                naive_result = results.get("naive")
                baseline_metrics = report_metrics(naive_result) if naive_result else {}

                sel_folds, _ = split_folds(
                    results[winner]["folds"], steps, _fold_step(series, steps, MAX_FOLDS)
                )
                selection_end = sel_folds[-1].t_index if sel_folds else None
                final_model = _build_final_model(
                    winner, results, series, steps, context, selection_end
                )
                # Interval residuals from the winner's HOLDOUT folds when
                # enough exist: selection-fold residuals understate true
                # out-of-sample error for the fold-minimizing winner.
                _, winner_hold = split_folds(
                    results[winner]["folds"], steps, _fold_step(series, steps, MAX_FOLDS)
                )
                residual_folds = (
                    winner_hold if len(winner_hold) >= 8 else results[winner]["folds"]
                )
                residuals = relative_residuals(
                    [f.pred for f in residual_folds], [f.actual for f in residual_folds]
                )
                artifact_dir = os.path.join(settings.models_dir, sym, horizon)
                os.makedirs(artifact_dir, exist_ok=True)
                artifact_path = os.path.join(
                    artifact_dir, f"{winner}-{version.replace(':', '')}.joblib"
                )
                joblib.dump(
                    {
                        "model": final_model,
                        "model_name": winner,
                        "symbol": sym,
                        "horizon": horizon,
                        "horizon_steps": steps,
                        "freq": freq,
                        "residual_pcts": residuals,
                        "metrics": report_metrics(results[winner]),
                        "trained_at": version,
                    },
                    artifact_path,
                )

                with engine.begin() as conn:
                    conn.execute(
                        update(model_versions)
                        .where(
                            model_versions.c.symbol == sym,
                            model_versions.c.horizon == horizon,
                        )
                        .values(is_active=False)
                    )
                    winner_id = None
                    for name, r in results.items():
                        params: dict = {"horizon_steps": steps, "freq": freq,
                                        "holdout_scored": r.get("holdout_metrics") is not None,
                                        "n_folds_total": len(r.get("folds", [])),
                                        "n_folds_selection": len(r.get("sel_folds", [])),
                                        "n_folds_holdout": len(r.get("hold_folds", []))}
                        if r.get("activation_state"):
                            params["activation_state"] = r["activation_state"]
                        if r.get("rejection_reason"):
                            params["rejection_reason"] = r["rejection_reason"]
                        if name == "ensemble":
                            params["weights"] = r["weights"]
                        row_id = conn.execute(
                            model_versions.insert().values(
                                symbol=sym,
                                horizon=horizon,
                                model_name=name,
                                version=version,
                                trained_at=started,
                                training_start=series.index.min().to_pydatetime(),
                                training_end=series.index.max().to_pydatetime(),
                                n_observations=int(len(series)),
                                metrics=report_metrics(r),
                                baseline_metrics=baseline_metrics,
                                params=params,
                                artifact_path=artifact_path if name == winner else None,
                                is_active=False,
                            )
                        ).inserted_primary_key[0]
                        if name == winner:
                            winner_id = row_id
                        if sym == primary:
                            # metric labels predate multi-symbol; keep them
                            # stable for the primary symbol only
                            MODEL_SMAPE.labels(horizon=horizon, model=name).set(
                                report_metrics(r)["smape"]
                            )
                        models_evaluated.append(
                            {"symbol": sym, "horizon": horizon, "model": name,
                             "smape": report_metrics(r)["smape"]}
                        )
                    if winner_id is not None:
                        conn.execute(
                            update(model_versions)
                            .where(model_versions.c.id == winner_id)
                            .values(is_active=True)
                        )

                sym_summary["horizons"][horizon] = {
                    "enabled": True,
                    "winner": winner,
                    "beats_naive": winner != "naive",
                    "metrics": report_metrics(results[winner]),
                    "baseline_metrics": baseline_metrics,
                }
            sym_summary["selected"] = dict(selected_by_symbol[sym])
    except Exception as exc:  # record the failure in training_runs
        error_msg = f"{type(exc).__name__}: {exc}"
        log.exception("training failed")

    status = "failed" if error_msg else ("succeeded" if any_trained or not requested else "succeeded")
    with engine.begin() as conn:
        conn.execute(
            update(training_runs)
            .where(training_runs.c.id == run_id)
            .values(
                finished_at=utcnow(),
                status=status,
                models_evaluated=models_evaluated,
                selected=selected_by_symbol,
                error=error_msg,
                notes="; ".join(notes) if notes else None,
            )
        )
    # legacy flat keys mirror the primary symbol (dashboards/tests predate
    # multi-symbol training)
    primary_summary = summary["symbols"].get(primary, {"horizons": {}, "selected": {}})
    summary["horizons"] = primary_summary["horizons"]
    summary["selected"] = primary_summary["selected"]
    summary["status"] = status
    if error_msg:
        summary["error"] = error_msg
    if notes:
        summary["notes"] = notes
    return summary
