"""Forecast model interface + registry.

All models share one interface so walk-forward validation can treat them
uniformly:

* ``fit(series, horizon)`` — ``series`` is a float ``pd.Series`` with a
  DatetimeIndex containing ONLY data known at fit time; ``horizon`` is the
  number of forward steps being forecast.
* ``predict_point()`` — the point forecast for ``horizon`` steps after the
  last observation of the fitted series.
"""
from __future__ import annotations

import abc
from typing import Callable, Optional

import pandas as pd


class ForecastModel(abc.ABC):
    """h-step-ahead point forecaster."""

    name: str = "base"

    @abc.abstractmethod
    def fit(self, series: pd.Series, horizon: int) -> "ForecastModel":
        """Fit on the given history.  Must not mutate ``series``."""

    @abc.abstractmethod
    def predict_point(self) -> float:
        """Point forecast ``horizon`` steps ahead of the fitted history."""

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
