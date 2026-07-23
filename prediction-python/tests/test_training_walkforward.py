"""Walk-forward validation: time-ordered folds, metrics, winner-vs-naive gate."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.models.training import (
    HORIZON_SPECS,
    MIN_TRAIN_POINTS,
    detect_regime,
    evaluate_candidates,
    fold_metrics,
    horizon_enabled,
    select_winner,
    walk_forward,
)


def _series(values) -> pd.Series:
    index = pd.date_range(
        datetime(2025, 1, 1, tzinfo=timezone.utc), periods=len(values), freq="D"
    )
    return pd.Series(list(values), index=index, dtype=float)


def _rw_series(n=200, seed=5) -> pd.Series:
    rng = np.random.default_rng(seed)
    return _series(100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n))))


def test_walk_forward_folds_time_ordered_and_expanding():
    series = _rw_series(150)
    folds = walk_forward(series, "naive", horizon_steps=1)
    assert folds, "expected folds"
    indices = [f.t_index for f in folds]
    assert indices == sorted(indices)
    assert indices[0] == MIN_TRAIN_POINTS - 1  # min 60 train points
    times = [f.t_time for f in folds]
    assert times == sorted(times)
    # actual is exactly the value horizon steps after t
    for f in folds:
        assert f.actual == pytest.approx(float(series.iloc[f.t_index + 1]))
        assert f.pred == pytest.approx(float(series.iloc[f.t_index]))  # naive


def test_walk_forward_respects_horizon_gap():
    series = _rw_series(140)
    folds = walk_forward(series, "naive", horizon_steps=7)
    assert folds
    assert max(f.t_index for f in folds) <= len(series) - 1 - 7


def test_fold_metrics_shape():
    series = _rw_series(160)
    metrics = fold_metrics(walk_forward(series, "sma", 1))
    for key in ("mae", "rmse", "smape", "directional_accuracy", "interval_coverage",
                "n_folds"):
        assert key in metrics
    assert metrics["smape"] > 0
    assert 0.0 <= metrics["directional_accuracy"] <= 1.0


def _cand(sel: float, hold: float | None = None) -> dict:
    return {
        "metrics": {"smape": sel},
        "sel_metrics": {"smape": sel},
        "holdout_metrics": {"smape": hold} if hold is not None else None,
    }


def test_select_winner_requires_beating_naive():
    results = {
        "naive": _cand(1.0),
        "rf": _cand(1.2),       # worse than naive
        "linear": _cand(1.05),  # worse than naive
    }
    assert select_winner(results) == "naive"

    results["gbr"] = _cand(0.7)
    assert select_winner(results) == "gbr"


def test_select_winner_holdout_confirmation():
    """A selection-fold winner that fails to beat naive on the held-out tail
    falls back to naive (winner-selection bias guard)."""
    results = {
        "naive": _cand(1.0, hold=1.0),
        "gbr": _cand(0.7, hold=1.3),   # great in selection, worse out-of-sample
    }
    assert select_winner(results) == "naive"

    results["gbr"] = _cand(0.7, hold=0.8)  # confirmed on holdout
    assert select_winner(results) == "gbr"


def test_evaluate_candidates_winner_gate_on_random_walk():
    """On a pure random walk nothing should reliably beat naive by much;
    whatever wins must have smape <= naive's on the same folds."""
    series = _rw_series(170, seed=11)
    results = evaluate_candidates(series, 1, candidates=("naive", "sma", "ses"))
    assert "naive" in results
    winner = select_winner(results)
    assert results[winner]["sel_metrics"]["smape"] <= results["naive"]["sel_metrics"]["smape"]


def test_evaluate_candidates_ensemble_only_from_beating_members():
    series = _series(np.linspace(100, 200, 180))  # strong deterministic trend
    results = evaluate_candidates(series, 1, candidates=("naive", "sma", "ses", "linear"))
    if "ensemble" in results:
        naive_smape = results["naive"]["metrics"]["smape"]
        for member in results["ensemble"]["weights"]:
            assert results[member]["metrics"]["smape"] < naive_smape


def test_horizon_enabled_gates():
    short_daily = _rw_series(100)
    ok, reason = horizon_enabled("daily", short_daily)
    assert not ok and "120" in reason

    long_daily = _rw_series(150)
    assert horizon_enabled("daily", long_daily)[0]

    hourly_index = pd.date_range(
        datetime(2026, 7, 1, tzinfo=timezone.utc), periods=5 * 24, freq="h"
    )
    short_hourly = pd.Series(np.ones(len(hourly_index)), index=hourly_index)
    ok, reason = horizon_enabled("hourly", short_hourly)
    assert not ok and "14" in reason

    dense_index = pd.date_range(
        datetime(2026, 6, 1, tzinfo=timezone.utc), periods=15 * 24, freq="h"
    )
    dense_hourly = pd.Series(np.ones(len(dense_index)), index=dense_index)
    assert horizon_enabled("hourly", dense_hourly)[0]

    # daily-resolution data spanning >14d must NOT enable hourly horizons
    sparse_index = pd.date_range(
        datetime(2026, 1, 1, tzinfo=timezone.utc), periods=100, freq="D"
    )
    sparse = pd.Series(np.ones(100), index=sparse_index)
    assert not horizon_enabled("hourly", sparse)[0]


def test_horizon_specs_contract():
    assert set(HORIZON_SPECS) == {"1h", "4h", "eod", "1d", "3d", "7d", "30d"}
    assert HORIZON_SPECS["4h"] == ("hourly", 4)
    assert HORIZON_SPECS["30d"] == ("daily", 30)


def test_detect_regime():
    up = _series(100.0 * np.exp(np.linspace(0, 0.5, 120)))
    assert detect_regime(up) in ("trending_up", "high_volatility")
    down = _series(100.0 * np.exp(np.linspace(0, -0.5, 120)))
    assert detect_regime(down) in ("trending_down", "high_volatility")
    rng = np.random.default_rng(2)
    flat = _series(100.0 + rng.normal(0, 0.3, 120))
    assert detect_regime(flat) in ("ranging", "high_volatility")
    assert detect_regime(_series([1, 2, 3])) == "unknown"
