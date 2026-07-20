# GitHub Repository Review & Methodology Research

Research date: **2026-07-20**. Facts verified against GitHub pages, the GitHub API, raw source files, and literature search. This document records which repositories were reviewed, which ideas were reused or rejected, licensing considerations, security concerns, and model limitations, as required by the project brief.

## 1. Requested repositories

### 1.1 `mashmool0/gold-price-predection`
https://github.com/mashmool0/gold-price-predection

- **What it does:** Hobby project: Selenium scraper pulling daily gold OHLCV from TradingView, an LSTM notebook (adapted from Kaggle) on 2007–2023 data, and a tkinter/pyautogui "trader assistant" GUI.
- **Code quality:** Poor. Almost entirely Jupyter notebooks; the LSTM notebook errors out mid-execution (date parsing fails); hardcoded local paths; no tests; no pinned requirements.
- **License:** **None** → all rights reserved. **No code may be reused.** Ideas only.
- **Maintenance:** Last push Aug 2024; effectively abandoned.
- **Security:** No leaked keys, but Selenium/pyautogui desktop automation is fragile, and scraping TradingView likely violates its ToS.
- **Reused:** the *concept* of a live price monitor (idea only).
- **Rejected:** the "98.9% accuracy" claim (this is ~`100 − MAPE` on a slow-moving price level, which a naive "tomorrow = today" forecast also achieves — the classic LSTM lag-mimic trap); TradingView scraping; unrunnable notebooks as a delivery format; LSTM on a single short series.

### 1.2 `itzdineshx/Gold-price-prediction`
https://github.com/itzdineshx/Gold-price-prediction

- **What it does:** Predicts gold price in INR from the USD/INR rate; Gradio demo app. Linear Regression (R² 0.724), Ridge, Random Forest on ~53 weekly observations.
- **Code quality:** Decent repo hygiene (src/data/models split, requirements.txt, README) but flawed methodology: it is **nowcasting, not forecasting** — it maps *same-day* USD/INR to *same-day* gold price, so it cannot predict tomorrow without already knowing tomorrow's exchange rate. Tiny dataset. A stray `.gradio/certificate.pem` is committed (public CA cert, but sloppy).
- **License:** MIT. **Maintenance:** one-shot (Feb 2025), unmaintained.
- **Security:** no secrets found; committed `.pkl` binaries are an arbitrary-code-execution vector if third-party pickles are ever loaded.
- **Reused:** repo layout discipline; comparing several simple models instead of one black box.
- **Rejected:** same-day-covariate design; R² on price *levels* as headline metric (near-unit-root series inflate R²); Random Forest on 53 points; committing model binaries to git.

### 1.3 `samadpls/GoldPredictAPI`
https://github.com/samadpls/GoldPredictAPI

- **What it does:** RandomForest on the classic Kaggle GLD dataset (2008–2018, 2,290 rows), served with FastAPI + Pydantic + Swagger.
- **Code quality:** Clean but minimal (4 commits). The API layer is the best part; the ML layer is textbook-flawed.
- **License:** MIT. **Maintenance:** single-day project (Jan 2024), unmaintained.
- **Security:** `pickle.load()` of a model file (ACE risk if swapped); raw exception text returned in HTTP 500s (detail leakage); no auth, no rate limiting.
- **Reused:** the FastAPI + Pydantic serving pattern (with fixes: `joblib` from a trusted internal path only, sanitized errors, service-to-service token auth — all implemented in our `prediction-python` service).
- **Rejected:** **`train_test_split(..., shuffle default)` on a time series** — test rows interleaved with training rows means the model sees the future and every reported score is leakage-inflated. Also rejected: same-day GLD-from-SLV nowcasting (nearly an identity mapping), the 2018-frozen dataset, and tree models predicting price *levels* (trees cannot extrapolate beyond the training range — we model returns instead).

## 2. Additional repositories evaluated

