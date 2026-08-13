"""Tests for the self-learning core: adaptive conformal alpha, the
meta-labeling gate, per-regime live calibration, and exog wiring into the
tabular feature matrix."""
from __future__ import annotations

from datetime import timedelta

import joblib
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.db import model_versions, predictions, prices, utcnow
from app.jobs.evaluate import compute_live_calibration
from app.models.base import ForecastModel
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
    # (10 features since Addendum 14 added is_global)
    # horizon_rows is what the gate saw per horizon in days: a 12-day horizon
    # is only scorable because this gate has 12-day examples
    gate = {
        "mean": [0.0] * 10, "std": [1.0] * 10,
        "coef": [0.0] * 10, "intercept": 0.0,
        "horizon_evidence": {"IR_GOLD_18K|12.000000": 40},
    }
    p = apply_meta_gate(gate, 1000.0, 990.0, 1010.0, 0.5, 0.5, "12d", "ranging", True)
    assert p == pytest.approx(0.5)


# --- per-regime calibration & confidence -------------------------------------

def test_live_calibration_has_regime_breakdown(engine):
    _insert_matured(engine, 40)
    cal = compute_live_calibration(engine)
    assert "1d" in cal["IR_GOLD_18K"]
    by_regime = cal["IR_GOLD_18K"]["1d"]["by_regime"]
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


# --- Addendum 9: new candidates & short-horizon features ----------------------

def test_new_candidates_registered_and_forecast():
    from app.models.base import make

    s = _series(n=150, seed=4)
    for name in ("extra_trees", "huber"):
        model = make(name)
        model.fit(s, 3)
        assert np.isfinite(model.predict_point()), name


def test_short_horizon_features_present_and_causal():
    from app.features.engineering import compute_feature_frame

    s = _series(n=80, seed=5)
    frame = compute_feature_frame(s)
    for col in ("rsi_7", "streak", "ret_skew_20"):
        assert col in frame.columns, col
    # streak is a signed integer run capped at +-10
    streaks = frame["streak"].dropna()
    assert streaks.abs().max() <= 10
    # rising 3 days in a row must show streak >= 3 at the end
    up = _series(n=40, seed=6)
    up.iloc[-4:] = [100.0, 101.0, 102.0, 103.0]
    assert compute_feature_frame(up)["streak"].iloc[-1] >= 3


# --- Addendum 9 (papers): GARCH-lite features & EvoLearn-tuned HistGB ---------

def test_garch_lite_and_denoised_features():
    from app.features.engineering import compute_feature_frame

    s = _series(n=120, seed=8)
    frame = compute_feature_frame(s)
    for col in ("garch_vol", "garch_vol_ratio_60", "ret_med_5"):
        assert col in frame.columns, col
    # conditional vol is non-negative and finite after warm-up
    tail = frame["garch_vol"].dropna()
    assert (tail >= 0).all() and np.isfinite(tail).all()
    # a volatility spike must raise the ratio above 1
    spiky = s.copy()
    spiky.iloc[-5:] = spiky.iloc[-5:] * np.array([1.0, 1.06, 0.95, 1.07, 0.94])
    ratio = compute_feature_frame(spiky)["garch_vol_ratio_60"].iloc[-1]
    assert ratio > 1.0


def test_tuned_hist_gb_selects_once_and_reuses():
    from app.models.base import make
    from app.models.ml import TunedHistGBModel

    model = make("hist_gb_tuned")
    assert isinstance(model, TunedHistGBModel)
    assert model.reuse_across_folds is True

    s = _series(n=160, seed=9)
    model.fit(s, 3)
    first_params = dict(model._tuned_params or {})
    assert first_params  # selection happened
    assert np.isfinite(model.predict_point())

    # refit on a longer window (walk-forward reuse): params must NOT change
    s2 = _series(n=200, seed=9)
    model.fit(s2, 3)
    assert model._tuned_params == first_params


def test_tuned_hist_gb_artifact_roundtrip(tmp_path):
    import joblib

    from app.models.base import make

    model = make("hist_gb_tuned")
    s = _series(n=160, seed=10)
    model.fit(s, 3)
    path = tmp_path / "tuned.joblib"
    joblib.dump(model, path)
    loaded = joblib.load(path)
    assert np.isfinite(loaded.predict_point())


# --- meta gate: applicability domain -----------------------------------------
# The numbers below are the stored IR_GOLD_18K gate as decomposed in
# production (n=233, base_rate 0.575, intercept 0.331). The coefficients are
# the measured per-feature contributions divided by their z-scores, so the
# fixture reproduces the real logit rather than an invented one.
_GATE_MEAN = [0.1314, 0.6951, 0.3552, 0.5891, 0.95, 0.5150, 0.25, 0.25, 0.25, 0.25]
_GATE_STD = [0.0948, 0.7043, 0.1402, 0.9795, 0.2179, 0.4998, 0.4330, 0.4330, 0.4330, 0.4330]
_GATE_COEF = [
    1.729 / 5.61,      # rel_width
    -1.638 / 2.12,     # abs_expected_pct
    -0.099 / 0.83,     # confidence
    -2.495 / 2.87,     # log_horizon_days
    0.0,               # data_fresh
    -1.106 / -1.03,    # is_global
    0.0, 0.0, 0.0, 0.0,  # regime one-hots (small in the real decomposition)
]


def _production_gate() -> dict:
    from app.models.metagate import FEATURE_NAMES

    return {
        "feature_names": list(FEATURE_NAMES),
        "mean": list(_GATE_MEAN), "std": list(_GATE_STD), "coef": list(_GATE_COEF),
        "intercept": 0.331, "n": 233, "base_rate": 0.5751,
        # horizon evidence, keyed by days: the gate refuses horizons it has no
        # rows for, so a fixture standing in for a real stored gate has to
        # carry what that gate saw (233 rows over 1d/3d/7d/30d)
        "horizon_evidence": {
            "IR_GOLD_18K|1.000000": 121, "IR_GOLD_18K|3.000000": 48,
            "IR_GOLD_18K|7.000000": 41, "IR_GOLD_18K|30.000000": 23,
        },
    }


def _bounds(point: float, rel_width: float) -> tuple[float, float]:
    return point * (1.0 - rel_width / 2.0), point * (1.0 + rel_width / 2.0)


def test_meta_gate_declines_to_score_far_outside_its_training_support():
    """The live IR_GOLD_18K 30d forecast: rel_width 0.663 sits 5.6 SD above the
    0.131 the gate was fitted on, log_horizon_days 2.9 SD above.

    A logistic regression extrapolates linearly and without bound, so it
    reported p_hit ~ 0.05 while its own training pool hit 57.5% of the time
    (and this system's real direction hit rate is 54% on non-flat calls). The
    gate must stay SILENT there — not emit a clamped, nicer-looking number.
    """
    gate = _production_gate()
    lower, upper = _bounds(100.0, 0.6628)
    assert apply_meta_gate(gate, 100.0, lower, upper, 2.1858, 0.4715,
                           "30d", "ranging", True) is None


