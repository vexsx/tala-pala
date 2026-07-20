"""Classical additions (docs/CONTRACTS.md Addendum 2): Theta and damped Holt.

* ``theta`` — statsmodels ThetaModel (no deseasonalization; the daily gold
  series has no stable weekly season once Fridays are missing anyway), with a
  pure-numpy two-theta-line fallback (theta=0 trend line + SES on the theta=2
  line, averaged 50/50) when the statsmodels fit fails.
* ``holt_damped`` — Holt's linear trend with ``damped_trend=True`` so long
  horizons do not extrapolate a straight line forever.

Both degrade to naive (last value) on tiny series or optimizer failure, like
the other classical members.
"""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from .base import ForecastModel, register

_SES_ALPHA_GRID = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


def _ses_forecast(values: np.ndarray, alpha_grid=_SES_ALPHA_GRID) -> float:
    """Flat SES forecast with the grid alpha minimizing one-step-ahead SSE."""
    best_level, best_sse = float(values[-1]), np.inf
    for alpha in alpha_grid:
        level = float(values[0])
        sse = 0.0
        for v in values[1:]:
            sse += (float(v) - level) ** 2
            level = alpha * float(v) + (1.0 - alpha) * level
        if sse < best_sse:
            best_sse, best_level = sse, level
    return best_level


def two_theta_forecast(values: np.ndarray, horizon: int) -> float:
    """Classic two-theta-line forecast: 0.5*(theta=0 trend extrapolation)
    + 0.5*(SES forecast of the theta=2 line ``2*y - trend``)."""
    n = len(values)
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, values, 1)
    theta0 = intercept + slope * (n - 1 + horizon)
    theta2 = 2.0 * values - (intercept + slope * x)
    return float(0.5 * theta0 + 0.5 * _ses_forecast(theta2))


class ThetaForecastModel(ForecastModel):
    name = "theta"

    def __init__(self) -> None:
        self._forecast: Optional[float] = None
        self.method: str = "theta"

    def fit(self, series: pd.Series, horizon: int) -> "ThetaForecastModel":
        values = series.astype(float).to_numpy()
        if len(values) < 15:
            self._forecast = float(values[-1])
            self.method = "naive_fallback"
            return self
        try:
            from statsmodels.tsa.forecasting.theta import ThetaModel

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fit = ThetaModel(values, deseasonalize=False).fit()
                self._forecast = float(fit.forecast(horizon).iloc[-1])
            self.method = "statsmodels"
        except Exception:
            self._forecast = two_theta_forecast(values, horizon)
            self.method = "two_theta"
        if not np.isfinite(self._forecast):
            self._forecast = float(values[-1])
            self.method = "naive_fallback"
        return self

    def predict_point(self) -> float:
        assert self._forecast is not None, "fit() first"
        return self._forecast


class HoltDampedModel(ForecastModel):
    name = "holt_damped"

    def __init__(self) -> None:
        self._forecast: Optional[float] = None
        self.method: str = "holt_damped"

    def fit(self, series: pd.Series, horizon: int) -> "HoltDampedModel":
        values = series.astype(float).to_numpy()
        if len(values) < 10:
            self._forecast = float(values[-1])
            self.method = "naive_fallback"
            return self
        try:
            from statsmodels.tsa.holtwinters import Holt

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fit = Holt(
                    values, damped_trend=True, initialization_method="estimated"
                ).fit(optimized=True)
            self._forecast = float(fit.forecast(horizon)[-1])
            self.method = "holt_damped"
        except Exception:
            self._forecast = float(values[-1])
            self.method = "naive_fallback"
        if not np.isfinite(self._forecast):
            self._forecast = float(values[-1])
            self.method = "naive_fallback"
        return self

    def predict_point(self) -> float:
        assert self._forecast is not None, "fit() first"
        return self._forecast


register("theta", ThetaForecastModel)
register("holt_damped", HoltDampedModel)
