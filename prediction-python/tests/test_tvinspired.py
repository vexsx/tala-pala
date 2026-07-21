"""Tests for the TradingView-community-inspired models (Addendum 6):
Lorentzian kNN, Kalman local-linear-trend, Monte Carlo probabilities."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.models.base import make
from app.models.tvinspired import (
    KalmanTrendModel,
    LorentzianKNNModel,
    mc_probabilities,
)


def _series(values, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D", tz="UTC")
    return pd.Series(np.asarray(values, dtype=float), index=idx)


def _trend(n=200, drift=0.002, vol=0.005, seed=3):
    rng = np.random.RandomState(seed)
    return _series(1000 * np.exp(np.cumsum(drift + vol * rng.randn(n))))


# --- registry ----------------------------------------------------------------

def test_models_are_registered():
    assert isinstance(make("lorentzian_knn"), LorentzianKNNModel)
    assert isinstance(make("kalman_llt"), KalmanTrendModel)


# --- lorentzian knn ----------------------------------------------------------

def test_lorentzian_short_series_degrades_to_naive():
    s = _series(np.linspace(100, 110, 30))
    model = LorentzianKNNModel().fit(s, 5)
    assert model.predict_point() == pytest.approx(float(s.iloc[-1]))


def test_lorentzian_follows_persistent_trend():
    s = _trend(n=250, drift=0.003, vol=0.002)
    model = LorentzianKNNModel().fit(s, 5)
    # in a persistently drifting series, similar past states were followed by
    # gains — the neighbor vote must forecast up
    assert model.predict_point() > float(s.iloc[-1])


def test_lorentzian_deterministic():
    s = _trend(n=220)
    p1 = LorentzianKNNModel().fit(s, 3).predict_point()
    p2 = LorentzianKNNModel().fit(s, 3).predict_point()
    assert p1 == p2


def test_lorentzian_spacing_decorrelates_neighbors():
    # spacing larger than the library collapses the vote to few neighbors —
    # it must still produce a finite forecast, not crash
    s = _trend(n=200)
    model = LorentzianKNNModel(m=10, spacing=50).fit(s, 5)
    assert np.isfinite(model.predict_point())


# --- kalman ------------------------------------------------------------------

def test_kalman_short_series_is_naive():
    s = _series(np.linspace(100, 120, 30))
    model = KalmanTrendModel().fit(s, 5)
    assert model.predict_point() == pytest.approx(float(s.iloc[-1]))


def test_kalman_extrapolates_linear_trend():
    # clean exponential growth: the local linear trend must continue upward
    s = _series(1000 * np.exp(0.002 * np.arange(150)))
    model = KalmanTrendModel().fit(s, 10)
    pred = model.predict_point()
    assert pred > float(s.iloc[-1])
    # and roughly at the extrapolated level (within 2%)
    expected = float(s.iloc[-1]) * np.exp(0.002 * 10)
    assert pred == pytest.approx(expected, rel=0.02)


# --- monte carlo -------------------------------------------------------------

def test_mc_too_short_returns_none():
    assert mc_probabilities(_series(np.linspace(100, 101, 20)), 5, 2.0) is None


def test_mc_probabilities_sane_and_deterministic():
    s = _trend(n=300, drift=0.002, vol=0.01)
    a = mc_probabilities(s, 7, 2.1)
    b = mc_probabilities(s, 7, 2.1)
    assert a == b  # fixed seed -> reproducible
    assert a is not None
    for key in ("p_up", "p_gain_over_cost", "p_loss_over_cost"):
        assert 0.0 <= a[key] <= 1.0
    # positive drift -> upward odds dominate
    assert a["p_up"] > 0.5
    assert a["sim_p05_pct"] < a["sim_median_pct"] < a["sim_p95_pct"]
    # clearing a cost hurdle is strictly harder than just being up
    assert a["p_gain_over_cost"] <= a["p_up"]


def test_mc_cost_scales_probability():
    s = _trend(n=300, drift=0.0, vol=0.01, seed=9)
    cheap = mc_probabilities(s, 7, 0.5)
    expensive = mc_probabilities(s, 7, 5.0)
    assert cheap is not None and expensive is not None
    assert expensive["p_gain_over_cost"] < cheap["p_gain_over_cost"]