def test_meta_gate_refusal_is_symmetric_for_implausibly_narrow_intervals():
    """An interval far NARROWER than anything the gate saw is just as much an
    extrapolation as one far wider; the check is on |z|, not on width."""
    gate = _production_gate()
    narrow_lo, narrow_hi = _bounds(100.0, 0.0001)   # ~1.4 SD below... still fine
    assert apply_meta_gate(gate, 100.0, narrow_lo, narrow_hi, 0.5, 0.5,
                           "1d", "ranging", True) is not None

    # push the mean down so a hair-thin interval really is 3+ SD out, mirroring
    # the wide case exactly
    gate["mean"][0] = 0.40      # fitted on wide bands only
    gate["std"][0] = 0.0948
    assert apply_meta_gate(gate, 100.0, narrow_lo, narrow_hi, 0.5, 0.5,
                           "1d", "ranging", True) is None


def test_meta_gate_still_scores_inside_its_support():
    """The refusal must not silence the gate on ordinary forecasts."""
    gate = _production_gate()
    lower, upper = _bounds(100.0, 0.13)   # right at the fitted mean rel_width
    p = apply_meta_gate(gate, 100.0, lower, upper, 0.7, 0.36, "1d", "ranging", True)
    assert p is not None
    assert 0.0 < p < 1.0


def test_meta_gate_publishes_thin_support_instead_of_blending_it_away():
    """CHANGED (was ``test_meta_gate_shrinks_toward_its_base_rate_...``): that
    test pinned the shrink ``(1-s)*p + s*base_rate``, which this round removed.
    It manufactured confidence out of a support failure — at |z| = 2.996 the
    published number was 99.6% a constant — so the behaviour it encoded was
    the defect. Between 2 and 3 SD the score is now the model's own, and the
    thin evidence behind it is published as a separate fact.
    """
    import numpy as np

    from app.models.metagate import score_meta_gate

    gate = _production_gate()
    # rel_width 2.5 SD above the fitted mean; everything else at the mean
    rel_width = _GATE_MEAN[0] + 2.5 * _GATE_STD[0]
    lower, upper = _bounds(100.0, rel_width)
    verdict = score_meta_gate(gate, 100.0, lower, upper, _GATE_MEAN[1], _GATE_MEAN[2],
                              "1d", "ranging", True)
    assert verdict.p_hit is not None

    # the raw logistic this fixture would have produced, computed independently
    feats = [rel_width, _GATE_MEAN[1], _GATE_MEAN[2], float(np.log(1.0)), 1.0, 0.0,
             0.0, 0.0, 1.0, 0.0]
    z = float(np.dot(_GATE_COEF, (np.array(feats) - _GATE_MEAN) / np.array(_GATE_STD)))
    raw = 1.0 / (1.0 + np.exp(-(z + 0.331)))
    assert verdict.p_hit == pytest.approx(raw, abs=1e-9)
    assert verdict.thin_support is True
    assert verdict.max_abs_z == pytest.approx(2.5, abs=1e-6)
    assert verdict.worst_feature == "rel_width"


def test_meta_gate_declines_when_an_indicator_level_was_never_trained_on():
    """A 0/1 feature can never be more than a couple of SD from its mean, so a
    z-rule alone would let the gate score a level it has no examples of."""
    from app.models.metagate import score_meta_gate

    gate = _production_gate()
    gate["mean"][5] = 0.0        # is_global: fitted on Tehran rows only
    # Give XAUUSD its 1-day horizon evidence so the horizon check PASSES and
    # is_global is the binding constraint. Without this the symbol-scoped
    # horizon rule refuses first and the test stops exercising the indicator
    # rule it is named for. (Real fits cannot separate the two — XAUUSD rows
    # at a horizon are exactly what raises the is_global share — which is why
    # this gate is hand-built.)
    gate["horizon_evidence"]["XAUUSD|1.000000"] = 40
    lower, upper = _bounds(100.0, 0.13)
    assert apply_meta_gate(gate, 100.0, lower, upper, 0.7, 0.36, "1d", "ranging",
                           True, "XAUUSD") is None
    verdict = score_meta_gate(gate, 100.0, lower, upper, 0.7, 0.36, "1d", "ranging",
                              True, "XAUUSD")
    assert verdict.worst_feature == "is_global"
    assert verdict.max_abs_z == float("inf")   # absent, not merely far away
    # ... while the level it WAS trained on still scores
    assert apply_meta_gate(gate, 100.0, lower, upper, 0.7, 0.36, "1d", "ranging",
                           True, "IR_GOLD_18K") is not None


def test_gate_verdict_names_the_reason_it_declined():
    """A silent gate must be explainable: 'declined' and 'never trained' are
    different facts and the prediction has to be able to tell them apart."""
    from app.models.metagate import score_meta_gate

    gate = _production_gate()
    lower, upper = _bounds(100.0, 0.6628)
    verdict = score_meta_gate(gate, 100.0, lower, upper, 2.1858, 0.4715,
                              "30d", "ranging", True)
    assert verdict.p_hit is None
    assert verdict.status == "out_of_support"
    assert verdict.worst_feature == "rel_width"
    assert verdict.max_abs_z > 5.0

    assert score_meta_gate(None, 100.0, 99.0, 101.0, 0.5, 0.5, "1d",
                           "ranging", True).status == "untrained"
    assert score_meta_gate({"broken": True}, 100.0, 99.0, 101.0, 0.5, 0.5, "1d",
                           "ranging", True).status == "unusable"


class _FixedPointModel(ForecastModel):
    """Minimal artifact model: forecasts one fixed price (module level so
    joblib can pickle it by reference)."""

    name = "fixed"

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def fit(self, series, horizon):  # noqa: D102 - interface
        return self

    def predict_point(self) -> float:  # noqa: D102 - interface
        return self.value


def _seed_active_model(engine, tmp_path, residual_pcts: list[float]) -> None:
    """80 daily IR_GOLD_18K prices + an active 30d model whose artifact carries
    ``residual_pcts`` (these set the conformal interval width, hence rel_width)."""
    now = utcnow()
    rows = []
    for i in range(80):
        ts = (now - timedelta(days=79 - i)).replace(minute=0, second=0, microsecond=0)
        rows.append(dict(symbol="IR_GOLD_18K", value=100.0 + 0.05 * i, currency="IRT",
                         unit="gram", source="seed", observed_at=ts, collected_at=ts,
                         quality="ok"))
    # last seeded price is 103.95; forecast +1.5% so abs_expected_pct stays
    # INSIDE the gate's support and rel_width is the only thing out of it
    artifact = tmp_path / "fixed.joblib"
    joblib.dump({"model": _FixedPointModel(105.5), "residual_pcts": residual_pcts}, artifact)
    with engine.begin() as conn:
        for row in rows:
            conn.execute(prices.insert().values(**row))
        conn.execute(model_versions.insert().values(
            symbol="IR_GOLD_18K", horizon="30d", model_name="fixed", version="v1",
            trained_at=now, metrics={"directional_accuracy": 0.6, "n_folds": 40},
            baseline_metrics={}, params={}, artifact_path=str(artifact), is_active=True,
        ))


