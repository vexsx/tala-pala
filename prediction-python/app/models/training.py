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
from datetime import datetime, timedelta
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
from .tvinspired import LorentzianKNNModel  # noqa: F401  (registers 'lorentzian_knn', 'kalman_llt')

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


def split_folds(folds: list[Fold]) -> tuple[list[Fold], list[Fold]]:
    """Chronological (selection, holdout) split; holdout empty when too few."""
    if len(folds) < HOLDOUT_MIN_TOTAL:
        return folds, []
    n_hold = max(HOLDOUT_MIN, int(len(folds) * HOLDOUT_FRACTION))
    return folds[:-n_hold], folds[-n_hold:]


def evaluate_candidates(
    series: pd.Series,
    horizon_steps: int,
    candidates: Optional[Sequence[str]] = None,
    context: Optional[dict] = None,
    max_folds: int = MAX_FOLDS,
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
    (custom horizons) trade validation depth for latency.
    """
    if candidates is None:
        candidates = CANDIDATES
    results: dict[str, dict] = {}
    for name in candidates:
        folds = walk_forward(series, name, horizon_steps, context=context, max_folds=max_folds)
        if folds:
            sel, hold = split_folds(folds)
            results[name] = {
                "folds": folds,
                "metrics": fold_metrics(folds),
                "sel_metrics": fold_metrics(sel),
                "holdout_metrics": fold_metrics(hold) if hold else None,
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
                sel, hold = split_folds(ens_folds)
                results["ensemble"] = {
                    "folds": ens_folds,
                    "metrics": fold_metrics(ens_folds),
                    "sel_metrics": fold_metrics(sel),
                    "holdout_metrics": fold_metrics(hold) if hold else None,
                    "weights": weights,
                }
    return results


def report_metrics(r: dict) -> dict:
    """The honest metrics for a candidate: holdout when available, else all."""
    return r.get("holdout_metrics") or r["metrics"]


def select_winner(results: dict[str, dict]) -> str:
    """Lowest selection-fold sMAPE among candidates beating naive there,
    CONFIRMED on the holdout: the chosen non-naive winner must also beat
    naive on the held-out folds, else the fallback is naive."""
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
    if best_name != "naive":
        winner_hold = results[best_name].get("holdout_metrics")
        naive_hold = results["naive"].get("holdout_metrics")
        if winner_hold and naive_hold and winner_hold["smape"] >= naive_hold["smape"]:
            return "naive"
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
                    context_cache[freq] = {
                        key: load_series(engine, ctx_sym, freq)
                        for key, ctx_sym in sym_context_map.items()
                    }
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

                final_model = _build_final_model(winner, results, series, steps, context)
                # Interval residuals from the winner's HOLDOUT folds when
                # enough exist: selection-fold residuals understate true
                # out-of-sample error for the fold-minimizing winner.
                _, winner_hold = split_folds(results[winner]["folds"])
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
                                        "holdout_scored": r.get("holdout_metrics") is not None}
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
