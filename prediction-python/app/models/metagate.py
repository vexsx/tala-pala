"""Meta-labeling gate: the system learns to judge its own forecasts.

López de Prado's meta-labeling idea (Advances in Financial Machine Learning,
2018): keep the primary model for *direction*, and train a secondary
classifier on realized outcomes to predict *whether the primary call will be
right*. Here the training data is the app's own ``predictions`` table — every
matured prediction is one labeled example (features known at prediction time,
label = did the direction call hit).

The evaluate job refits the gate as outcomes accumulate and stores it as
plain coefficients in ``app_settings['meta_gate']``; the prediction pass
applies it with numpy only (no sklearn needed at inference). Every part is
causal: features are those stored ON the prediction row, the label arrives
strictly later.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db import predictions, utcnow

log = logging.getLogger(__name__)

META_GATE_KEY = "meta_gate"
MIN_SAMPLES = 40          # matured, non-flat predictions before the gate exists
MAX_SAMPLES = 500         # most recent examples used for the refit
REGIMES = ("trending_up", "trending_down", "ranging", "high_volatility")
HORIZON_DAYS = {"1h": 1 / 24, "4h": 4 / 24, "eod": 1.0, "1d": 1.0,
                "3d": 3.0, "7d": 7.0, "30d": 30.0}

PRIMARY_SYMBOL = "IR_GOLD_18K"

FEATURE_NAMES = (
    "rel_width",        # (upper - lower) / point — model's own uncertainty
    "abs_expected_pct", # size of the predicted move
    "confidence",       # pre-gate confidence stored at prediction time
    "log_horizon_days", # horizon scale
    "data_fresh",       # was input data fresh
    "is_global",        # symbol != IR_GOLD_18K: the two instruments have
                        # different hit-rate structure; pooling them without
                        # this feature bled one's calibration into the other
    *(f"regime_{r}" for r in REGIMES),
)


def _row_features(
    point: float, lower: float, upper: float, expected_pct: float,
    confidence: float, horizon: str, regime: str, data_fresh: bool,
    symbol: str = PRIMARY_SYMBOL,
) -> Optional[list[float]]:
    if point == 0:
        return None
    days = HORIZON_DAYS.get(horizon)
    if days is None:
        try:
            days = float(str(horizon).rstrip("d"))
        except ValueError:
            return None
    feats = [
        (upper - lower) / abs(point),
        abs(expected_pct),
        float(confidence),
        float(np.log(days)),
        1.0 if data_fresh else 0.0,
        0.0 if symbol == PRIMARY_SYMBOL else 1.0,
    ]
    feats.extend(1.0 if regime == r else 0.0 for r in REGIMES)
    return feats


def _recover_base(point: float, expected_change_pct: float) -> Optional[float]:
    if expected_change_pct <= -100:
        return None
    base = point / (1.0 + expected_change_pct / 100.0)
    return base if base else None


def fit_meta_gate(engine: Engine) -> Optional[dict]:
    """Fit the gate on matured predictions; returns the storable dict or None.

    Flat calls (predicted direction 'flat') are excluded — the gate scores
    directional conviction, and a flat call has none to score.
    """
    stmt = (
        select(
            predictions.c.point_forecast, predictions.c.lower_bound,
            predictions.c.upper_bound, predictions.c.expected_change_pct,
            predictions.c.confidence, predictions.c.raw_confidence,
            predictions.c.horizon,
            predictions.c.regime, predictions.c.data_fresh,
            predictions.c.direction, predictions.c.actual_value,
            predictions.c.symbol,
        )
        .where(predictions.c.actual_value.is_not(None))
        .order_by(predictions.c.target_time.desc())
        .limit(MAX_SAMPLES)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()

    X: list[list[float]] = []
    y: list[int] = []
    for (point, lower, upper, exp_pct, conf, raw_conf, horizon, regime, fresh,
         direction, actual, symbol) in rows:
        if direction == "flat":
            continue
        point = float(point)
        base = _recover_base(point, float(exp_pct))
        if base is None:
            continue
        # Train on the PRE-gate confidence: the stored blended value contains
        # the previous gate's own output (self-reference). Old rows without
        # raw_confidence fall back to the blended value.
        conf_feature = float(raw_conf) if raw_conf is not None else float(conf)
        feats = _row_features(point, float(lower), float(upper), float(exp_pct),
                              conf_feature, str(horizon), str(regime), bool(fresh),
                              str(symbol))
        if feats is None:
            continue
        pred_sign = np.sign(point - base)
        real_sign = np.sign(float(actual) - base)
        if pred_sign == 0:
            continue
        X.append(feats)
        y.append(1 if pred_sign == real_sign else 0)

    if len(y) < MIN_SAMPLES or len(set(y)) < 2:
        return None  # not enough evidence (or degenerate labels) yet

    from sklearn.linear_model import LogisticRegression

    Xa = np.asarray(X, dtype=float)
    mean = Xa.mean(axis=0)
    std = Xa.std(axis=0)
    # floor, not ==0: near-constant features would round to a stored std of 0
    # and divide-by-zero at apply time
    std[std < 1e-6] = 1.0
    Xs = (Xa - mean) / std
    clf = LogisticRegression(C=1.0, max_iter=1000)
    clf.fit(Xs, np.asarray(y))
    return {
        "feature_names": list(FEATURE_NAMES),
        "mean": [round(float(v), 8) for v in mean],
        "std": [round(float(v), 8) for v in std],
        "coef": [round(float(v), 8) for v in clf.coef_[0]],
        "intercept": round(float(clf.intercept_[0]), 8),
        "n": int(len(y)),
        "base_rate": round(float(np.mean(y)), 4),
        "trained_at": utcnow().isoformat(),
    }


def apply_meta_gate(
    gate: Optional[dict],
    point: float, lower: float, upper: float, expected_pct: float,
    confidence: float, horizon: str, regime: str, data_fresh: bool,
    symbol: str = PRIMARY_SYMBOL,
) -> Optional[float]:
    """P(direction call is right) from the stored gate; None when unusable."""
    if not gate:
        return None
    feats = _row_features(point, lower, upper, expected_pct, confidence,
                          horizon, regime, data_fresh, symbol)
    if feats is None:
        return None
    # A gate persisted before a feature-set change cannot score the new
    # vector; stay silent until the evaluate job refits it.
    stored_names = gate.get("feature_names")
    if stored_names is not None and list(stored_names) != list(FEATURE_NAMES):
        return None
    try:
        mean = np.asarray(gate["mean"], dtype=float)
        std = np.asarray(gate["std"], dtype=float)
        std = np.where(std <= 0, 1.0, std)  # defensive: stored gates predate the floor
        coef = np.asarray(gate["coef"], dtype=float)
        intercept = float(gate["intercept"])
        x = (np.asarray(feats, dtype=float) - mean) / std
        z = float(np.dot(coef, x) + intercept)
        return float(1.0 / (1.0 + np.exp(-z)))
    except (KeyError, ValueError, TypeError):
        return None
