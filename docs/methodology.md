# Prediction, Backtesting, and Signal Methodology

## Guiding principles

1. **Time is sacred.** No random train/test splits, ever. All evaluation is walk-forward (rolling-origin): fit on data up to time *t*, forecast *t+h*, roll forward, aggregate.
2. **Naive is the gate.** A model is only activated for a horizon if it beats the naive last-value baseline (sMAPE) on the same walk-forward folds. If nothing beats naive, naive is served — honestly labeled.
3. **Model returns, not levels.** ML models predict h-step log-returns; tree models cannot extrapolate price levels of a trending series.
4. **Uncertainty is a first-class output.** Every forecast ships with an empirical prediction interval, a confidence score, and freshness warnings.

## Features (`feature_engineering.py`)

Computed point-in-time (using only data with `observed_at <= as_of`; enforced by a leakage-guard assertion that tests exercise): lagged prices and returns (1,2,3,5,10,20), rolling mean/std (5,10,20), momentum, RSI(14), local premium and its 30-day z-score, USD/IRT returns, XAUUSD returns, calendar features (day-of-week, Jalali month), and data-quality flags.

## Model candidates

| Model | Type | Why included |
|---|---|---|
| Naive last value | baseline | The bar every model must clear |
| SMA(k) | baseline | Smoothed baseline |
| SES / Holt | statistical | Strong on short noisy series |
| ARIMA (small AIC grid) | statistical | Classical benchmark, good short-horizon behavior |
| Ridge regression | ML | Linear signal extraction from features |
| Random Forest | ML | Non-linear interactions, robust |
| Gradient Boosting | ML | Usually the strongest tabular learner |
| Theta | statistical | Top family of the M4 competition |
| Holt (damped trend) | statistical | Avoids runaway trend extrapolation |
| SARIMAX + exogenous | statistical | Lets USD/IRT and XAU returns inform the gold forecast (exog point-in-time lagged, held constant over the horizon) |
| Quantile Gradient Boosting | ML | Learns its own 5/50/95% quantiles → native prediction intervals |
| HistGradientBoosting | ML | Fast strong tabular learner (early stopping) |
| k-NN pattern analogue | ML | Forecasts from the 25 most similar historical 20-day return patterns |
| Ensemble | meta | Inverse-sMAPE-weighted blend of validated models (weights shift to live accuracy once ≥20 matured predictions per member) |

**Deliberately excluded** (documented decision, revisit when justified): LSTM/GRU, temporal transformers and Prophet. With a single series of a few thousand daily points, recurrent nets tend to collapse into echoing the last value while appearing "99% accurate"; the M4/M5 competition literature shows simple statistical methods and boosted trees dominate this regime. Excluding torch/tensorflow also keeps the image small and the attack surface low. See `docs/repo-review.md` for citations.

## Horizons

`1h, 4h, eod, 1d, 3d, 7d, 30d`. Intraday horizons are enabled only when ≥14 days of hourly coverage exists; daily horizons require ≥120 daily points. Disabled horizons are reported as such rather than served with garbage.

## Validation metrics

Per fold and aggregated: MAE, RMSE, sMAPE, directional accuracy (with a ±0.15% "flat" band, compared against the always-up baseline), and prediction-interval empirical coverage. Metrics stored in `model_versions.metrics` next to `baseline_metrics` for the same folds.

## Prediction intervals

Conformal-style: the empirical quantiles (5%/95%) of walk-forward validation residuals per horizon are applied around the point forecast. Coverage is itself validated; if realized coverage drifts, the model is flagged degraded.

## Regime detection

20-day trend slope and volatility percentile classify each moment as `trending_up`, `trending_down`, `ranging`, or `high_volatility`. Regime is attached to predictions and backtest results are broken down by regime.

## Backtesting (`backtest/engine.py`)

Walk-forward simulation, never using information from after the decision time. Configurable: transaction fee %, buy/sell spread %, slippage %, minimum holding period, position sizing. Compared against buy-and-hold, no-action, and SMA20/50 crossover. Reported: total & annualized return, win rate, profit factor, max drawdown, number of trades, average trade return, a Sharpe-like ratio (labeled as such — daily sampling, no risk-free adjustment), directional accuracy, results per regime, and gross vs net of costs. **Backtested performance does not guarantee future performance.**

## Signal engine (`signals/engine.py`)

The Buy/Hold/Sell signal is **not** just the forecast sign. A 0–100 bullishness score combines weighted factors: expected return vs round-trip cost, model confidence and cross-horizon agreement, trend (price vs SMA20/50), RSI zones, momentum, premium z-score (an abnormally high local premium argues against buying), volatility regime, and data freshness. Stale data forces `hold` with an explicit warning — the system never silently acts on old data.

Mapping: ≥75 strong_buy · ≥60 buy · 40–60 hold · ≤40 sell · ≤25 strong_sell.

Every signal includes: plain-language explanation, supporting and conflicting indicators, main risks, an invalidation condition ("this view is wrong if …"), and a recommended review time. Wording is calibrated ("conditions currently favor…", "the model estimates…") — never a guarantee.

## Live accuracy loop

Matured predictions get `actual_value` backfilled; live accuracy per horizon is shown on the Models page next to backtest metrics, and model-degradation alerts fire when live error exceeds 1.5× the baseline.
