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

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import model_versions, prices, training_runs, utcnow
from ..features.engineering import daily_close, hourly_close
from ..metrics import MODEL_SMAPE
from .analogue import KNNAnalogueModel  # noqa: F401  (registers 'knn_analogue')
from .arima import ARIMAModel  # noqa: F401  (registers 'arima')
from .base import ForecastModel, ModelUnavailable, make
from .baselines import NaiveModel  # noqa: F401  (registers baselines)
from .classical import ThetaForecastModel  # noqa: F401  (registers 'theta', 'holt_damped')
from .ensemble import EnsembleModel, combine, inverse_smape_weights
from .intervals import relative_residuals, walk_forward_coverage
from .ml import TabularModel  # noqa: F401  (registers ml models)
from .sarimax_exog import SarimaxExogModel  # noqa: F401  (registers 'sarimax_exog')

log = logging.getLogger(__name__)

MIN_TRAIN_POINTS = 60
MIN_DAILY_POINTS = 120
MIN_HOURLY_DAYS = 14
MAX_FOLDS = 40

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
)

# auxiliary symbols made available to exog-aware models via set_context
CONTEXT_SYMBOLS: dict[str, str] = {"usd_irt": "USD_IRT", "xau_usd": "XAUUSD"}


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


def walk_forward(
    series: pd.Series,
    model_name: str,
    horizon_steps: int,
    min_train: int = MIN_TRAIN_POINTS,
    max_folds: int = MAX_FOLDS,
    context: Optional[dict] = None,
) -> list[Fold]:
    """Expanding-window walk-forward validation (no shuffling).

    Fold ``i``: fit on ``series[:i+1]`` (data known at time i), predict the
    value at ``i + horizon_steps``, compare with the realized value.  A fresh
    model instance is created per fold except models flagged
    ``reuse_across_folds`` (ARIMA/SARIMAX), which reuse their order selection
    from the earliest window (train-only information).  ``context`` carries
    auxiliary point-in-time series for exog-aware models; a model raising
    :class:`ModelUnavailable` (e.g. exog missing) is skipped entirely.
    """
    n = len(series)
    last_now = n - 1 - horizon_steps
    first_now = min_train - 1
    if last_now < first_now:
        return []
    step = max(1, (last_now - first_now) // max_folds + 1)

    reusable = make(model_name)  # ARIMA/SARIMAX benefit from cached order selection
    folds: list[Fold] = []
    for i in range(first_now, last_now + 1, step):
        train = series.iloc[: i + 1]
        try:
            model = reusable if reusable.reuse_across_folds else make(model_name)
            model.set_context(context)
            model.fit(train, horizon_steps)
            pred = float(model.predict_point())
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


def fold_metrics(folds: Sequence[Fold]) -> dict:
    """mae, rmse, smape, directional_accuracy, interval_coverage for folds."""
    if not folds:
        return {}
    preds = np.array([f.pred for f in folds])
    actuals = np.array([f.actual for f in folds])
    bases = np.array([f.base for f in folds])
    errors = actuals - preds
    denom = np.abs(actuals) + np.abs(preds)
    smape = float(np.mean(np.where(denom > 0, 2.0 * np.abs(errors) / denom, 0.0)) * 100.0)
    dir_hits = np.sign(preds - bases) == np.sign(actuals - bases)
    return {
        "n_folds": len(folds),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors**2))),
        "smape": smape,
        "directional_accuracy": float(np.mean(dir_hits)),
        "interval_coverage": walk_forward_coverage(preds.tolist(), actuals.tolist()),
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


def evaluate_candidates(
    series: pd.Series,
    horizon_steps: int,
    candidates: Optional[Sequence[str]] = None,
    context: Optional[dict] = None,
    max_folds: int = MAX_FOLDS,
) -> dict[str, dict]:
    """Walk-forward all candidates on the same folds; add the ensemble.

    Returns ``{model_name: {"folds": [...], "metrics": {...}}}``.
    ``candidates`` defaults to the module-level ``CANDIDATES`` (resolved at
    call time so tests can narrow the set); ``context`` feeds exog-aware
    models (see :func:`walk_forward`); ``max_folds`` lets interactive callers
    (custom horizons) trade validation depth for latency.
    """
    if candidates is None:
        candidates = CANDIDATES
    results: dict[str, dict] = {}
    for name in candidates:
        folds = walk_forward(series, name, horizon_steps, context=context, max_folds=max_folds)
        if folds:
            results[name] = {"folds": folds, "metrics": fold_metrics(folds)}

    naive = results.get("naive")
    if naive:
        naive_smape = naive["metrics"]["smape"]
        member_smapes = {
            name: r["metrics"]["smape"]
            for name, r in results.items()
            if name != "naive" and r["metrics"]["smape"] < naive_smape
        }
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
                results["ensemble"] = {
                    "folds": ens_folds,
                    "metrics": fold_metrics(ens_folds),
                    "weights": weights,
                }
    return results


def select_winner(results: dict[str, dict]) -> str:
    """Lowest sMAPE, but ONLY if it beats naive on the same folds; else naive."""
    if "naive" not in results:
        return min(results, key=lambda n: results[n]["metrics"]["smape"]) if results else ""
    naive_smape = results["naive"]["metrics"]["smape"]
    best_name, best_smape = "naive", naive_smape
    for name, r in results.items():
        if name == "naive":
            continue
        smape = r["metrics"]["smape"]
        if smape < best_smape and smape < naive_smape:
            best_name, best_smape = name, smape
    return best_name