| Repo | Why it matters | Verdict |
|---|---|---|
| [amirh0ss3in/tgju_api](https://github.com/amirh0ss3in/tgju_api) (MIT) | Reference implementation of the unofficial TGJU endpoints for Iranian gold/currency series. | Used as a *reference* for endpoint shapes only; we vendor and maintain our own defensive parser. TGJU has no official API, so all such repos scrape — we add health checks and fallbacks. |
| [margani/pricedb](https://github.com/margani/pricedb) (MIT, active Apr 2026) | GitHub-Actions-refreshed historical database of IRR free-market rates incl. gold. | Best find for Iranian data: usable for backfilling history and as a backup source. |
| [Nixtla/statsforecast](https://github.com/Nixtla/statsforecast) (Apache-2.0, active) | Production-grade AutoARIMA/AutoETS/Theta + correct baselines + conformal intervals. | Methodological blueprint. We implement the same walk-forward + baseline-gate pattern with statsmodels/sklearn to keep the dependency footprint small; adopting statsforecast later is a documented upgrade path. |
| [unit8co/darts](https://github.com/unit8co/darts) / [sktime](https://github.com/sktime/sktime) (Apache-2.0, active) | `historical_forecasts()` / `SlidingWindowSplitter` = free walk-forward backtesting. | Same as above — pattern adopted, dependency not (heavy). |

Noted in passing: `rodgdutra/CNN-LSTM_gold_price` (paper implementation, useful if a deep-learning comparison is ever justified) and `FarzadNekouee/Gold-Price-Prediction-LSTM` (better-documented LSTM walkthrough, same genre and same pitfalls).

## 3. Licensing summary

- Repo 1.1 has **no license** — nothing copied. Repos 1.2/1.3 are MIT — patterns adopted, no code copied verbatim.
- Our stack uses Apache-2.0/MIT/BSD dependencies only (FastAPI, SQLAlchemy, pandas, numpy, scikit-learn, statsmodels, chi, pgx, React, Vite, Recharts). statsmodels is BSD-3. No GPL runtime dependencies.

## 4. Techniques adopted (literature-backed)

- **Walk-forward (rolling-origin) validation only; never random splits.** Scalers/transforms fitted inside each fold on training data only.
- **Naive baseline as a hard gate:** a model is activated only if it beats naive on the same walk-forward folds (M4 competition: pure-ML entries mostly failed to beat simple statistical benchmarks — [M4 results, IJF](https://www.sciencedirect.com/science/article/abs/pii/S0169207018300785), [Makridakis et al., PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0194889)).
- **ARIMA/ETS are first-class citizens** for a single short daily series; ML wins mainly with many related series ([M5 results](https://www.sciencedirect.com/science/article/pii/S0169207021001874)).
- **Gradient boosting / RF on lagged tabular features, target = log-returns** (not levels — trees can't extrapolate levels; and gold 2026 levels are outside any 2008–2018 training support).
- **No LSTM/transformer** until something beats naive convincingly: with one series of a few thousand points, LSTMs commonly degenerate into echoing the last observation ([DL time-series survey, arXiv:2401.13912](https://arxiv.org/pdf/2401.13912)).
- **Prediction intervals via empirical residual quantiles (conformal-style)**, with out-of-sample coverage verified in validation ([conformal benchmarking, arXiv:2601.18509](https://arxiv.org/pdf/2601.18509), [awesome-conformal-prediction](https://github.com/valeman/awesome-conformal-prediction)).
- **Directional accuracy reported with care:** compared against the always-up baseline, near-zero moves treated as a "flat" band, never used as the sole metric.

## 5. Model limitations (disclosed in-app)

- Gold in toman is driven heavily by the free-market USD rate, which reacts to political news that no time-series model anticipates.
- Backtested performance does not guarantee future performance; regime changes (sanctions news, currency shocks) invalidate learned patterns.
- Short-horizon (1h/4h) models are only enabled when sufficient intraday history exists; they remain noisier than daily models.
- Prediction intervals are calibrated on historical residuals; tail events (devaluations) exceed them.
