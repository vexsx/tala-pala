"""Tests for the self-learning core: adaptive conformal alpha, the
meta-labeling gate, per-regime live calibration, and exog wiring into the
tabular feature matrix."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.db import predictions, utcnow
from app.jobs.evaluate import compute_live_calibration
from app.models.intervals import (
    ACI_MAX_ALPHA,
    ACI_MIN_ALPHA,
    DEFAULT_ALPHA,
    adaptive_alpha,
    empirical_interval,
)
from app.models.metagate import apply_meta_gate, fit_meta_gate
from app.models.ml import _feature_matrix
from app.models.predicting import blended_confidence


# --- adaptive conformal ------------------------------------------------------

def test_adaptive_alpha_no_evidence_keeps_nominal():
    assert adaptive_alpha(None, 0) == DEFAULT_ALPHA
    assert adaptive_alpha(0.5, 5) == DEFAULT_ALPHA  # below ACI_MIN_N


def test_adaptive_alpha_undercoverage_widens():
    # live coverage 0.75 vs target 0.9 -> smaller alpha -> wider quantiles
    alpha = adaptive_alpha(0.75, 40)
    assert alpha < DEFAULT_ALPHA
    assert alpha >= ACI_MIN_ALPHA


def test_adaptive_alpha_overcoverage_narrows_and_clamps():
    assert adaptive_alpha(1.0, 40) > DEFAULT_ALPHA
    assert adaptive_alpha(1.0, 40) <= ACI_MAX_ALPHA
    assert adaptive_alpha(0.0, 40) == ACI_MIN_ALPHA


def test_adaptive_alpha_changes_interval_width():
    residuals = list(np.random.RandomState(0).normal(0, 0.02, size=200))
    lo_n, hi_n = empirical_interval(100.0, residuals, DEFAULT_ALPHA)
    lo_w, hi_w = empirical_interval(100.0, residuals, adaptive_alpha(0.7, 40))
    assert (hi_w - lo_w) > (hi_n - lo_n)  # under-coverage produced a wider band


# --- meta gate ---------------------------------------------------------------

def _insert_matured(engine, n: int, hit_when_confident: bool = True):
    """Synthetic matured predictions: confident calls hit, unconfident miss."""
    now = utcnow()
    rows = []
    rng = np.random.RandomState(7)
    for i in range(n):
        confident = i % 2 == 0
        base = 1000.0
        point = base * 1.01  # predicted up 1%
        hit = confident if hit_when_confident else not confident
        actual = base * (1.02 if hit else 0.98)
        rows.append(dict(
            symbol="IR_GOLD_18K", horizon="1d", model_name="test",
            predicted_at=now - timedelta(days=n - i), target_time=now - timedelta(days=n - i - 1),
            point_forecast=point, lower_bound=point * 0.98, upper_bound=point * 1.02,
            expected_change_pct=1.0, direction="up",
            confidence=0.8 if confident else 0.3,
            regime="trending_up" if confident else "ranging",
            drivers=[], data_fresh=True, warnings=[],
            actual_value=actual, created_at=now,
        ))
        _ = rng  # deterministic layout; rng kept for future noise
    with engine.begin() as conn:
        for row in rows:
            conn.execute(predictions.insert().values(**row))


def test_fit_meta_gate_needs_enough_samples(engine):
    _insert_matured(engine, 10)
    assert fit_meta_gate(engine) is None


def test_fit_meta_gate_learns_confidence_signal(engine):
    _insert_matured(engine, 80)
    gate = fit_meta_gate(engine)
    assert gate is not None
    assert gate["n"] == 80
    # confident/trending calls hit, unconfident/ranging ones missed: the gate
    # must score the confident profile higher
    p_confident = apply_meta_gate(
        gate, 1010.0, 990.0, 1030.0, 1.0, 0.8, "1d", "trending_up", True)
    p_unconfident = apply_meta_gate(
        gate, 1010.0, 990.0, 1030.0, 1.0, 0.3, "1d", "ranging", True)
    assert p_confident is not None and p_unconfident is not None
    assert p_confident > 0.6
    assert p_unconfident < 0.4


def test_apply_meta_gate_handles_garbage():
    assert apply_meta_gate(None, 1, 0, 2, 1, 0.5, "1d", "ranging", True) is None
    assert apply_meta_gate({"broken": True}, 1, 0, 2, 1, 0.5, "1d", "ranging", True) is None


def test_meta_gate_custom_horizon_string():
    # custom horizons arrive as e.g. "12d" — must parse, not crash
    gate = {
        "mean": [0.0] * 9, "std": [1.0] * 9,
        "coef": [0.0] * 9, "intercept": 0.0,
    }
    p = apply_meta_gate(gate, 1000.0, 990.0, 1010.0, 0.5, 0.5, "12d", "ranging", True)
    assert p == pytest.approx(0.5)


# --- per-regime calibration & confidence -------------------------------------

def test_live_calibration_has_regime_breakdown(engine):
    _insert_matured(engine, 40)
    cal = compute_live_calibration(engine)
    assert "1d" in cal
    by_regime = cal["1d"]["by_regime"]
    assert by_regime["trending_up"]["dir_hit_rate"] == 1.0
    assert by_regime["ranging"]["dir_hit_rate"] == 0.0


def test_blended_confidence_prefers_regime_stats():
    live = {
        "n": 60, "dir_hit_rate": 0.5, "coverage": 0.9,
        "by_regime": {"trending_up": {"n": 30, "dir_hit_rate": 0.9}},
    }
    with_regime = blended_confidence(0.5, live, "trending_up")
    without = blended_confidence(0.5, live, "ranging")  # no ranging stats -> overall
    assert with_regime > without


def test_blended_confidence_ignores_thin_regime_stats():
    live = {
        "n": 60, "dir_hit_rate": 0.5,
        "by_regime": {"high_volatility": {"n": 3, "dir_hit_rate": 1.0}},
    }
    # 3 samples is below MIN_REGIME_N -> falls back to overall stats
    assert blended_confidence(0.5, live, "high_volatility") == blended_confidence(0.5, live, None)


# --- exog features reach the tabular models ----------------------------------

def _series(n=120, seed=1, start="2025-01-01"):
    rng = np.random.RandomState(seed)
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.Series(1000 + np.cumsum(rng.normal(0, 5, n)), index=idx)


def test_feature_matrix_includes_exog_when_context_present():
    gold = _series()
    ctx = {"usd_irt": _series(seed=2) * 100, "xau_usd": _series(seed=3) * 2}
    plain = _feature_matrix(gold)
    rich = _feature_matrix(gold, ctx)
    assert "usd_ret_1" not in plain.columns
    assert {"usd_ret_1", "xau_ret_1", "premium_pct", "premium_z_30"} <= set(rich.columns)
    # raw exog levels must be dropped (scale-free inputs only)
    assert "usd_irt" not in rich.columns and "xau_usd" not in rich.columns


def test_feature_matrix_truncates_future_exog():
    gold = _series(n=60)
    # exog extends 40 days past the last gold point (as in walk-forward folds)
    long_usd = _series(n=100, seed=2)
    ctx = {"usd_irt": long_usd, "xau_usd": _series(n=100, seed=3)}
    frame = _feature_matrix(gold, ctx)
    assert len(frame) == 60
    # the last usd return must equal the one computed from the TRUNCATED series
    truncated = long_usd[long_usd.index <= gold.index[-1]]
    expected = truncated.pct_change().iloc[-1]
    assert frame["usd_ret_1"].iloc[-1] == pytest.approx(expected)
