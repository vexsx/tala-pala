"""SARIMAX on log-returns with point-in-time exogenous regressors.

Exogenous columns (docs/CONTRACTS.md Addendum 2): USD_IRT log-return, XAUUSD
log-return and the 30-day premium z-score.  The auxiliary series arrive via
``set_context`` (see ``ForecastModel.set_context``); before use they are
truncated to timestamps <= the last gold observation and forward-filled
(propagating the PAST only), so every exog row uses values known at that
row's time — the same leakage policy as feature engineering.

h-step forecasts need a future exog path which is unknown at prediction time;
the last KNOWN exog row is held constant over the horizon (documented
simplification — exogenous news is treated as flat, the AR structure carries
the forecast).

Order selection mirrors ARIMA: a small grid scored by AIC once per model
instance on the earliest training window (train-only information), then
reused across walk-forward folds (``reuse_across_folds``).

When the exogenous series are unavailable the model raises
:class:`ModelUnavailable` so walk-forward skips it entirely.
"""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from ..core.formula import KARAT_18_PURITY, TROY_OUNCE_GRAMS
from .base import ForecastModel, ModelUnavailable, register

# (p, d, q) on log-returns — d=0 because differencing already happened.
ORDER_GRID: tuple[tuple[int, int, int], ...] = (
    (1, 0, 0),
    (0, 0, 1),
    (1, 0, 1),
    (2, 0, 0),
)

MIN_POINTS = 60          # gold observations required to attempt a SARIMAX fit
MIN_CLEAN_ROWS = 40      # aligned rows after NaN warm-up removal
PREMIUM_Z_WINDOW = 30
EXOG_COLUMNS = ("usd_logret", "xau_logret", "premium_z")


class SarimaxExogModel(ForecastModel):
    name = "sarimax_exog"
    reuse_across_folds = True  # order selected once on the earliest window

    def __init__(self) -> None:
        self.order: Optional[tuple[int, int, int]] = None
        self._forecast: Optional[float] = None
        self._context: Optional[dict] = None

    def set_context(self, context: Optional[dict]) -> "SarimaxExogModel":
        self._context = context
        return self

    # -- exog construction ----------------------------------------------------

    def _aux_series(self, name: str, cutoff: pd.Timestamp) -> pd.Series:
        aux = (self._context or {}).get(name)
        if aux is None or len(aux) == 0:
            raise ModelUnavailable(f"sarimax_exog: {name} series unavailable")
        aux = aux.astype(float)
        aux = aux[aux.index <= cutoff]  # point-in-time: nothing after the gold history
        if aux.empty:
            raise ModelUnavailable(f"sarimax_exog: {name} has no data before cutoff")
        return aux

    def _exog_frame(self, gold: pd.Series) -> pd.DataFrame:
        index = gold.index
        cutoff = index[-1]
        usd = self._aux_series("usd_irt", cutoff)
        xau = self._aux_series("xau_usd", cutoff)

        def _align(aux: pd.Series) -> pd.Series:
            # forward-fill = propagate the PAST only (causal)
            return aux.reindex(index.union(aux.index)).ffill().reindex(index)

        usd_a, xau_a = _align(usd), _align(xau)
        exog = pd.DataFrame(index=index)
        exog["usd_logret"] = np.log(usd_a).diff()
        exog["xau_logret"] = np.log(xau_a).diff()
        theoretical = xau_a / TROY_OUNCE_GRAMS * usd_a * KARAT_18_PURITY
        premium = (gold - theoretical) / theoretical * 100.0
        prem_mean = premium.rolling(PREMIUM_Z_WINDOW).mean()
        prem_std = premium.rolling(PREMIUM_Z_WINDOW).std()
        exog["premium_z"] = (premium - prem_mean) / prem_std.replace(0.0, np.nan)
        return exog

    # -- fitting ---------------------------------------------------------------

    def _select_order(self, y: np.ndarray, X: np.ndarray) -> tuple[int, int, int]:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        best_order, best_aic = ORDER_GRID[0], np.inf
        for order in ORDER_GRID:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    fit = SARIMAX(
                        y, exog=X, order=order,
                        enforce_stationarity=False, enforce_invertibility=False,
                    ).fit(disp=False)
                if np.isfinite(fit.aic) and fit.aic < best_aic:
                    best_order, best_aic = order, float(fit.aic)
            except Exception:
                continue
        return best_order

    def fit(self, series: pd.Series, horizon: int) -> "SarimaxExogModel":
        gold = series.astype(float)
        last = float(gold.iloc[-1])
        self._forecast = last  # naive unless a real fit succeeds

        # guard first: no exog series -> the model must not participate at all
        if not self._context or any(
            (self._context.get(k) is None or len(self._context.get(k)) == 0)
            for k in ("usd_irt", "xau_usd")
        ):
            raise ModelUnavailable("sarimax_exog: exogenous series unavailable")

        if len(gold) < MIN_POINTS:
            return self  # naive fallback (consistent with arima on tiny series)

        exog = self._exog_frame(gold)  # may raise ModelUnavailable
        data = pd.concat([np.log(gold).diff().rename("y"), exog], axis=1).dropna()
        if len(data) < MIN_CLEAN_ROWS:
            return self

        y = data["y"].to_numpy()
        X = data[list(EXOG_COLUMNS)].to_numpy()
        try:
            from statsmodels.tsa.statespace.sarimax import SARIMAX

            if self.order is None:
                self.order = self._select_order(y, X)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fit = SARIMAX(
                    y, exog=X, order=self.order,
                    enforce_stationarity=False, enforce_invertibility=False,
                ).fit(disp=False)
                # future exog unknown -> hold the last known row constant
                future_exog = np.tile(X[-1], (horizon, 1))
                returns = np.asarray(fit.forecast(steps=horizon, exog=future_exog))
            forecast = last * float(np.exp(np.sum(returns)))
            if np.isfinite(forecast):
                self._forecast = forecast
        except Exception:
            self._forecast = last
        return self

    def predict_point(self) -> float:
        assert self._forecast is not None, "fit() first"
        return self._forecast


register("sarimax_exog", SarimaxExogModel)