def test_prediction_says_out_loud_when_the_gate_declined_to_score(engine, settings, tmp_path):
    """A gate that stays silent must be VISIBLE.

    Without this, "the gate declined to score this forecast" and "the gate was
    never trained" look identical to a reader — both produce a prediction with
    no self-assessment driver.
    """
    from app.jobs.evaluate import upsert_setting
    from app.models.metagate import META_GATE_KEY
    from app.models.predicting import _predict_one

    # +/-33% residuals -> a ~0.66 rel_width, 5.6 SD outside the gate's support
    _seed_active_model(engine, tmp_path, [0.33, -0.33] * 6)
    upsert_setting(engine, META_GATE_KEY, _production_gate())

    row = _predict_one(engine, settings, "IR_GOLD_18K", "30d", {})
    assert isinstance(row, dict), row
    assert row["direction"] == "up"          # the gate only runs on non-flat calls

    notes = [d["note"] for d in row["drivers"] if d.get("factor") == "self_assessment"]
    assert len(notes) == 1
    assert "declined to score" in notes[0]
    # the note must name the offending feature; it now names it in words a
    # reader of the UI can act on ("this interval width") rather than by its
    # column name (was: "rel_width")
    assert "this interval width" in notes[0]
    assert "confidence left untouched" in notes[0]

    # and the confidence really was left alone (pre-gate == shipped)
    with engine.connect() as conn:
        stored = conn.execute(
            select(predictions.c.confidence, predictions.c.raw_confidence)
        ).all()
    assert len(stored) == 1
    assert float(stored[0][0]) == pytest.approx(float(stored[0][1]))


def test_prediction_distinguishes_an_untrained_gate_from_a_declining_one(engine, settings, tmp_path):
    """No gate at all reads differently from a gate that refused."""
    from app.models.predicting import _predict_one

    _seed_active_model(engine, tmp_path, [0.33, -0.33] * 6)  # no meta_gate stored

    row = _predict_one(engine, settings, "IR_GOLD_18K", "30d", {})
    assert isinstance(row, dict), row
    notes = [d["note"] for d in row["drivers"] if d.get("factor") == "self_assessment"]
    assert len(notes) == 1
    assert "not available yet" in notes[0]
    assert "declined" not in notes[0]


def test_meta_gate_rejects_stale_feature_set():
    """A gate persisted before a feature-set change must stay silent (None)
    instead of scoring a mismatched vector."""
    from app.models.metagate import apply_meta_gate

    old_gate = {
        "feature_names": ["rel_width", "abs_expected_pct"],  # pre-change set
        "mean": [0.0] * 2, "std": [1.0] * 2, "coef": [0.0] * 2, "intercept": 0.0,
    }
    assert apply_meta_gate(old_gate, 1000.0, 990.0, 1010.0, 0.5, 0.5, "1d",
                           "ranging", True) is None


# --- meta gate: support is decided by EVIDENCE, not by a marginal Gaussian ----
# The scheduler only ever emits 1h/4h/eod/1d/3d/7d/30d, so log_horizon_days is
# DISCRETE in training while /internal/predict/custom accepts any 1..90 days.
# A |z| rule cannot see the gaps between the modes: it read 10d (zero training
# rows) as closer to the fit than 30d (31 rows), i.e. it was anti-correlated
# with actual support on the one feature that matters most.

_AUDIT_POOL = {"1d": 194, "eod": 115, "3d": 89, "7d": 71, "30d": 31}  # 500 rows


def _insert_horizon_pool(engine, per_horizon: dict[str, int] = _AUDIT_POOL) -> None:
    """Matured predictions whose only structural difference is the HORIZON.

    Interval width, move size, freshness, symbol and regime cycle identically
    inside every horizon block, so anything the gate refuses here it refuses
    for horizon evidence alone.
    """
    now = utcnow()
    rows = []
    i = 0
    for horizon, count in per_horizon.items():
        for _ in range(count):
            base = 1000.0
            point = base * 1.01                      # predicted +1%
            rel = 0.12 + 0.005 * (i % 5)             # 0.12 .. 0.14
            hit = (i % 3) != 0                       # base rate 2/3
            rows.append(dict(
                symbol="IR_GOLD_18K", horizon=horizon, model_name="test",
                predicted_at=now - timedelta(days=600 - i),
                target_time=now - timedelta(days=599 - i),
                point_forecast=point,
                lower_bound=point * (1.0 - rel / 2.0),
                upper_bound=point * (1.0 + rel / 2.0),
                expected_change_pct=1.0, direction="up",
                confidence=0.30 + 0.01 * (i % 6),
                raw_confidence=0.30 + 0.01 * (i % 6),
                regime=("trending_up", "trending_down", "ranging",
                        "high_volatility")[i % 4],
                drivers=[], data_fresh=True, warnings=[],
                actual_value=base * (1.02 if hit else 0.98), created_at=now,
            ))
            i += 1
    with engine.begin() as conn:
        for row in rows:
            conn.execute(predictions.insert().values(**row))


def _pool_row(horizon: str) -> dict:
    """A forecast identical to the pool's rows except for the horizon."""
    point, rel = 1010.0, 0.13
    return dict(
        point=point, lower=point * (1.0 - rel / 2.0), upper=point * (1.0 + rel / 2.0),
        expected_pct=1.0, confidence=0.33, horizon=horizon, regime="ranging",
        data_fresh=True, symbol="IR_GOLD_18K",
    )


def _score(gate: dict, horizon: str):
    from app.models.metagate import score_meta_gate

    r = _pool_row(horizon)
    return score_meta_gate(gate, r["point"], r["lower"], r["upper"], r["expected_pct"],
                           r["confidence"], r["horizon"], r["regime"], r["data_fresh"],
                           r["symbol"])


