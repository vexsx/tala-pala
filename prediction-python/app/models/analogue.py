"""Pattern-analogue forecaster (``knn_analogue``, Addendum 2) — pure numpy.

The last ``k=20`` log-returns are z-scored into a query window; the ``m=25``
nearest historical windows (euclidean distance on z-scored windows) vote with
inverse-distance weights, and the forecast applies their weighted-mean
subsequent ``h``-step cumulative log-return to the last price.

Deterministic: no RNG anywhere, ties broken by a stable argsort.  On series
too short to build a meaningful library the model degrades to naive
(zero forecast return).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .base import ForecastModel, register

K_WINDOW = 20     # length of the return window being matched
M_NEIGHBORS = 25  # analogues that vote
MIN_LIBRARY = 5   # minimum candidate windows to attempt a forecast


class KNNAnalogueModel(ForecastModel):
    name = "knn_analogue"

    def __init__(self, k: int = K_WINDOW, m: int = M_NEIGHBORS) -> None:
        self.k = k
        self.m = m
        self._last: Optional[float] = None
        self._cum_logret: float = 0.0

    @staticmethod
    def _normalize(window: np.ndarray) -> np.ndarray:
        std = float(np.std(window))
        return (window - float(np.mean(window))) / (std if std > 1e-12 else 1.0)

    def fit(self, series: pd.Series, horizon: int) -> "KNNAnalogueModel":
        values = series.astype(float).to_numpy()
        self._last = float(values[-1])
        self._cum_logret = 0.0

        returns = np.diff(np.log(values))
        n = len(returns)
        # candidate window ends j: window = returns[j-k+1 .. j], outcome =
        # sum(returns[j+1 .. j+h]); everything is history known at fit time.
        n_candidates = (n - horizon) - (self.k - 1)
        if n < self.k or n_candidates < MIN_LIBRARY:
            return self  # too short -> naive (zero forecast return)

        query = self._normalize(returns[-self.k:])
        windows = np.empty((n_candidates, self.k))
        outcomes = np.empty(n_candidates)
        for row, j in enumerate(range(self.k - 1, n - horizon)):
            windows[row] = self._normalize(returns[j - self.k + 1 : j + 1])
            outcomes[row] = float(np.sum(returns[j + 1 : j + 1 + horizon]))

        distances = np.sqrt(np.sum((windows - query) ** 2, axis=1))
        nearest = np.argsort(distances, kind="stable")[: min(self.m, n_candidates)]
        weights = 1.0 / (distances[nearest] + 1e-9)
        cum = float(np.average(outcomes[nearest], weights=weights))
        if np.isfinite(cum):
            self._cum_logret = cum
        return self

    def predict_point(self) -> float:
        assert self._last is not None, "fit() first"
        return self._last * float(np.exp(self._cum_logret))


register("knn_analogue", KNNAnalogueModel)
