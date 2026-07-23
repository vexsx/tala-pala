"""Inverse-sMAPE-weighted ensemble of validated member models.

Weights come from walk-forward validation at training time; once every member
has enough *matured live predictions* (rows in the ``predictions`` table with
``actual_value`` filled), :func:`live_member_smapes` supplies live sMAPEs so
the prediction pass can re-weight members by real-world accuracy instead.
"""
from __future__ import annotations

from typing import Optional, Sequence

from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db import predictions, utcnow
from .base import ForecastModel, ModelUnavailable

MIN_LIVE_MATURED = 20   # matured predictions required per member
LIVE_SMAPE_WINDOW = 60  # most recent matured predictions considered


def live_member_smapes(
    engine: Engine,
    horizon: str,
    members: Sequence[str],
    min_rows: int = MIN_LIVE_MATURED,
    window: int = LIVE_SMAPE_WINDOW,
    symbol: str = "IR_GOLD_18K",
) -> Optional[dict[str, float]]:
    """Per-member live sMAPE from matured ``predictions`` rows for a horizon.

    For each member model name the most recent (by ``target_time``) up to
    ``window`` rows with ``actual_value`` filled are compared point-forecast
    vs actual.  Returns ``{member: smape_pct}`` only when EVERY member has at
    least ``min_rows`` matured rows; otherwise ``None`` (keep validation
    weights — partial live evidence must not skew the mix).
    """
    out: dict[str, float] = {}
    # Members only accumulate rows while they are themselves active, so
    # without a recency bound the comparison could pit one member's rows
    # from last winter against another's from this spring — different
    # regimes, not comparable accuracy. Stale evidence keeps validation
    # weights instead.
    recent_cutoff = utcnow() - timedelta(days=120)
    with engine.connect() as conn:
        for name in members:
            rows = conn.execute(
                select(predictions.c.point_forecast, predictions.c.actual_value)
                .where(
                    predictions.c.symbol == symbol,
                    predictions.c.horizon == horizon,
                    predictions.c.model_name == name,
                    predictions.c.actual_value.is_not(None),
                    predictions.c.target_time >= recent_cutoff,
                )
                .order_by(predictions.c.target_time.desc())
                .limit(window)
            ).all()
            if len(rows) < min_rows:
                return None
            preds = np.array([float(r[0]) for r in rows])
            actuals = np.array([float(r[1]) for r in rows])
            denom = np.abs(actuals) + np.abs(preds)
            out[name] = float(
                np.mean(np.where(denom > 0, 2.0 * np.abs(actuals - preds) / denom, 0.0))
                * 100.0
            )
    return out


def inverse_smape_weights(smapes: dict[str, float], eps: float = 1e-6) -> dict[str, float]:
    """Weights proportional to 1/sMAPE, normalized to sum to 1."""
    raw = {name: 1.0 / max(float(s), eps) for name, s in smapes.items()}
    total = sum(raw.values())
    if total <= 0:
        n = max(len(raw), 1)
        return {name: 1.0 / n for name in raw}
    return {name: w / total for name, w in raw.items()}


def combine(predictions: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted average of member point predictions."""
    common = [name for name in predictions if name in weights]
    if not common:
        raise ValueError("no overlapping members between predictions and weights")
    total_w = sum(weights[name] for name in common)
    return sum(predictions[name] * weights[name] for name in common) / total_w


class EnsembleModel(ForecastModel):
    """Holds member models + fixed validation weights; refits members on fit()."""

    name = "ensemble"

    def __init__(self, members: dict[str, ForecastModel], weights: dict[str, float]) -> None:
        if not members:
            raise ValueError("ensemble needs at least one member")
        self.members = members
        self.weights = {k: float(v) for k, v in weights.items() if k in members}
        self._point: Optional[float] = None

    def set_context(self, context) -> "EnsembleModel":
        for model in self.members.values():
            model.set_context(context)
        return self

    def fit(self, series: pd.Series, horizon: int) -> "EnsembleModel":
        preds: dict[str, float] = {}
        for name, model in self.members.items():
            try:
                model.fit(series, horizon)
            except ModelUnavailable:
                # e.g. an exog member whose auxiliary series vanished at
                # predict time: drop it; combine() renormalizes the weights
                continue
            preds[name] = model.predict_point()
        self._point = combine(preds, self.weights)
        return self

    def predict_point(self) -> float:
        assert self._point is not None, "fit() first"
        return self._point