def test_meta_gate_scores_a_horizon_only_when_that_horizon_has_evidence(engine):
    """The whole horizon rule, as one biconditional over all 90 day counts.

    Two rounds of this module tried to soften it. Addendum 22's |z| rule scored
    horizons with ZERO examples (10d passed at |z| = 1.62 while 30d, the only
    evidenced long horizon, was refused at 3.35). Addendum 23 then let an
    unevidenced horizon BORROW the nearest evidenced one within a 1.5x radius,
    which is a different way of saying the same untrue thing: a 20-day forecast
    graded on 30-day outcomes is not a measurement of 20-day skill.

    Both are gone, and what replaces them is checkable in one line: the gate
    scores day count N if and only if it has at least
    ``GATE_MIN_LEVEL_ROWS`` matured predictions at exactly N days. Any
    fallback, of any radius, fails this.
    """
    from app.models.metagate import GATE_MIN_LEVEL_ROWS

    _insert_horizon_pool(engine)
    gate = fit_meta_gate(engine)
    assert gate is not None

    # what the gate actually saw is persisted, keyed by DAYS: 1d and eod are
    # the same one-day horizon and pool into one count
    assert gate["horizon_evidence"]["IR_GOLD_18K|1.000000"] == 194 + 115
    assert gate["horizon_evidence"]["IR_GOLD_18K|30.000000"] == 31
    assert "IR_GOLD_18K|10.000000" not in gate["horizon_evidence"]

    scored, declined = [], []
    for days in range(1, 91):
        v = _score(gate, f"{days}d")
        rows = int(gate["horizon_evidence"].get(f"IR_GOLD_18K|{float(days):.6f}", 0))
        evidenced = rows >= GATE_MIN_LEVEL_ROWS
        assert (v.status == "scored") is evidenced, (days, rows, v)
        if not evidenced:
            # and it says WHICH feature failed, so the refusal is readable
            assert v.status == "out_of_support", (days, v)
            assert v.worst_feature == "log_horizon_days", (days, v)
        (scored if evidenced else declined).append(days)

    # the sweep really does exercise both answers (a gate that refused
    # everything, or scored everything, would satisfy a one-sided version)
    assert scored == [1, 3, 7, 30], scored
    assert len(declined) == 86


def test_meta_gate_scores_the_horizon_that_actually_has_evidence(engine):
    """30d — 31 training rows — was the one horizon the z-rule refused."""
    from app.models.metagate import GATE_THIN_Z

    _insert_horizon_pool(engine)
    gate = fit_meta_gate(engine)
    v30 = _score(gate, "30d")
    assert v30.status == "scored"
    assert v30.p_hit is not None
    # and nothing CONTINUOUS is even near the edge here: the old rule refused
    # this forecast on log_horizon_days, a discrete column it cannot read
    assert v30.max_abs_z < GATE_THIN_Z
    assert v30.thin_support is False


def test_meta_gate_scores_an_ordinary_in_support_prediction(engine):
    """Over-refusal is the failure mode a naive fix introduces. An everyday
    1d forecast, right in the middle of the training cloud, must be SCORED."""
    _insert_horizon_pool(engine)
    gate = fit_meta_gate(engine)
    v = _score(gate, "1d")
    assert v.status == "scored"
    assert v.p_hit is not None and 0.0 < v.p_hit < 1.0
    assert v.thin_support is False
    # a score, not the pool's constant hit rate handed back
    assert v.p_hit != pytest.approx(float(gate["base_rate"]), abs=1e-6)


# The largest one-day change in shipped confidence that a forecast may drift by
# WITHOUT the published evidence behind it changing. The natural drift from one
# more day of horizon — a slightly wider interval — is under 2 points on this
# fixture, so 5 leaves room for the forecast to move while an unexplained step
# still fails.
#
# This is NOT a claim that no step exceeds it. There is a step at every evidence
# boundary and it is meant to be there: the gate blends its learned P(direction
# hit) into confidence at a horizon it has matured predictions for, and leaves
# confidence alone at one it does not, so the number necessarily changes between
# 7d and 8d. Measured on the pure gate below, that step is 0.244; measured
# through the real ``predict_custom``, 0.147.
#
# Two earlier rounds tried to make the step small and both made things worse —
# the second one produced a 0.060 step at 19d -> 20d, i.e. LARGER than this
# tolerance, while claiming to have removed the cliff. So the tests here no
# longer assert that no step exceeds the tolerance. They assert that every step
# which does is one the reader was told about.
MAX_ONE_DAY_CONFIDENCE_JUMP = 0.05


def _gate_shipped_confidence(gate: dict, days: int, confidence: float) -> tuple:
    """``(shipped_confidence, verdict)`` for a fixed forecast at ``days``.

    Mirrors what both prediction paths do with the verdict: a scored gate is
    blended 50/50 into confidence, a declining one leaves it untouched.
    """
    import numpy as np

    from app.models.metagate import score_meta_gate

    point, rel = 1010.0, 0.13
    verdict = score_meta_gate(
        gate, point, point * (1.0 - rel / 2.0), point * (1.0 + rel / 2.0),
        1.0, confidence, f"{days}d", "ranging", True, "IR_GOLD_18K")
    if verdict.p_hit is None:
        return confidence, verdict
    return float(np.clip(0.5 * confidence + 0.5 * verdict.p_hit, 0.05, 0.95)), verdict


def test_gate_confidence_steps_only_where_the_evidence_changes(engine):
    """Sweep a contiguous day range that PROVABLY straddles evidence boundaries.

    This is the third version of this test, and the first two both passed by
    choosing their window. Version one swept ``range(8, 25)``, where every day
    count is unevidenced, so "nothing moves" was true before the defect, during
    it and after. Version two swept ``range(1, 11)`` and ``range(20, 46)``,
    which were exactly the two maximal islands the borrow rule scored, so
    "nothing is declined" held by construction and the jump assertion only ever
    measured drift inside an island. Executed at the time: ``range(1, 11)``
    passed and ``range(1, 12)`` failed, on steps of 0.24 — five times the
    tolerance — that the chosen window simply excluded.

    So this version asserts no such thing. A step at an evidence boundary is
    expected and correct; it is where the system stops having a measurement of
    its own skill. What is asserted is that every step is ACCOUNTED FOR:

    1. the window contains both a scored day and a declined one, checked
       explicitly, so narrowing it to dodge a boundary fails here first;
    2. every step larger than the tolerance coincides with a change of gate
       status;
    3. every such step is disclosed — the two days' ``self_assessment``
       drivers differ, and say which of "scored" / "declined" happened;
    4. inside a run of same-status days the number stays put.
    """
    from app.models.predicting import _gate_driver

    _insert_horizon_pool(engine)          # evidence at 1, 3, 7 and 30 days
    gate = fit_meta_gate(engine)

    window = range(1, 47)                 # contiguous, and unchosen: 1..46
    shipped, verdicts = {}, {}
    for d in window:
        shipped[d], verdicts[d] = _gate_shipped_confidence(gate, d, confidence=0.33)

    # 1) the window provably contains a boundary
    scored = [d for d in window if verdicts[d].status == "scored"]
    declined = [d for d in window if verdicts[d].status != "scored"]
    assert scored and declined, (scored, declined)
    assert any(abs(a - b) == 1 for a in scored for b in declined), (
        f"window {window} contains no adjacent scored/declined pair")

    boundaries = []
    for a, b in zip(list(window), list(window)[1:]):
        step = abs(shipped[b] - shipped[a])
        same_status = verdicts[a].status == verdicts[b].status
        if step > MAX_ONE_DAY_CONFIDENCE_JUMP:
            # 2) a big step happens only where the evidence changes
            assert not same_status, (
                f"{step:.3f} step between {a}d and {b}d with the gate "
                f"{verdicts[a].status} on both sides; shipped={shipped}")
            # 3) and the reader is told, in the driver, on both sides
            note_a = _gate_driver(verdicts[a], gate)["note"]
            note_b = _gate_driver(verdicts[b], gate)["note"]
            assert note_a != note_b, (a, b, note_a)
            for d, note in ((a, note_a), (b, note_b)):
                if verdicts[d].status == "scored":
                    assert "learned P(direction hit)" in note, (d, note)
                else:
                    assert "declined to score this forecast" in note, (d, note)
                    assert "this forecast horizon" in note, (d, note)
                    assert "confidence left untouched" in note, (d, note)
            boundaries.append((a, b))
        elif same_status:
            # 4) no drift inside a run
            assert step <= MAX_ONE_DAY_CONFIDENCE_JUMP, (a, b, step)

    # every scored day in the interior of the window contributes two boundaries
    assert boundaries, "no boundary was exercised"


