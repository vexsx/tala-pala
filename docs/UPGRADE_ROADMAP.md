# tala-pala upgrade — audit, baseline and roadmap

Produced 2026-07-25 from a six-lens audit in which agents ran empirical probes
(Monte-Carlo coverage studies, fold-geometry measurements, prediction-vector
comparisons) rather than reading code alone. Every claim below was verified
against the source or the production database; measurements are quoted.

---

## A. Repository audit

### A.1 Baseline (measured, pre-change)

**Data coverage** (`prices`, quality='ok', 2026-07-25):

| symbol | rows before | daily span | note |
|---|---|---|---|
| IR_GOLD_18K | 2664 | 2022-04 → now | 1206 daily buckets |
| XAUUSD | 2254 | 2021-12 → now | seeded |
| USD_IRT | 2080 | 2022-04 → now | seeded |
| XAGUSD | 1250 | **5 days** | live collection only |
| DXY | 1137 | **5 days** | live collection only |
| BRENT_OIL | 768 | **5 days** | live collection only |
| US10Y | **5** | **1 day** | effectively absent |
| IR_GOLD_FUND_* | 8 each | 3 days | quota-limited feed |

Hourly data exists only since 2026-07-19 (~1322 distinct hours, most of them
seeded dailies). **This is the binding constraint on the whole system**: macro
features could not be trained or ablated, and the hourly horizons are
data-starved — which is why naive legitimately wins 1h/4h.

**Models** (active, run 13–15): 18 candidates + ensemble, per symbol × horizon.
Naive won 4/7 Tehran horizons and 4/7 XAU horizons.

**Validation**: expanding-window walk-forward, ≤40 folds, min 60 training
points, selection/holdout split 70/30 (added in Addendum 14).

**Runtime**: full two-symbol training 35–40 min; DB 21 MB; model artifacts small.

### A.2 What is already strong

- Point-in-time exog truncation inside folds (`_pit` cutoff = train slice end).
- Target purging via `dropna` — no target leakage into fitted rows.
- Scalers inside `make_pipeline`, so they are fit per fold, not globally.
- Naive-baseline activation gate, and the honest habit of letting naive win.
- Maturity matching floored at `predicted_at` (Addendum 13).
- Ensemble weights from selection folds only (Addendum 14).
- Provider fallback with MAD-based suspect quarantine.

### A.3 Confirmed weaknesses (with the measurement that proved them)

| Sev | Finding | Measurement |
|---|---|---|
| **CRIT** | `np.quantile` interval has no finite-sample guarantee | nominal-90% band realized **0.72–0.78** at n=8–12 |
| **CRIT** | No embargo between selection and holdout | **30×** target overlap at n=120,h=30; holdout wall-span shorter than one horizon |
| **CRIT** | No significance/multiplicity control | bare `argmin` over 18 candidates, strict `<` |
| **CRIT** | Signal ignores the live dealer spread | hurdle fixed at 2.2% while UI used ~0.49% |
| **CRIT** | Monte Carlo unconditional on the forecast | identical odds for +5% and −5% forecasts |
| **CRIT** | Signal fabricates model backing when predictions are missing | pure-TA score reached 83 → "strong_buy" at hardcoded 0.5 confidence |
| **HIGH** | Deployed `hist_gb_tuned` ≠ validated model | re-tuned on full series; tuning tail overlapped holdout by **~198 rows** |
| **HIGH** | `hist_gb_tuned` ≡ `hist_gb` | `TUNE_MIN_ROWS=60` unreachable (24–32 clean rows); max |pred diff| = **0.0**; also double-weighted the ensemble |
| **HIGH** | Tabular candidates silently degenerate to naive early | bit-identical to naive until n_train ≥ **89** at h=30 |
| **HIGH** | Client retry duplicates in-flight Python jobs | sync handlers cannot be cancelled by disconnect |
| **HIGH** | Provider dispersion is structurally dead | same-cycle duplicates dropped before any DB write |
| **MED** | `eod` train/serve mismatch | trained ~27–46h out, graded 5–18h out |
| **MED** | Partial-bar skew at predict time | expected move shifts **0.13–0.16pp** > the 0.15% flat band |
| **MED** | Bar-steps vs wall-clock targets | XAU 30-step model validated over ~35 calendar days |
| **MED** | `interval_coverage` scored on 0–2 folds, hard 0.0 | "0% coverage" shown for never-scored models |
| **MED** | Metrics are write-only | no Prometheus scraper exists anywhere in compose |
| **LOW** | `hour` feature constant on daily frame; `dow` duplicated | dead split candidates |