def _build_final_model(
    name: str,
    results: dict[str, dict],
    series: pd.Series,
    horizon_steps: int,
    context: Optional[dict] = None,
) -> ForecastModel:
    """Refit the chosen model on the full series for artifact persistence."""
    if name == "ensemble":
        weights = results["ensemble"]["weights"]
        members = {member: make(member) for member in weights}
        model: ForecastModel = EnsembleModel(members, weights)
    else:
        model = make(name)
    model.set_context(context)
    model.fit(series, horizon_steps)
    return model


def train_all(
    engine: Engine, settings: Settings, horizons: Optional[Sequence[str]] = None
) -> dict:
    """Full training pass: per-horizon evaluation, selection, persistence."""
    requested = [h for h in (horizons or list(HORIZON_SPECS)) if h in HORIZON_SPECS]
    started = utcnow()
    version = started.strftime("%Y-%m-%dT%H:%M:%SZ")

    with engine.begin() as conn:
        run_id = conn.execute(
            training_runs.insert().values(
                started_at=started, status="running", horizons=list(requested),
                models_evaluated=[], selected={},
            )
        ).inserted_primary_key[0]

    summary: dict = {"run_id": int(run_id), "horizons": {}, "selected": {}}
    models_evaluated: list[dict] = []
    selected: dict[str, str] = {}
    notes: list[str] = []
    series_cache: dict[str, pd.Series] = {}
    context_cache: dict[str, dict] = {}
    any_trained = False
    error_msg: Optional[str] = None

    try:
        for horizon in requested:
            freq, steps = HORIZON_SPECS[horizon]
            if freq not in series_cache:
                series_cache[freq] = load_series(engine, "IR_GOLD_18K", freq)
            series = series_cache[freq]

            enabled, reason = horizon_enabled(freq, series)
            if not enabled:
                notes.append(f"{horizon}: disabled ({reason})")
                summary["horizons"][horizon] = {"enabled": False, "reason": reason}
                continue

            if freq not in context_cache:
                context_cache[freq] = {
                    key: load_series(engine, symbol, freq)
                    for key, symbol in CONTEXT_SYMBOLS.items()
                }
            context = context_cache[freq]

            results = evaluate_candidates(series, steps, context=context)
            if not results:
                notes.append(f"{horizon}: no candidate produced folds")
                summary["horizons"][horizon] = {"enabled": False, "reason": "no folds"}
                continue

            winner = select_winner(results)
            selected[horizon] = winner
            any_trained = True
            baseline_metrics = results.get("naive", {}).get("metrics", {})

            final_model = _build_final_model(winner, results, series, steps, context)
            winner_folds = results[winner]["folds"]
            residuals = relative_residuals(
                [f.pred for f in winner_folds], [f.actual for f in winner_folds]
            )
            artifact_dir = os.path.join(settings.models_dir, horizon)
            os.makedirs(artifact_dir, exist_ok=True)
            artifact_path = os.path.join(
                artifact_dir, f"{winner}-{version.replace(':', '')}.joblib"
            )
            joblib.dump(
                {
                    "model": final_model,
                    "model_name": winner,
                    "horizon": horizon,
                    "horizon_steps": steps,
                    "freq": freq,
                    "residual_pcts": residuals,
                    "metrics": results[winner]["metrics"],
                    "trained_at": version,
                },
                artifact_path,
            )

            with engine.begin() as conn:
                conn.execute(
                    update(model_versions)
                    .where(model_versions.c.horizon == horizon)
                    .values(is_active=False)
                )
                winner_id = None
                for name, r in results.items():
                    params: dict = {"horizon_steps": steps, "freq": freq}
                    if name == "ensemble":
                        params["weights"] = r["weights"]
                    row_id = conn.execute(
                        model_versions.insert().values(
                            horizon=horizon,
                            model_name=name,
                            version=version,
                            trained_at=started,
                            training_start=series.index.min().to_pydatetime(),
                            training_end=series.index.max().to_pydatetime(),
                            n_observations=int(len(series)),
                            metrics=r["metrics"],
                            baseline_metrics=baseline_metrics,
                            params=params,
                            artifact_path=artifact_path if name == winner else None,
                            is_active=False,
                        )
                    ).inserted_primary_key[0]
                    if name == winner:
                        winner_id = row_id
                    MODEL_SMAPE.labels(horizon=horizon, model=name).set(
                        r["metrics"]["smape"]
                    )
                    models_evaluated.append(
                        {"horizon": horizon, "model": name, "smape": r["metrics"]["smape"]}
                    )
                if winner_id is not None:
                    conn.execute(
                        update(model_versions)
                        .where(model_versions.c.id == winner_id)
                        .values(is_active=True)
                    )

            summary["horizons"][horizon] = {
                "enabled": True,
                "winner": winner,
                "beats_naive": winner != "naive",
                "metrics": results[winner]["metrics"],
                "baseline_metrics": baseline_metrics,
            }
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
                selected=selected,
                error=error_msg,
                notes="; ".join(notes) if notes else None,
            )
        )
    summary["selected"] = selected
    summary["status"] = status
    if error_msg:
        summary["error"] = error_msg
    if notes:
        summary["notes"] = notes
    return summary