def test_gate_refuses_genuine_horizon_extrapolation(engine):
    """A day count this system has never been graded on gets no score at all.

    Kept from the borrow era with its rationale intact but its frontier gone:
    there is no radius any more, so 8d is refused for the same reason 90d is —
    zero matured predictions of its own.
    """
    _insert_horizon_pool(engine)          # evidence at 1, 3, 7 and 30 days
    gate = fit_meta_gate(engine)
    for days in (2, 4, 8, 10, 11, 12, 15, 19, 20, 45, 46, 60, 90):
        v = _score(gate, f"{days}d")
        assert v.status == "out_of_support", (days, v)
        assert v.worst_feature == "log_horizon_days", (days, v)
        assert v.p_hit is None, (days, v)

    # pinning only the refusals would be satisfied by a gate that refuses
    # everything, so pin the other side too
    for days in (1, 3, 7, 30):
        assert _score(gate, f"{days}d").status == "scored", days


def test_a_declined_horizon_is_disclosed_and_leaves_confidence_alone(engine):
    """The refusal is a published fact, and it costs the user nothing silently.

    Replaces ``test_borrowed_horizon_is_disclosed_...``. There is no
    substitution left to disclose; what has to be visible now is the decline
    itself, and that the number the user sees was not touched by it.
    """
    from app.models.predicting import _gate_driver

    _insert_horizon_pool(engine)
    gate = fit_meta_gate(engine)

    v8 = _score(gate, "8d")
    assert v8.status == "out_of_support" and v8.p_hit is None
    note8 = _gate_driver(v8, gate)["note"]
    assert "declined to score this forecast" in note8, note8
    assert "this forecast horizon never appears in" in note8, note8
    assert "confidence left untouched" in note8, note8
    # and it never claims a neighbour's evidence
    assert "nearest horizon" not in note8, note8

    # a declining gate returns the caller's own confidence, unmodified
    conf = 0.33
    shipped, verdict = _gate_shipped_confidence(gate, 8, conf)
    assert verdict.p_hit is None
    assert shipped == pytest.approx(conf)

    # while the evidenced neighbour is scored and says so
    note7 = _gate_driver(_score(gate, "7d"), gate)["note"]
    assert "learned P(direction hit)" in note7, note7


def test_meta_gate_never_substitutes_its_base_rate_for_a_score():
    """Between 2 and 3 SD the old code returned (1-s)*p + s*base_rate with
    s -> 1, so at |z| = 2.9 the published number was 89% a constant that
    contains nothing about this forecast — status "scored", p_hit not None,
    and it MANUFACTURED confidence (raw 0.08 -> 0.53) exactly where the
    evidence had failed."""
    import numpy as np

    gate = _production_gate()
    # a 2.74% expected move: 2.9 SD above the fitted mean, thin but inside the
    # domain — and on a NEGATIVE coefficient, so the raw score is far below the
    # base rate, the case where the shrink manufactured confidence
    expected_pct = _GATE_MEAN[1] + 2.9 * _GATE_STD[1]
    lower, upper = _bounds(100.0, _GATE_MEAN[0])
    from app.models.metagate import score_meta_gate

    verdict = score_meta_gate(gate, 100.0, lower, upper, expected_pct, _GATE_MEAN[2],
                              "1d", "ranging", True)
    feats = [_GATE_MEAN[0], expected_pct, _GATE_MEAN[2], 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    z = float(np.dot(_GATE_COEF, (np.array(feats) - _GATE_MEAN) / np.array(_GATE_STD)))
    raw = 1.0 / (1.0 + np.exp(-(z + 0.331)))
    assert raw < 0.2 and float(gate["base_rate"]) > 0.55   # fixture sanity

    assert verdict.p_hit == pytest.approx(raw, abs=1e-9)   # the model's own answer
    assert abs(verdict.p_hit - float(gate["base_rate"])) > 0.4   # NOT the base rate
    # thin support is a fact about the forecast, so it is published, not
    # smuggled into the number
    assert verdict.thin_support is True
    assert verdict.status == "scored"


def test_gate_with_truncated_coefficients_is_unusable_not_a_crash():
    """A stored gate lacking feature_names with a short mean/std vector used to
    raise IndexError out of the support check (not caught), killing the whole
    prediction run instead of leaving the gate silent."""
    from app.models.metagate import score_meta_gate

    stale = {
        "mean": [0.0] * 3, "std": [1.0] * 3, "coef": [0.0] * 3,
        "intercept": 0.0, "n": 100, "base_rate": 0.5,
        "horizon_evidence": {"IR_GOLD_18K|1.000000": 100},
    }
    verdict = score_meta_gate(stale, 100.0, 99.0, 101.0, 0.5, 0.5, "1d", "ranging", True)
    assert verdict.status == "unusable"
    assert verdict.p_hit is None


# --- the two prediction paths must agree on what "confidence" MEANS ----------
# ``_predict_one`` blends the validation heuristic toward live outcomes and
# persists THAT as ``raw_confidence``, which is the feature ``fit_meta_gate``
# trains the ``confidence`` column on. ``custom.predict_custom`` handed the gate
# the UNBLENDED heuristic. The gate then z-scored one scale against the other
# and refused every custom forecast — on horizons backed by hundreds of rows —
# while the driver told the user their confidence was many SD abnormal.
#
# The fixture below is internally consistent the way production is: every
# stored ``raw_confidence`` is computed from that row's own interval width
# through ``_confidence`` and then ``blended_confidence`` against the live
# calibration the evaluate job would have written.

_POOL_REL_WIDTHS = (0.001, 0.004, 0.01, 0.03, 0.06, 0.10, 0.16, 0.25)
_POOL_MOVES = (0.2, 0.5, 1.0, 2.0, 3.5, 5.0, 7.0)
_POOL_DIR_ACCS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90)
REGIMES_CYCLE = ("trending_up", "trending_down", "ranging", "high_volatility")


