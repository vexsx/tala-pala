"""Walk-forward validation: time-ordered folds, metrics, winner-vs-naive gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.models.training import (
    HORIZON_SPECS,
    MIN_TRAIN_POINTS,
    Fold,
    detect_regime,
    evaluate_candidates,
    fold_metrics,
    horizon_enabled,
    select_winner,
    walk_forward,
)


def _t(i: int) -> datetime:
    """Fold timestamp helper (day i of a fixed reference window)."""
    return datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)


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
        "folds": [],  # empty -> bootstrap returns None (untestable, not a veto)
    }


def test_select_winner_requires_beating_naive():
    results = {
        "naive": _cand(1.0),
        "rf": _cand(1.2),       # worse than naive
        "linear": _cand(1.05),  # worse than naive
    }
    assert select_winner(results) == "naive"

    results["gbr"] = _cand(0.7)  # 30% better than naive: clears MIN_EDGE_PCT
    assert select_winner(results) == "gbr"


def test_select_winner_rejects_immaterial_edge():
    """A winner that beats naive by less than MIN_EDGE_PCT is noise, not skill."""
    results = {"naive": _cand(1.0), "rf": _cand(0.995)}  # 0.5% better
    assert select_winner(results) == "naive"


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


# --- Addendum 15: conformal intervals, embargo, significance ------------------

def test_conformal_interval_covers_nominal_level():
    """The 90% band must actually cover ~90% under exchangeability.

    The previous np.quantile implementation measured 0.72-0.78 at the 8-12
    residuals this pipeline produces; the conformal order statistic restores
    the nominal level. Uses a fixed seed so the assertion is deterministic.
    """
    import numpy as np

    from app.models.intervals import conformal_interval

    rng = np.random.default_rng(11)
    for n in (10, 12, 20):
        hits = 0
        trials = 4000
        for _ in range(trials):
            draws = rng.standard_t(df=5, size=n + 1) * 0.01
            lo, hi, diag = conformal_interval(1000.0, draws[:n], 0.1)
            hits += lo <= 1000.0 * (1 + draws[n]) <= hi
            assert diag["coverage_guaranteed"] is True
        assert hits / trials >= 0.88, f"n={n} coverage {hits / trials:.3f}"


def test_conformal_flags_low_evidence_instead_of_pretending():
    from app.models.intervals import conformal_interval

    lo, hi, diag = conformal_interval(1000.0, [0.001, -0.002, 0.003], 0.1)
    assert diag["coverage_guaranteed"] is False
    assert diag["method"] == "conformal_extrapolated"
    # must be at least as wide as the largest observed error
    assert hi - lo >= 2 * 0.003 * 1000.0


def test_split_folds_embargoes_overlapping_targets():
    """With step < horizon_steps, adjacent folds share future data; the gap
    between selection and holdout must remove those overlapping folds."""
    from app.models.training import split_folds

    folds = [
        Fold(t_index=i, t_time=_t(i), base=100.0, pred=100.0, actual=100.0)
        for i in range(40)
    ]
    sel, hold = split_folds(folds, horizon_steps=7, step=1)
    assert len(hold) == 12
    # last selection fold must be >= horizon_steps behind the first holdout fold
    assert hold[0].t_index - sel[-1].t_index >= 7
    # no embargo needed when folds are spaced at least a horizon apart
    sel2, hold2 = split_folds(folds, horizon_steps=1, step=1)
    assert hold2[0].t_index - sel2[-1].t_index == 1


def test_bootstrap_rejects_indistinguishable_candidate():
    """A candidate that is better on average by pure noise must not pass."""
    import numpy as np

    from app.models.training import bootstrap_beats

    rng = np.random.default_rng(5)
    naive, cand = [], []
    for i in range(30):
        a = 100.0 + rng.normal(0, 1)
        naive.append(Fold(i, _t(i), 100.0, 100.0, a))
        cand.append(Fold(i, _t(i), 100.0, 100.0 + rng.normal(0, 1), a))
    assert bootstrap_beats(cand, naive) is False


def test_bootstrap_accepts_clear_winner():
    import numpy as np

    from app.models.training import bootstrap_beats

    rng = np.random.default_rng(6)
    naive, cand = [], []
    for i in range(30):
        a = 100.0 + rng.normal(0, 1)
        naive.append(Fold(i, _t(i), 100.0, 90.0, a))       # badly wrong
        cand.append(Fold(i, _t(i), 100.0, a + 0.05, a))    # nearly exact
    assert bootstrap_beats(cand, naive) is True


def test_fold_metrics_reports_mase_and_nullable_coverage():
    naive_like = [Fold(i, _t(i), 100.0, 100.0, 101.0) for i in range(5)]
    m = fold_metrics(naive_like)
    assert m["mase"] == pytest.approx(1.0)      # exactly naive performance
    assert m["interval_coverage"] is None       # too few folds to score honestly


def test_degenerate_candidate_cannot_win():
    """A model that merely reproduced naive on most folds was never exercised;
    its sMAPE describes naive, not the model that would ship refit on all data."""
    from app.models.training import degenerate_share

    naive = [Fold(i, _t(i), 100.0, 100.0, 101.0) for i in range(20)]
    # identical on 15/20 folds -> 75% degenerate
    cand = [
        Fold(i, _t(i), 100.0, 100.0 if i < 15 else 100.5, 101.0)
        for i in range(20)
    ]
    assert degenerate_share(cand, naive) == pytest.approx(0.75)

    results = {
        "naive": {"metrics": {"smape": 1.0}, "sel_metrics": {"smape": 1.0},
                  "holdout_metrics": {"smape": 1.0}, "folds": naive},
        "rf": {"metrics": {"smape": 0.5}, "sel_metrics": {"smape": 0.5},
               "holdout_metrics": {"smape": 0.5}, "folds": cand},
    }
    assert select_winner(results) == "naive"
    assert "not exercised" in results["rf"]["rejection_reason"]


def test_drop_incomplete_bar_removes_still_filling_bucket():
    """The newest daily bucket is a stub until its UTC day ends."""
    from app.models.predicting import _drop_incomplete_bar
    from app.db import utcnow

    now = utcnow()
    idx = pd.date_range(end=now.replace(hour=0, minute=0, second=0, microsecond=0),
                        periods=40, freq="D", tz="UTC")
    series = pd.Series(range(40), index=idx, dtype=float)
    trimmed, dropped = _drop_incomplete_bar(series, "daily")
    assert dropped == 39.0                      # today's stub removed
    assert len(trimmed) == 39
    # a series whose last bar is a completed past day is untouched
    old = series.iloc[:-1]
    trimmed2, dropped2 = _drop_incomplete_bar(old, "daily")
    assert dropped2 is None and len(trimmed2) == len(old)
