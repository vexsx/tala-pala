"""Models inspired by widely-used TradingView community prediction scripts.

Techniques are REIMPLEMENTED from their published mathematical descriptions —
no Pine Script code is copied (TradingView scripts carry varied licenses).
Each entered the same walk-forward tournament as every other candidate and
only activates when it beats the naive baseline. Three ideas survived review:

* ``lorentzian_knn`` — k-nearest-neighbors over *indicator feature vectors*
  using the Lorentzian distance ``sum(ln(1 + |x_i - y_i|))`` (popularized by
  jdehorty's "Machine Learning: Lorentzian Classification"). The log damping
  makes the metric robust to outlier bars and regime warping, and
  chronologically-spaced neighbor selection reduces autocorrelated votes.

* ``kalman_llt`` — Kalman local-linear-trend state-space forecaster (the
  engine inside several "Kalman predictor" scripts), fit on log prices via
  statsmodels UnobservedComponents. A principled cousin of Holt's method:
  level + slope states with learned noise variances.

* :func:`mc_probabilities` — moving-block bootstrap Monte Carlo over
  historical log returns (the honest version of the popular GBM "prediction
  cone" scripts: bootstrap keeps fat tails and volatility clustering that a
  normal GBM throws away). Produces P(move clears trading costs) for the
  decision engine rather than pretending to improve point accuracy.
"""
from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from .base import ForecastModel, register

# --- lorentzian knn ----------------------------------------------------------

LKNN_NEIGHBORS = 20      # neighbors that vote
LKNN_SPACING = 4         # min index distance between chosen neighbors
LKNN_MIN_LIBRARY = 30    # candidate rows required before forecasting
RSI_P = 14
STOCH_P = 14
SMA_P = 20
MOM_P = 10
VOL_P = 20


def _lorentzian_features(values: np.ndarray) -> np.ndarray:
    """Causal feature matrix (n x 5): RSI, stoch %K, momentum, z-score, vol.

    All rolling windows end at the current row. Rows with insufficient
    warm-up contain NaN and are filtered by the caller.
    """
    s = pd.Series(values, dtype=float)
    delta = s.diff()
    gain = delta.clip(lower=0.0).rolling(RSI_P).mean()
    loss = (-delta.clip(upper=0.0)).rolling(RSI_P).mean()
    rs = gain / loss.replace(0.0, np.nan)
    rsi = (100.0 - 100.0 / (1.0 + rs)).where(~(loss == 0.0) | gain.isna(), 100.0)

    lo = s.rolling(STOCH_P).min()
    hi = s.rolling(STOCH_P).max()
    stoch = 100.0 * (s - lo) / (hi - lo).replace(0.0, np.nan)

    momentum = s.pct_change(MOM_P) * 100.0

    sma = s.rolling(SMA_P).mean()
    std = s.rolling(SMA_P).std().replace(0.0, np.nan)
    zscore = (s - sma) / std

    vol = s.pct_change().rolling(VOL_P).std() * 100.0

    return np.column_stack([
        rsi.to_numpy(), stoch.to_numpy(), momentum.to_numpy(),
        zscore.to_numpy(), vol.to_numpy(),
    ])


class LorentzianKNNModel(ForecastModel):
    """Directional kNN on indicator features with Lorentzian distance."""

    name = "lorentzian_knn"

    def __init__(self, m: int = LKNN_NEIGHBORS, spacing: int = LKNN_SPACING) -> None:
        self.m = m
        self.spacing = spacing
        self._last: Optional[float] = None
        self._cum_logret: float = 0.0

    def fit(self, series: pd.Series, horizon: int) -> "LorentzianKNNModel":
        values = series.astype(float).to_numpy()
        self._last = float(values[-1])
        self._cum_logret = 0.0

        feats = _lorentzian_features(values)
        logret = np.diff(np.log(values), prepend=np.log(values[0]))
        n = len(values)

        # candidate rows i must have complete features AND a known h-step
        # outcome (i + horizon < n); the query is the LAST complete row
        valid = ~np.isnan(feats).any(axis=1)
        if not valid[-1]:
            return self  # not enough warm-up for a query vector
        candidates = [i for i in range(n - horizon) if valid[i]]
        if len(candidates) < LKNN_MIN_LIBRARY:
            return self

        lib = feats[candidates]
        # z-score each feature over the library (train-only statistics)
        mean = np.nanmean(lib, axis=0)
        std = np.nanstd(lib, axis=0)
        std[std < 1e-12] = 1.0
        lib_z = (lib - mean) / std
        query_z = (feats[-1] - mean) / std

        # Lorentzian distance: log damping tames outlier feature values
        dists = np.sum(np.log1p(np.abs(lib_z - query_z)), axis=1)

        # chronological spacing: walk candidates by ascending distance but
        # skip any within `spacing` rows of an already-picked neighbor, so a
        # single historical episode cannot dominate the vote
        order = np.argsort(dists, kind="stable")
        chosen: list[int] = []
        chosen_rows: list[int] = []
        for idx in order:
            row = candidates[idx]
            if all(abs(row - r) >= self.spacing for r in chosen_rows):
                chosen.append(idx)
                chosen_rows.append(row)
                if len(chosen) >= self.m:
                    break
        if not chosen:
            return self

        outcomes = np.array([
            float(np.sum(logret[candidates[i] + 1 : candidates[i] + 1 + horizon]))
            for i in chosen
        ])
        weights = 1.0 / (dists[chosen] + 1e-9)
        cum = float(np.average(outcomes, weights=weights))
        if np.isfinite(cum):
            self._cum_logret = cum
        return self

    def predict_point(self) -> float:
        assert self._last is not None, "fit() first"
        return self._last * float(np.exp(self._cum_logret))


