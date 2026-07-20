"""Model fit/predict on synthetic series, ensemble weights, interval coverage."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.models.base import make
from app.models.baselines import NaiveModel, SMAModel, SESModel
from app.models.ensemble import EnsembleModel, combine, inverse_smape_weights
from app.models.intervals import coverage, empirical_interval, walk_forward_coverage


def _series(values) -> pd.Series:
    index = pd.date_range(
        datetime(2026, 1, 1, tzinfo=timezone.utc), periods=len(values), freq="D"
    )
    return pd.Series(list(values), index=index, dtype=float)


def _trend_series(n=150, start=100.0, step=1.0, seed=1) -> pd.Series:
    rng = np.random.default_rng(seed)
    return _series(start + step * np.arange(n) + rng.normal(0, 0.2, n))


def test_naive_predicts_last_value():
    series = _series([1.0, 2.0, 3.0, 42.0])
    model = NaiveModel().fit(series, horizon=1)
    assert model.predict_point() == 42.0


def test_sma_predicts_window_mean():
    series = _series([1, 2, 3, 4, 5, 10, 20, 30, 40, 50])
    model = SMAModel(k=5).fit(series, horizon=1)
    assert model.predict_point() == pytest.approx(30.0)


def test_ses_holt_follows_trend():
    series = _trend_series()
    model = SESModel().fit(series, horizon=5)
    pred = model.predict_point()
    last = float(series.iloc[-1])
    # Holt should extrapolate the up-trend, at minimum not forecast a collapse
    assert pred > last - 1.0
    assert model.method in ("ses", "holt")


def test_arima_reasonable_on_trend():
    series = _trend_series(n=120)
    model = make("arima")
    model.fit(series, 3)
    pred = model.predict_point()
    assert abs(pred - float(series.iloc[-1])) < 20.0


@pytest.mark.parametrize("name", ["linear", "rf", "gbr"])
def test_ml_models_fit_and_predict(name):
    series = _trend_series(n=160)
    model = make(name)
    model.fit(series, 5)
    pred = model.predict_point()
    last = float(series.iloc[-1])
    assert np.isfinite(pred)
    assert abs(pred / last - 1.0) < 0.25  # sane magnitude
    importances = model.feature_importances()
    assert importances is not None and len(importances) > 3
    total = sum(w for _, w in importances)
    assert total == pytest.approx(1.0, abs=1e-6)


def test_ml_model_degrades_gracefully_on_tiny_series():
    series = _trend_series(n=20)
    model = make("gbr")
    model.fit(series, 1)
    assert model.predict_point() == pytest.approx(float(series.iloc[-1]))


def test_inverse_smape_weights():
    weights = inverse_smape_weights({"a": 1.0, "b": 2.0, "c": 4.0})
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["a"] > weights["b"] > weights["c"]
    # a has half the error of b => double the weight
    assert weights["a"] / weights["b"] == pytest.approx(2.0)


def test_combine_weighted_average():
    assert combine({"a": 10.0, "b": 20.0}, {"a": 0.75, "b": 0.25}) == pytest.approx(12.5)
    with pytest.raises(ValueError):
        combine({"x": 1.0}, {"y": 1.0})


def test_ensemble_model_predicts_weighted_member_average():
    series = _series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    members = {"naive": NaiveModel(), "sma": SMAModel(k=5)}
    ens = EnsembleModel(members, {"naive": 0.5, "sma": 0.5})
    ens.fit(series, 1)
    expected = 0.5 * 10.0 + 0.5 * 8.0  # naive last=10, sma5 mean=8
    assert ens.predict_point() == pytest.approx(expected)


def test_empirical_interval_and_coverage_on_synthetic():
    rng = np.random.default_rng(3)
    preds = np.full(600, 100.0)
    actuals = preds * (1.0 + rng.normal(0, 0.02, size=preds.size))
    residuals = ((actuals - preds) / preds).tolist()

    intervals = [empirical_interval(100.0, residuals, alpha=0.1)] * len(actuals)
    cov = coverage(actuals.tolist(), intervals)
    assert 0.85 <= cov <= 0.95  # ~90% nominal

    wf_cov = walk_forward_coverage(preds.tolist(), actuals.tolist(), alpha=0.1)
    assert 0.80 <= wf_cov <= 0.97


def test_empirical_interval_small_sample_fallback():
    lo, hi = empirical_interval(100.0, [0.001], alpha=0.1)
    assert lo == pytest.approx(95.0)
    assert hi == pytest.approx(105.0)
