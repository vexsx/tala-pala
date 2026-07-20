"""Baseline forecasters: naive last-value, SMA(k), SES / Holt (statsmodels)."""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from .base import ForecastModel, register


class NaiveModel(ForecastModel):
    """Random-walk baseline: forecast = last observed value."""

    name = "naive"

    def __init__(self) -> None:
        self._last: Optional[float] = None

    def fit(self, series: pd.Series, horizon: int) -> "NaiveModel":
        if series.empty:
            raise ValueError("naive: empty series")
        self._last = float(series.iloc[-1])
        return self

    def predict_point(self) -> float:
        assert self._last is not None, "fit() first"
        return self._last


class SMAModel(ForecastModel):
    """Simple moving average of the last ``k`` observations."""

    name = "sma"

    def __init__(self, k: int = 5) -> None:
        self.k = k
        self._value: Optional[float] = None

    def fit(self, series: pd.Series, horizon: int) -> "SMAModel":
        if series.empty:
            raise ValueError("sma: empty series")
        tail = series.iloc[-self.k :]
        self._value = float(tail.mean())
        return self

    def predict_point(self) -> float:
        assert self._value is not None, "fit() first"
        return self._value


class SESModel(ForecastModel):
    """Simple exponential smoothing, upgraded to Holt's linear trend when the
    trended fit has lower in-sample SSE.  Falls back to naive on tiny series
    or optimizer failure."""

    name = "ses"

    def __init__(self) -> None:
        self._forecast: Optional[float] = None
        self.method: str = "ses"

    def fit(self, series: pd.Series, horizon: int) -> "SESModel":
        values = series.astype(float).to_numpy()
        if len(values) < 10:
            self._forecast = float(values[-1])
            self.method = "naive_fallback"
            return self
        try:
            from statsmodels.tsa.holtwinters import Holt, SimpleExpSmoothing

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ses_fit = SimpleExpSmoothing(values, initialization_method="estimated").fit(
                    optimized=True
                )
                holt_fit = Holt(values, initialization_method="estimated").fit(optimized=True)
            if float(holt_fit.sse) < float(ses_fit.sse):
                self._forecast = float(holt_fit.forecast(horizon)[-1])
                self.method = "holt"
            else:
                self._forecast = float(ses_fit.forecast(horizon)[-1])
                self.method = "ses"
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


register("naive", NaiveModel)
register("sma", SMAModel)
register("ses", SESModel)