### A.4 Data limitations (honest)

- No news/event history at all; a news subsystem starts accumulating from zero.
- Macro history is vendor daily closes — no vintage/revision data, so
  revision-sensitive series (CPI, payrolls) cannot be used without an
  ALFRED-style vintage source (needs a free API key the deployment lacks).
- Iranian-side sources are thin: one dealer with two-sided quotes, one FX
  proxy, a quota-limited exchange feed.
- Fund data (8 rows) is far too sparse to model.

### A.5 Operational limitations

- Single host, single replica; in-memory lockout/limiter state resets on deploy.
- 1.5 GB memory cap on the prediction service — rules out transformer-class models.
- No metric scraper, so all instrumentation is currently dead code.
- No Docker/migration/compose validation in CI.

---

## B. Prioritized roadmap

### P0 — correctness and leakage (all implemented in Addendum 15)

| Item | Benefit | Risk | Complexity | Services | Migration | Rollback |
|---|---|---|---|---|---|---|
| Split-conformal intervals | Honest 90% coverage (0.78→0.91) | Wider bands | M | Python | none | revert `intervals.py` |
| Fold embargo | Holdout genuinely out-of-sample | Fewer folds | S | Python | none | revert `split_folds` |
| Significance gate (edge + bootstrap + degeneracy) | Stops activating luck | More naive winners | M | Python | none | lower `MIN_EDGE_PCT` |
| Selection-only hyperparameters | Deployed model = validated model | none | M | Python | none | revert `prepare_params` |
| Unified cost source | Signal and UI stop contradicting | Threshold shifts | S | Python | none | revert `costs.py` |
| Monotone signal score | No 12-point cliff | Scores shift | S | Python | none | revert branch |
| Forecast-conditioned Monte Carlo | Odds mean what the UI says | none | S | Python | none | pass `None` |
| Technical-only disclosure | No fabricated model backing | none | S | Python | none | revert |
| Partial-bar guard + bar-aligned targets | Direction no longer depends on cron hour | Base/anchor change | M | Python | none | revert helpers |
| Exogenous backfill | Macro features become trainable | none | S | Python | none | delete rows by source |

### P1 — highest expected accuracy/reliability gain

| Item | Benefit | Risk | Complexity | Services | Migration | Status |
|---|---|---|---|---|---|---|
| Revive provider dispersion | Real quote-uncertainty signal | more rows | M | Python | none | in progress |
| Microstructure feature module | Spread/dispersion/premium-decomposition features | none (not yet wired) | L | Python | none | in progress |
| Job-retry idempotency | No duplicate concurrent jobs | none | S | Go | none | in progress |
| Ablation harness (price / +macro / +microstructure / +news) | Decides what actually helps | none | L | Python | maybe | **not started** |
| Wire microstructure into training after ablation | Accuracy, if proven | overfitting | M | Python | none | **not started** |

### P2 — product and explainability

| Item | Benefit | Complexity | Status |
|---|---|---|---|
| News/event schema + Fed RSS ingestion + taxonomy | Substrate for event studies | L | in progress |
| Event-study tooling (returns/vol around events) | Measures whether news matters | L | **not started** |
| Forecast factor decomposition in the UI | Explains drivers | M | **not started** |
| Calibration/coverage panel | Shows interval health honestly | M | **not started** |
| Prometheus scraper + dashboards | Makes metrics real | M | **not started** |

### P3 — research (do not adopt without evidence)

- Conformalized quantile regression per horizon (needs more folds).
- Regime detection via HMM / Bayesian online change-point, versioned so a past
  timestamp always gets the label available at that time.
- Champion/challenger with shadow predictions and automatic rollback.
- Deep sequence models — **rejected for now**: ~1200 daily points and a 1.5 GB
  CPU-only host make N-BEATS/TFT/PatchTST overfitting machines here.

---

## C. Honest limitations that will remain

- Holdout is ~12 folds: its verdicts are noisy and will sometimes reject a good
  model or admit a lucky one.
- Naive remains the correct winner for several horizons, especially hourly.
- News signals are noisy; sentiment is not causality; event classifications can
  be wrong; policy and FX shocks are not forecastable from price history.
- New sources can disappear or change format without notice.
- Improvements are horizon-specific; nothing here makes 1h predictable.