def _seed_custom_prices(engine, n: int = 300) -> None:
    """A smooth IRT-gold-like drift: enough history for a 45-day custom
    request, and tight enough that the fast candidates produce the narrow
    intervals (hence high validation confidence) they produce in production."""
    now = utcnow()
    rng = np.random.RandomState(11)
    values = 1000.0 * np.power(1.002, np.arange(n)) * (1.0 + rng.normal(0, 0.0005, n))
    with engine.begin() as conn:
        for i, v in enumerate(values):
            ts = (now - timedelta(days=n - i)).replace(minute=0, second=0, microsecond=0)
            conn.execute(prices.insert().values(
                symbol="IR_GOLD_18K", source="seed", observed_at=ts, collected_at=ts,
                value=float(v), currency="IRT", unit="gram", quality="ok"))


def _seed_blended_pool(engine) -> dict:
    """Matured predictions stored the way ``_predict_one`` stores them.

    Returns the fitted gate. Interval widths and move sizes span the range the
    day sweeps visit, so ``rel_width`` and ``abs_expected_pct`` are never the
    reason for a refusal and the confidence scale is isolated.
    """
    from sqlalchemy import update

    from app.jobs.evaluate import compute_live_calibration, upsert_setting
    from app.models.metagate import META_GATE_KEY
    from app.models.predicting import (
        LIVE_CAL_KEY,
        _confidence,
        blended_confidence,
        load_live_calibration,
    )

    now = utcnow()
    i = 0
    with engine.begin() as conn:
        for horizon, count in _AUDIT_POOL.items():
            for k in range(count):
                base, move = 1000.0, _POOL_MOVES[i % len(_POOL_MOVES)]
                point = base * (1.0 + move / 100.0)
                rel = _POOL_REL_WIDTHS[i % len(_POOL_REL_WIDTHS)]
                hit = (i % 5) != 0 and (i % 7) != 0            # ~0.69 base rate
                conn.execute(predictions.insert().values(
                    symbol="IR_GOLD_18K", horizon=horizon, model_name="test",
                    predicted_at=now - timedelta(days=900 - i),
                    target_time=now - timedelta(days=899 - i),
                    point_forecast=point,
                    lower_bound=point * (1.0 - rel / 2.0),
                    upper_bound=point * (1.0 + rel / 2.0),
                    expected_change_pct=move, direction="up",
                    confidence=0.5, raw_confidence=0.5,        # rewritten below
                    # live calibration windows the newest 60 rows per horizon;
                    # one regime there puts the blend on n=60 of evidence
                    # (w = 0.3), which is the production configuration
                    regime=("ranging" if k >= count - 60 else REGIMES_CYCLE[i % 4]),
                    drivers=[], data_fresh=(i % 4 != 0), warnings=[],
                    actual_value=base * (1.0 + (move if hit else -move) / 100.0),
                    created_at=now))
                i += 1

    upsert_setting(engine, LIVE_CAL_KEY, compute_live_calibration(engine))
    cal_all = load_live_calibration(engine)
    with engine.begin() as conn:
        rows = conn.execute(select(
            predictions.c.id, predictions.c.horizon, predictions.c.lower_bound,
            predictions.c.upper_bound, predictions.c.point_forecast,
            predictions.c.regime)).all()
        for pid, horizon, lo, hi, pt, regime in rows:
            rel_width = (float(hi) - float(lo)) / float(pt)
            val_conf = _confidence(_POOL_DIR_ACCS[pid % len(_POOL_DIR_ACCS)], rel_width)
            blended = blended_confidence(
                val_conf, (cal_all.get("IR_GOLD_18K") or {}).get(horizon), regime)
            conn.execute(update(predictions).where(predictions.c.id == pid)
                         .values(confidence=blended, raw_confidence=blended))

    gate = fit_meta_gate(engine)
    assert gate is not None
    upsert_setting(engine, META_GATE_KEY, gate)
    return gate


def test_custom_confidence_is_on_the_scale_the_gate_was_trained_on(
    engine, settings, monkeypatch
):
    """The custom path fed the gate a confidence the gate had never seen.

    Reproduced before the fix on this exact fixture: the gate's ``confidence``
    feature was mean 0.686 / sd 0.043 (it is trained on the BLENDED value),
    ``predict_custom`` handed it the raw 0.95, and the gate refused at
    |z| = 6.1 — on a 1-day horizon backed by 309 training rows — telling the
    user their confidence level was abnormal. It was the application's two
    halves disagreeing, not the forecast.
    """
    import app.models.custom as custom_mod
    from app.models.metagate import FEATURE_NAMES
    from app.models.predicting import _confidence, blended_confidence

    _seed_custom_prices(engine)
    gate = _seed_blended_pool(engine)
    monkeypatch.setattr(custom_mod, "FAST_CANDIDATES", ("naive", "linear", "theta"))

    out = custom_mod.predict_custom(engine, settings, 7)
    assert out["direction"] != "flat", out["expected_change_pct"]

    # 1) the gate scored it — a horizon with 71 training rows of its own
    note = [d["note"] for d in out["drivers"] if d.get("factor") == "self_assessment"]
    assert len(note) == 1
    assert "learned P(direction hit)" in note[0], note[0]
    assert "this confidence level" not in note[0], note[0]

    # 2) the shipped confidence really is the blended quantity, and it lands
    #    inside the distribution the gate's confidence column was fitted on
    ci = FEATURE_NAMES.index("confidence")
    mean, std = float(gate["mean"][ci]), float(gate["std"][ci])
    dir_acc = float(out["metrics"]["directional_accuracy"])
    rel_width = (out["upper_bound"] - out["lower_bound"]) / out["point_forecast"]
    raw = _confidence(dir_acc, rel_width)
    live = (compute_live_calibration(engine).get("IR_GOLD_18K") or {}).get("7d")
    expected = blended_confidence(raw, live, out["regime"])
    z_raw = abs(raw - mean) / std
    z_blended = abs(expected - mean) / std
    assert z_raw > 3.0, f"fixture no longer reproduces the defect (|z_raw|={z_raw:.1f})"
    assert z_blended < 3.0, f"blended confidence still out of support (|z|={z_blended:.1f})"

    # 3) and the number the user is shown is that one, 50/50 with the gate
    shipped = 0.5 * expected + 0.5 * _gate_p_hit(note[0])
    assert float(out["confidence"]) == pytest.approx(shipped, abs=0.02)

    # 4) which live outcomes calibrated it is published, not assumed
    cal = [d["note"] for d in out["drivers"] if d.get("factor") == "confidence_calibration"]
    assert len(cal) == 1
    assert "matured 7d prediction(s)" in cal[0], cal[0]


def _gate_p_hit(note: str) -> float:
    return float(note.split("learned P(direction hit)=")[1][:4])


