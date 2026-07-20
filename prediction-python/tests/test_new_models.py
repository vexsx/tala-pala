"""Addendum 2 model families: theta, holt_damped, sarimax_exog, quantile_gbr,
hist_gb, knn_analogue — fit/predict on synthetic series, exog guard, native
interval ordering, determinism, ensemble integration."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import app.models.training  # noqa: F401  (imports register every model family)
from app.models.base import ModelUnavailable, available, make
from app.models.baselines import NaiveModel
from app.models.classical import two_theta_forecast
from app.models.ensemble import EnsembleModel
from app.models.sarimax_exog import SarimaxExogModel


def _series(values, start=datetime(2026, 1, 1, tzinfo=timezone.utc)) -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(list(values), index=index, dtype=float)


def _trend_series(n=160, start=100.0, step=1.0, seed=1) -> pd.Series:
    rng = np.random.default_rng(seed)
    return _series(start + step * np.arange(n) + rng.normal(0, 0.2, n))


def _rw_series(n=200, seed=5, drift=0.0005) -> pd.Series:
    rng = np.random.default_rng(seed)
    return _series(100.0 * np.exp(np.cumsum(rng.normal(drift, 0.01, n))))


def _context(gold: pd.Series, seed=9) -> dict:
    """usd/xau auxiliary series aligned with the gold index."""
    rng = np.random.default_rng(seed)
    n = len(gold)
    usd = 100_000.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.005, n)))
    xau = 3_300.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.006, n)))
    return {
        "usd_irt": pd.Series(usd, index=gold.index),
        "xau_usd": pd.Series(xau, index=gold.index),
    }


def test_new_models_are_registered_with_contract_names():
    # exact Addendum 2 names in model_versions.model_name
    assert {"theta", "holt_damped", "sarimax_exog", "quantile_gbr", "hist_gb",
            "knn_analogue"} <= set(available())


# --- theta / holt_damped -----------------------------------------------------


def test_theta_follows_trend():
    series = _trend_series()
    model = make("theta").fit(series, 5)
    pred = model.predict_point()
    last = float(series.iloc[-1])
    assert np.isfinite(pred)
    assert pred > last - 1.0  # extrapolates the up-trend, no collapse


def test_theta_tiny_series_falls_back_to_naive():
    series = _series([1.0, 2.0, 3.0, 42.0])
    model = make("theta").fit(series, 3)
    assert model.predict_point() == pytest.approx(42.0)


def test_two_theta_fallback_reasonable_on_trend():
    values = 100.0 + 1.0 * np.arange(120)
    forecast = two_theta_forecast(values, horizon=5)
    # deterministic trend: the two-theta forecast continues upward
    assert forecast > values[-1]
    assert forecast == pytest.approx(values[-1] + 5.0, rel=0.05)


def test_holt_damped_follows_trend_without_overshoot():
    series = _trend_series()
    model = make("holt_damped").fit(series, 5)
    pred = model.predict_point()
    last = float(series.iloc[-1])
    assert np.isfinite(pred)
    assert pred > last - 1.0
    # damping keeps a 5-step forecast below a wildly amplified trend
    assert pred < last + 5 * 3.0


def test_holt_damped_tiny_series_falls_back_to_naive():
    model = make("holt_damped").fit(_series([5.0, 6.0, 7.0]), 5)
    assert model.predict_point() == pytest.approx(7.0)
    assert model.method == "naive_fallback"


# --- sarimax_exog ------------------------------------------------------------


def test_sarimax_exog_requires_context():
    series = _rw_series(150)
    with pytest.raises(ModelUnavailable):
        make("sarimax_exog").fit(series, 1)


def test_sarimax_exog_requires_nonempty_exog_series():
    series = _rw_series(150)
    model = SarimaxExogModel()
    model.set_context({"usd_irt": pd.Series(dtype=float), "xau_usd": None})
    with pytest.raises(ModelUnavailable):
        model.fit(series, 1)


def test_sarimax_exog_fits_and_predicts_with_context():
    series = _rw_series(160)
    model = SarimaxExogModel()
    model.set_context(_context(series))
    model.fit(series, 3)
    pred = model.predict_point()
    last = float(series.iloc[-1])
    assert np.isfinite(pred)
    assert abs(pred / last - 1.0) < 0.25
    assert model.order is not None  # grid selection happened
    # the selected order is reused on refit (walk-forward contract)
    first_order = model.order
    model.fit(series.iloc[:-10], 3)
    assert model.order == first_order


def test_sarimax_exog_tiny_series_degrades_to_naive():
    series = _rw_series(30)
    model = SarimaxExogModel()
    model.set_context(_context(series))
    model.fit(series, 1)
    assert model.predict_point() == pytest.approx(float(series.iloc[-1]))


# --- quantile_gbr ------------------------------------------------------------


def test_quantile_gbr_native_interval_ordering():
    series = _rw_series(170, seed=3)
    model = make("quantile_gbr").fit(series, 1)
    point = model.predict_point()
    interval = model.predict_interval()
    assert interval is not None
    lower, upper = interval
    assert np.isfinite(lower) and np.isfinite(upper)
    assert lower <= point <= upper  # lo <= mid <= hi, crossing repaired
    importances = model.feature_importances()
    assert importances is not None and len(importances) > 3


def test_quantile_gbr_degrades_without_native_interval():
    series = _trend_series(n=20)
    model = make("quantile_gbr").fit(series, 1)
    assert model.predict_point() == pytest.approx(float(series.iloc[-1]))
    assert model.predict_interval() is None


# --- hist_gb -----------------------------------------------------------------


def test_hist_gb_fits_and_predicts():
    series = _trend_series(n=160)
    model = make("hist_gb").fit(series, 5)
    pred = model.predict_point()
    last = float(series.iloc[-1])
    assert np.isfinite(pred)
    assert abs(pred / last - 1.0) < 0.25


def test_hist_gb_degrades_gracefully_on_tiny_series():
    series = _trend_series(n=20)
    model = make("hist_gb").fit(series, 1)
    assert model.predict_point() == pytest.approx(float(series.iloc[-1]))


# --- knn_analogue ------------------------------------------------------------


def test_knn_analogue_deterministic():
    series = _rw_series(220, seed=13)
    pred_a = make("knn_analogue").fit(series, 3).predict_point()
    pred_b = make("knn_analogue").fit(series, 3).predict_point()
    assert np.isfinite(pred_a)
    assert pred_a == pred_b  # exact: pure numpy, no RNG, stable sort


def test_knn_analogue_sane_magnitude():
    series = _rw_series(220, seed=17)
    pred = make("knn_analogue").fit(series, 5).predict_point()
    last = float(series.iloc[-1])
    assert abs(pred / last - 1.0) < 0.25


def test_knn_analogue_short_series_predicts_last_value():
    series = _series(np.linspace(100, 110, 15))
    model = make("knn_analogue").fit(series, 3)
    assert model.predict_point() == pytest.approx(float(series.iloc[-1]))


def test_knn_analogue_persistent_trend_forecasts_continuation():
    # returns are constant -> every analogue's outcome is the same positive
    # cumulative return, so the forecast must continue upward
    values = 100.0 * np.exp(0.005 * np.arange(160))
    pred = make("knn_analogue").fit(_series(values), 3).predict_point()
    assert pred == pytest.approx(values[-1] * np.exp(0.005 * 3), rel=1e-6)


# --- ensemble + walk-forward integration -------------------------------------


def test_ensemble_drops_unavailable_member():
    series = _rw_series(150)
    members = {"naive": NaiveModel(), "sarimax_exog": SarimaxExogModel()}
    ens = EnsembleModel(members, {"naive": 0.5, "sarimax_exog": 0.5})
    ens.fit(series, 1)  # no context: sarimax member is dropped, not fatal
    assert ens.predict_point() == pytest.approx(float(series.iloc[-1]))


def test_walk_forward_skips_sarimax_without_context():
    from app.models.training import walk_forward

    series = _rw_series(150)
    assert walk_forward(series, "sarimax_exog", 1, context=None) == []


def test_evaluate_candidates_includes_new_members():
    from app.models.training import evaluate_candidates, select_winner

    series = _rw_series(170, seed=11)
    results = evaluate_candidates(
        series, 1,
        candidates=("naive", "theta", "holt_damped", "knn_analogue"),
        context=_context(series),
    )
    assert "naive" in results
    for name in ("theta", "holt_damped", "knn_analogue"):
        assert name in results, name
        assert results[name]["metrics"]["n_folds"] > 0
    winner = select_winner(results)
    assert results[winner]["metrics"]["smape"] <= results["naive"]["metrics"]["smape"]
