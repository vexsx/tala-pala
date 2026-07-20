"""ARIMA with a small (p,d,q) grid selected by AIC on training data only.

To keep walk-forward affordable the order is selected once per model instance
(on the first ``fit`` call, i.e. the earliest training window — no future
information) and then re-used for subsequent refits.
"""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from .base import ForecastModel, register

GRID: tuple[tuple[int, int, int], ...] = (
    (0, 1, 0),
    (0, 1, 1),
    (1, 0, 0),
    (1, 1, 0),
    (1, 1, 1),
    (2, 1, 0),
    (2, 1, 1),
)


class ARIMAModel(ForecastModel):
    name = "arima"

    def __init__(self) -> None:
        self.order: Optional[tuple[int, int, int]] = None
        self._forecast: Optional[float] = None

    def _select_order(self, values: np.ndarray) -> tuple[int, int, int]:
        from statsmodels.tsa.arima.model import ARIMA

        best_order, best_aic = (0, 1, 0), np.inf
        for order in GRID:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    fit = ARIMA(values, order=order).fit()
                if np.isfinite(fit.aic) and fit.aic < best_aic:
                    best_order, best_aic = order, float(fit.aic)
            except Exception:
                continue
        return best_order

    def fit(self, series: pd.Series, horizon: int) -> "ARIMAModel":
        values = series.astype(float).to_numpy()
        if len(values) < 30:
            self._forecast = float(values[-1])
            return self
        try:
            from statsmodels.tsa.arima.model import ARIMA

            if self.order is None:
                self.order = self._select_order(values)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fit = ARIMA(values, order=self.order).fit()
                self._forecast = float(fit.forecast(horizon)[-1])
        except Exception:
            self._forecast = float(values[-1])
        if not np.isfinite(self._forecast):
            self._forecast = float(values[-1])
        return self

    def predict_point(self) -> float:
        assert self._forecast is not None, "fit() first"
        return self._forecast


register("arima", ARIMAModel)