def test_custom_shipped_confidence_moves_only_where_published_evidence_does(
    engine, settings, monkeypatch
):
    """The user-visible number, through the real endpoint path.

    Same property as the gate-level sweep, one level up and with one more thing
    allowed to move it: this path runs a model tournament per horizon, so the
    WINNING MODEL can change from one day count to the next and take the
    interval width — hence the validation confidence — with it. Measured on
    this fixture: 37d and 38d are both declined by the gate, yet shipped
    confidence steps 0.802 -> 0.873 because the winner changes from ``linear``
    (rel_width 0.0788) to ``ensemble`` (0.0394). That is a different forecast,
    not a different rule, and ``model_name`` publishes it.

    So the property asserted is: every step larger than the tolerance is
    accompanied by a change the response itself reports — the gate's status, or
    the model that produced the forecast — and never by neither.
    """
    import app.models.custom as custom_mod

    _seed_custom_prices(engine)
    _seed_blended_pool(engine)
    monkeypatch.setattr(custom_mod, "FAST_CANDIDATES", ("naive", "linear", "theta"))

    window = range(5, 11)   # 7d is evidenced; 5, 6, 8, 9, 10 are not
    obs = {}
    for days in window:
        out = custom_mod.predict_custom(engine, settings, days)
        drv = {d["factor"]: d.get("note", "") for d in out["drivers"] if "factor" in d}
        sa = drv["self_assessment"]
        obs[days] = {
            "conf": float(out["confidence"]),
            "status": "scored" if "learned P(direction hit)" in sa else "declined",
            "model": out["model_name"],
            "self_assessment": sa,
            "calibration": drv["confidence_calibration"],
        }

    # 1) the window provably straddles a boundary — narrowing it to a run of
    #    same-status days (which is how the previous two versions passed) fails
    #    right here
    statuses = {o["status"] for o in obs.values()}
    assert statuses == {"scored", "declined"}, obs
    assert obs[7]["status"] == "scored" and obs[8]["status"] == "declined"

    days = sorted(obs)
    for a, b in zip(days, days[1:]):
        step = abs(obs[b]["conf"] - obs[a]["conf"])
        if step <= MAX_ONE_DAY_CONFIDENCE_JUMP:
            continue
        # 2) something the reader can see changed
        changed = (obs[a]["status"] != obs[b]["status"]
                   or obs[a]["model"] != obs[b]["model"])
        assert changed, (
            f"{step:.3f} step between {a}d and {b}d with the same gate status "
            f"({obs[a]['status']}) and the same model ({obs[a]['model']})")
        # 3) and if it was the evidence, both drivers say so
        if obs[a]["status"] != obs[b]["status"]:
            for d in (a, b):
                sa, cal = obs[d]["self_assessment"], obs[d]["calibration"]
                if obs[d]["status"] == "scored":
                    assert "learned P(direction hit)" in sa, (d, sa)
                    assert cal.startswith("calibrated against"), (d, cal)
                else:
                    assert "declined to score this forecast" in sa, (d, sa)
                    assert "this forecast horizon" in sa, (d, sa)
                    assert "not calibrated against live outcomes" in cal, (d, cal)

    # 4) inside the declined run the number only drifts
    for a, b in ((8, 9), (9, 10), (5, 6)):
        assert obs[a]["status"] == obs[b]["status"] == "declined"
        assert obs[a]["model"] == obs[b]["model"]
        assert abs(obs[b]["conf"] - obs[a]["conf"]) <= MAX_ONE_DAY_CONFIDENCE_JUMP, (
            a, b, obs[a]["conf"], obs[b]["conf"])


def test_live_calibration_never_borrows_another_horizons_outcomes():
    """``live_calibration_for_days`` fell back to the nearest horizon UNBOUNDED.

    Reproduced on the shipped function before deletion: on a deployment whose
    first four hours had matured, a **90-day** forecast was calibrated on the
    **1-hour** directional hit rate — 2160x out — and because
    :func:`blended_confidence` floors its weight at ``w = 0.3``, seventy
    percent of that 90-day shipped confidence was the one-hour number. With a
    90% 1h hit rate the 90-day forecast shipped 0.810; with a 35% one, 0.425.
    Same forecast, and neither number contains anything about 90 days.

    There is now one lookup for both paths and it is exact-horizon.
    """
    from app.models.predicting import blended_confidence, live_calibration_for

    only_1h = {"IR_GOLD_18K": {"1h": {"n": 60, "dir_hit_rate": 0.9, "coverage": 0.9}}}
    for days in range(1, 91):
        assert live_calibration_for(only_1h, "IR_GOLD_18K", f"{days}d") is None, days

    # so the validation heuristic ships unchanged rather than 70% one-hour
    val = 0.60
    assert blended_confidence(
        val, live_calibration_for(only_1h, "IR_GOLD_18K", "90d"), "ranging"
    ) == pytest.approx(val)

    # and an exact match is still used
    cal = {"IR_GOLD_18K": {"7d": {"n": 60, "dir_hit_rate": 0.9, "coverage": 0.9}}}
    assert live_calibration_for(cal, "IR_GOLD_18K", "7d") == cal["IR_GOLD_18K"]["7d"]


def test_both_prediction_paths_read_the_legacy_flat_calibration_layout():
    """One lookup, so the pre-multi-symbol layout cannot split the two paths.

    ``live_calibration_for`` falls back to the flat ``{horizon: block}`` layout
    for the primary symbol; the deleted ``live_calibration_for_days`` did not,
    so on a database written before Addendum 8 the scheduled 7d forecast
    shipped 0.915 (calibrated) while the custom 7-day forecast shipped 0.950
    (uncalibrated) — the exact class of disagreement Addendum 23 claimed to
    have closed.
    """
    from app.models.predicting import blended_confidence, live_calibration_for

    legacy = {"7d": {"n": 60, "dir_hit_rate": 0.90, "coverage": 0.9}}
    scheduled = live_calibration_for(legacy, "IR_GOLD_18K", "7d")
    custom = live_calibration_for(legacy, "IR_GOLD_18K", f"{7}d")
    assert scheduled == custom == legacy["7d"]
    assert blended_confidence(0.95, scheduled, "ranging") == pytest.approx(
        blended_confidence(0.95, custom, "ranging"))


def test_custom_path_is_self_consistent_about_evidence(engine, settings, monkeypatch):
    """Calibrated against live outcomes IF AND ONLY IF the gate scores it.

    This is what makes the exact-horizon rule safe rather than merely strict.
    The gate's ``confidence`` column is fitted on BLENDED confidences, so an
    unblended one is off the scale it was trained on; if a horizon could lose
    its calibration while keeping its gate score, the gate would be reading a
    number it had never seen. It cannot, because both facts come from the same
    place — matured predictions at that exact horizon. A horizon with none gets
    neither, and says so twice.
    """
    import app.models.custom as custom_mod

    _seed_custom_prices(engine)
    _seed_blended_pool(engine)            # matured rows at 1, 3, 7 and 30 days
    monkeypatch.setattr(custom_mod, "FAST_CANDIDATES", ("naive", "linear", "theta"))

    seen = set()
    for days in (3, 12, 30, 45):
        out = custom_mod.predict_custom(engine, settings, days)
        drv = {d["factor"]: d.get("note", "") for d in out["drivers"] if "factor" in d}
        scored = "learned P(direction hit)" in drv["self_assessment"]
        calibrated = drv["confidence_calibration"].startswith("calibrated against")
        assert scored is calibrated, (days, drv)
        if not scored:
            assert "declined to score this forecast" in drv["self_assessment"], days
            assert "this forecast horizon" in drv["self_assessment"], days
            assert f"no {days}-day predictions" in drv["confidence_calibration"], days
            assert ("no other horizon's outcomes were substituted"
                    in drv["confidence_calibration"]), days
        else:
            assert f"matured {days}d prediction(s)" in drv["confidence_calibration"]
        seen.add(scored)
    assert seen == {True, False}, seen   # both branches were actually exercised


