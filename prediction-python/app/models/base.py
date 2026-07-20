"""Forecast model interface + registry.

All models share one interface so walk-forward validation can treat them
uniformly:

* ``fit(series, horizon)`` — ``series`` is a float ``pd.Series`` with a
  DatetimeIndex containing ONLY data known at fit time; ``horizon`` is the
  number of forward steps being forecast.
* ``predict_point()`` — the point forecast for ``horizon`` steps after the
  last observation of the fitted series.
* ``set_context(context)`` — optional auxiliary point-in-time series for
  models with exogenous inputs (no-op by default).
* ``predict_interval()`` — optional native interval (None by default; the
  prediction pass then falls back to residual-quantile intervals).
"""
from __future__ import annotations

import abc
from typing import Callable, Optional

import pandas as pd


class ModelUnavailable(Exception):
    """The model cannot run in the current context (e.g. its exogenous series
    are missing).  Walk-forward validation skips the model entirely instead of
    retrying it on every fold."""


class ForecastModel(abc.ABC):
    """h-step-ahead point forecaster."""

    name: str = "base"
    # When True, walk-forward reuses ONE instance across folds so expensive
    # one-off work (e.g. ARIMA/SARIMAX order selection on the earliest window,
    # train-only information) is not repeated per fold.
    reuse_across_folds: bool = False

    @abc.abstractmethod
    def fit(self, series: pd.Series, horizon: int) -> "ForecastModel":
        """Fit on the given history.  Must not mutate ``series``."""

    @abc.abstractmethod
    def predict_point(self) -> float:
        """Point forecast ``horizon`` steps ahead of the fitted history."""

    def set_context(self, context: Optional[dict]) -> "ForecastModel":
        """Attach auxiliary point-in-time series (e.g. ``{"usd_irt": Series,
        "xau_usd": Series}``).  Models that use exogenous data override this;
        the default is a no-op so plain models can ignore it."""
        return self

    def predict_interval(self) -> Optional[tuple[float, float]]:
        """Native ``(lower, upper)`` interval when the model provides one
        (e.g. quantile regression); None means the caller should fall back to
        empirical residual-quantile intervals."""
        return None

    def feature_importances(self) -> Optional[list[tuple[str, float]]]:
        """(feature, importance) pairs when available (tabular models)."""
        return None


ModelFactory = Callable[[], ForecastModel]

_REGISTRY: dict[str, ModelFactory] = {}


def register(name: str, factory: ModelFactory) -> None:
    _REGISTRY[name] = factory


def make(name: str) -> ForecastModel:
    if name not in _REGISTRY:
        raise KeyError(f"unknown model: {name!r} (known: {sorted(_REGISTRY)})")
    return _REGISTRY[name]()


def available() -> list[str]:
    return sorted(_REGISTRY)
