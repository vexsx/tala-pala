"""Tabular ML forecasters (Ridge / RandomForest / GradientBoosting /
HistGradientBoosting / quantile GradientBoosting).

Features are built causally from the price series itself
(:func:`app.features.engineering.compute_feature_frame`); the target is the
``h``-step **log-return** ``log(y[t+h] / y[t])``, converted back to a price at
predict time.  Deliberately no torch/tensorflow/prophet — see docs for the
rationale; sklearn ensembles are sufficient at this data scale.
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..features.engineering import compute_feature_frame
from .base import ForecastModel, register

# calendar features stay; raw price-level columns are dropped so the model
# learns from scale-free inputs
_DROP_COLS = ("close", "lag_1", "lag_2", "lag_3", "lag_5", "lag_10", "lag_20",
              "roll_mean_5", "roll_mean_10", "roll_mean_20")


def _feature_matrix(series: pd.Series) -> pd.DataFrame:
    frame = compute_feature_frame(series)
    return frame.drop(columns=[c for c in _DROP_COLS if c in frame.columns])


class TabularModel(ForecastModel):
    """Wraps an sklearn regressor predicting the h-step log-return."""

    name = "tabular"

    def __init__(
        self,
        name: str,
        estimator_factory: Callable[[], object],
        min_rows: int = 30,
    ) -> None:
        self.name = name
        self._factory = estimator_factory
        self.min_rows = min_rows
        self.estimator: Optional[object] = None
        self.feature_names: list[str] = []
        self._last_close: Optional[float] = None
        self._pred_logret: Optional[float] = None

    def fit(self, series: pd.Series, horizon: int) -> "TabularModel":
        series = series.astype(float)
        self._last_close = float(series.iloc[-1])
        features = _feature_matrix(series)
        close = series
        target = np.log(close.shift(-horizon) / close)

        train = features.copy()
        train["__target__"] = target
        train = train.dropna()
        if len(train) < self.min_rows:
            # not enough clean rows -> degrade to naive (predict zero return)
            self.estimator = None
            self._pred_logret = 0.0
            return self

        X = train.drop(columns="__target__")
        y = train["__target__"].to_numpy()
        self.feature_names = list(X.columns)
        self.estimator = self._factory()
        self.estimator.fit(X.to_numpy(), y)  # type: ignore[attr-defined]

        last_row = features.iloc[[-1]][self.feature_names]
        if last_row.isna().any(axis=None):
            self._pred_logret = 0.0
        else:
            self._pred_logret = float(
                self.estimator.predict(last_row.to_numpy())[0]  # type: ignore[attr-defined]
            )
        if not np.isfinite(self._pred_logret):
            self._pred_logret = 0.0
        return self

    def predict_point(self) -> float:
        assert self._last_close is not None, "fit() first"
        return self._last_close * float(np.exp(self._pred_logret or 0.0))

    def feature_importances(self) -> Optional[list[tuple[str, float]]]:
        if self.estimator is None or not self.feature_names:
            return None
        est = self.estimator
        steps = getattr(est, "steps", None)  # unwrap sklearn Pipelines
        if steps:
            est = steps[-1][1]
        importances = getattr(est, "feature_importances_", None)
        if importances is None:
            coef = getattr(est, "coef_", None)
            if coef is None:
                return None
            importances = np.abs(np.asarray(coef, dtype=float))
        total = float(np.sum(importances)) or 1.0
        pairs = [
            (name, float(imp) / total)
            for name, imp in zip(self.feature_names, importances)
        ]
        pairs.sort(key=lambda p: p[1], reverse=True)
        return pairs


def _make_linear() -> TabularModel:
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return TabularModel("linear", lambda: make_pipeline(StandardScaler(), Ridge(alpha=1.0)))


def _make_rf() -> TabularModel:
    from sklearn.ensemble import RandomForestRegressor

    return TabularModel(
        "rf",
        lambda: RandomForestRegressor(
            n_estimators=120, max_depth=6, min_samples_leaf=5, random_state=42, n_jobs=-1
        ),
    )


def _make_gbr() -> TabularModel:
    from sklearn.ensemble import GradientBoostingRegressor

    return TabularModel(
        "gbr",
        lambda: GradientBoostingRegressor(
            n_estimators=150, max_depth=3, learning_rate=0.05, subsample=0.9,
            random_state=42,
        ),
    )


def _make_hist_gb() -> TabularModel:
    from sklearn.ensemble import HistGradientBoostingRegressor

    # capacity kept modest (leaf cap + early stopping) so 40-fold walk-forward
    # stays affordable at this data scale
    return TabularModel(
        "hist_gb",
        lambda: HistGradientBoostingRegressor(
            max_iter=150, learning_rate=0.06, min_samples_leaf=5,
            max_leaf_nodes=15, l2_regularization=1e-3,
            early_stopping=True, n_iter_no_change=8, validation_fraction=0.15,
            random_state=42,
        ),
    )


QUANTILES = (0.05, 0.5, 0.95)  # native 90% interval + median point


class QuantileGBRModel(ForecastModel):
    """Three quantile GradientBoostingRegressors (5%/50%/95%) on the h-step
    log-return.  The median model is the point forecast; the outer quantiles
    give the model's OWN native interval via ``predict_interval`` (sorted to
    repair any quantile crossing, so lower <= point <= upper always holds)."""

    name = "quantile_gbr"

    def __init__(self, min_rows: int = 30) -> None:
        self.min_rows = min_rows
        self.estimators: dict[float, object] = {}
        self.feature_names: list[str] = []
        self._last_close: Optional[float] = None
        self._logrets: Optional[tuple[float, float, float]] = None  # lo, mid, hi

    def fit(self, series: pd.Series, horizon: int) -> "QuantileGBRModel":
        from sklearn.ensemble import GradientBoostingRegressor

        series = series.astype(float)
        self._last_close = float(series.iloc[-1])
        self._logrets = None
        self.estimators = {}

        features = _feature_matrix(series)
        target = np.log(series.shift(-horizon) / series)
        train = features.copy()
        train["__target__"] = target
        train = train.dropna()
        if len(train) < self.min_rows:
            return self  # degrade to naive (no native interval either)

        X = train.drop(columns="__target__")
        y = train["__target__"].to_numpy()
        self.feature_names = list(X.columns)
        last_row = features.iloc[[-1]][self.feature_names]
        if last_row.isna().any(axis=None):
            return self

        preds: dict[float, float] = {}
        for alpha in QUANTILES:
            # three fits per (re)fit call: keep each tree budget small so the
            # walk-forward loop stays affordable (reduced size, not folds)
            est = GradientBoostingRegressor(
                loss="quantile", alpha=alpha, n_estimators=60, max_depth=2,
                learning_rate=0.1, subsample=0.9, random_state=42,
            )
            est.fit(X.to_numpy(), y)
            self.estimators[alpha] = est
            preds[alpha] = float(est.predict(last_row.to_numpy())[0])
        triple = np.sort([preds[a] for a in QUANTILES])  # repair crossings
        if np.all(np.isfinite(triple)):
            self._logrets = (float(triple[0]), float(triple[1]), float(triple[2]))
        return self

    def predict_point(self) -> float:
        assert self._last_close is not None, "fit() first"
        if self._logrets is None:
            return self._last_close
        return self._last_close * float(np.exp(self._logrets[1]))

    def predict_interval(self) -> Optional[tuple[float, float]]:
        if self._logrets is None or self._last_close is None:
            return None
        lower = self._last_close * float(np.exp(self._logrets[0]))
        upper = self._last_close * float(np.exp(self._logrets[2]))
        return (lower, upper)

    def feature_importances(self) -> Optional[list[tuple[str, float]]]:
        est = self.estimators.get(0.5)
        importances = getattr(est, "feature_importances_", None)
        if est is None or importances is None or not self.feature_names:
            return None
        total = float(np.sum(importances)) or 1.0
        pairs = [
            (name, float(imp) / total)
            for name, imp in zip(self.feature_names, importances)
        ]
        pairs.sort(key=lambda p: p[1], reverse=True)
        return pairs


register("linear", _make_linear)
register("rf", _make_rf)
register("gbr", _make_gbr)
register("hist_gb", _make_hist_gb)
register("quantile_gbr", QuantileGBRModel)