def test_custom_forecast_publishes_a_measured_or_explained_coverage_rate(
    engine, settings, monkeypatch
):
    """``interval_coverage_walk_forward.rate`` was structurally always null.

    ``CUSTOM_MAX_FOLDS`` was 25 and ``walk_forward_coverage`` burns its first
    10 folds on the residual pool, so the block could score at most 15 against
    ``MIN_SCORED_FOR_COVERAGE`` = 20 — for every horizon and every candidate,
    forever. A null a caller can never see filled is a bug wearing a field's
    clothes; either it carries a measurement, or it says why it does not.
    """
    import app.models.custom as custom_mod
    from app.models.intervals import MIN_SCORED_FOR_COVERAGE

    _seed_custom_prices(engine)
    monkeypatch.setattr(custom_mod, "FAST_CANDIDATES", ("naive", "linear", "theta"))

    out = custom_mod.predict_custom(engine, settings, 7)
    cov = out["metrics"]["interval_coverage_walk_forward"]
    if cov["rate"] is None:
        assert cov["scored_folds"] < MIN_SCORED_FOR_COVERAGE
        assert any("does not carry a measured interval-coverage rate" in w
                   for w in out["warnings"]), out["warnings"]
    else:
        assert cov["status"] == "measured"
        assert cov["scored_folds"] >= MIN_SCORED_FOR_COVERAGE
        assert 0.0 <= cov["rate"] <= 1.0
        assert not any("does not carry a measured interval-coverage rate" in w
                       for w in out["warnings"]), out["warnings"]

    # on an ordinary request with ordinary history it must actually be measured
    assert cov["status"] == "measured", cov


def test_custom_forecast_publishes_a_self_assessment_driver(engine, settings, monkeypatch):
    """The custom path blended the gate into shipped confidence while emitting
    NO driver, so a reader could not tell a scored gate from a declined one
    from an absent one.

    CHANGED: this used to run at 10 days against ``_insert_horizon_pool`` and
    assert "declined", which passed for the wrong reason — that fixture stores
    an artificial 0.30..0.35 confidence band while the custom path produces
    ~0.95, so the refusal it observed was the confidence-scale mismatch, not
    the horizon. It now runs on the internally-consistent pool at 60 days,
    where the confidence is on the fitted scale and the horizon is the only
    thing wrong with the request.
    """
    import app.models.custom as custom_mod

    _seed_custom_prices(engine)
    _seed_blended_pool(engine)

    # keep the interactive path quick and deterministic
    monkeypatch.setattr(custom_mod, "FAST_CANDIDATES", ("naive", "linear", "theta"))
    out = custom_mod.predict_custom(engine, settings, 60)
    assert out["direction"] != "flat", out["expected_change_pct"]
    notes = [d for d in out["drivers"] if d.get("factor") == "self_assessment"]
    assert len(notes) == 1, out["drivers"]
    assert "declined" in notes[0]["note"]
    # and it declines on the HORIZON, not on a confidence the app itself skewed
    assert "this forecast horizon" in notes[0]["note"], notes[0]["note"]


def _insert_symbol_pool(engine, per_symbol_horizon: dict) -> None:
    """Matured rows keyed by (symbol, horizon) — same shape as the horizon pool."""
    now = utcnow()
    i = 0
    rows = []
    for (symbol, horizon), count in per_symbol_horizon.items():
        for _ in range(count):
            base = 1000.0
            point = base * 1.01
            rel = 0.12 + 0.005 * (i % 5)
            hit = (i % 3) != 0
            rows.append(dict(
                symbol=symbol, horizon=horizon, model_name="test",
                predicted_at=now - timedelta(days=600 - i),
                target_time=now - timedelta(days=599 - i),
                point_forecast=point,
                lower_bound=point * (1.0 - rel / 2.0),
                upper_bound=point * (1.0 + rel / 2.0),
                expected_change_pct=1.0, direction="up",
                confidence=0.30 + 0.01 * (i % 6),
                raw_confidence=0.30 + 0.01 * (i % 6),
                regime=("trending_up", "trending_down", "ranging",
                        "high_volatility")[i % 4],
                drivers=[], data_fresh=True, warnings=[],
                actual_value=base * (1.02 if hit else 0.98), created_at=now,
            ))
            i += 1
    with engine.begin() as conn:
        for row in rows:
            conn.execute(predictions.insert().values(**row))


def test_gate_evidence_is_scoped_to_the_instrument(engine):
    """One symbol's matured rows must not license another's horizon.

    Reproduces the audit finding: 60 matured XAUUSD 30d rows and ZERO
    IR_GOLD_18K 30d rows made a 30-day IR_GOLD_18K request "scored", moving
    shipped confidence on another instrument's outcomes. It also broke the
    invariant that a scored horizon is a calibrated one — live calibration is
    per-symbol, so that request was scored while UNBLENDED, re-creating the
    scale mismatch the blend exists to remove.
    """
    from app.models.metagate import (
        GATE_MIN_LEVEL_ROWS, evidence_key, fit_meta_gate, score_meta_gate,
    )

    _insert_symbol_pool(engine, {
        ("IR_GOLD_18K", "1d"): 120,
        ("IR_GOLD_18K", "7d"): 60,
        ("XAUUSD", "30d"): 60,
    })

    gate = fit_meta_gate(engine)
    assert gate is not None
    ev = gate["horizon_evidence"]
    assert ev.get(evidence_key("XAUUSD", 30.0), 0) >= GATE_MIN_LEVEL_ROWS
    assert ev.get(evidence_key("IR_GOLD_18K", 30.0), 0) == 0

    point, rel = 1010.0, 0.13
    gold = score_meta_gate(gate, point, point * (1 - rel / 2), point * (1 + rel / 2),
                           1.0, 0.33, "30d", "ranging", True, "IR_GOLD_18K")
    assert gold.status == "out_of_support"
    assert gold.p_hit is None
    assert gold.worst_feature == "log_horizon_days"

    # ...and the symbol that DOES have the rows is still scored, so this is a
    # scoping fix and not a blanket refusal.
    glob = score_meta_gate(gate, point, point * (1 - rel / 2), point * (1 + rel / 2),
                           1.0, 0.33, "30d", "ranging", True, "XAUUSD")
    assert glob.status == "scored"
    assert glob.p_hit is not None