# --- kalman local linear trend ----------------------------------------------

KALMAN_MIN_POINTS = 60
KALMAN_MAX_ITER = 60


class KalmanTrendModel(ForecastModel):
    """Local-linear-trend Kalman filter on log prices (statsmodels UC)."""

    name = "kalman_llt"

    def __init__(self) -> None:
        self._forecast: Optional[float] = None

    def fit(self, series: pd.Series, horizon: int) -> "KalmanTrendModel":
        values = series.astype(float).to_numpy()
        self._forecast = float(values[-1])  # naive unless the fit succeeds
        if len(values) < KALMAN_MIN_POINTS or np.any(values <= 0):
            return self
        try:
            from statsmodels.tsa.statespace.structural import UnobservedComponents

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = UnobservedComponents(np.log(values), level="local linear trend")
                fit = model.fit(disp=0, maxiter=KALMAN_MAX_ITER)
                pred = float(fit.forecast(horizon)[-1])
            if np.isfinite(pred):
                self._forecast = float(np.exp(pred))
        except Exception:
            pass  # keep the naive fallback
        return self

    def predict_point(self) -> float:
        assert self._forecast is not None, "fit() first"
        return self._forecast


register("lorentzian_knn", LorentzianKNNModel)
register("kalman_llt", KalmanTrendModel)


# --- monte carlo probabilities ------------------------------------------------

MC_PATHS = 2000
MC_BLOCK = 5           # moving-block bootstrap block length
MC_MIN_RETURNS = 30
MC_SEED = 42           # deterministic: same inputs -> same probabilities


def mc_probabilities(
    series: pd.Series, horizon: int, cost_pct: float
) -> Optional[dict]:
    """Bootstrap Monte Carlo outcome probabilities for an h-step horizon.

    Moving-block bootstrap (block=5) over historical log returns preserves
    short-range volatility clustering; 2000 paths of ``horizon`` steps give
    the distribution of cumulative returns, reported as:

    * ``p_up`` — P(cumulative return > 0)
    * ``p_gain_over_cost`` — P(return > cost_pct), i.e. a buy round-trip pays
    * ``p_loss_over_cost`` — P(return < -cost_pct)
    * ``sim_p05_pct`` / ``sim_median_pct`` / ``sim_p95_pct`` — simulated cone

    Returns None when history is too short. Deterministic (fixed seed).
    """
    values = series.astype(float).to_numpy()
    if len(values) < MC_MIN_RETURNS + 1:
        return None
    returns = np.diff(np.log(values))
    if len(returns) < MC_MIN_RETURNS:
        return None

    rng = np.random.default_rng(MC_SEED)
    n = len(returns)
    block = min(MC_BLOCK, n)
    n_blocks = int(np.ceil(horizon / block))
    # sample block start indices for all paths at once
    starts = rng.integers(0, n - block + 1, size=(MC_PATHS, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(MC_PATHS, -1)
    cum = returns[idx[:, :horizon]].sum(axis=1)

    cost = float(cost_pct) / 100.0
    # thresholds in log-return space; log1p(-cost) requires cost < 1 (always
    # true for percent trading costs)
    gain_thr = float(np.log1p(cost))
    loss_thr = float(np.log1p(-cost)) if cost < 1.0 else -cost

    def to_pct(x: float) -> float:
        return round(float(np.expm1(x)) * 100.0, 4)

    return {
        "p_up": round(float(np.mean(cum > 0)), 4),
        "p_gain_over_cost": round(float(np.mean(cum > gain_thr)), 4),
        "p_loss_over_cost": round(float(np.mean(cum < loss_thr)), 4),
        "sim_p05_pct": to_pct(float(np.quantile(cum, 0.05))),
        "sim_median_pct": to_pct(float(np.quantile(cum, 0.5))),
        "sim_p95_pct": to_pct(float(np.quantile(cum, 0.95))),
        "n_paths": MC_PATHS,
    }
