-- Addendum 13: indexes for query paths that previously seq-scanned.
--
-- raw_observations is the largest-growth table (every datapoint from every
-- provider, 365-day retention):
--   * market-summary reads the latest hamrahgold spread on every dashboard
--     load (WHERE provider_code = ... ORDER BY observed_at DESC);
--   * the funds panel and the funds-slot guard filter provider_code;
--   * retention cleanup deletes WHERE collected_at < cutoff.
-- predictions grew a symbol column in 0010 but every symbol-filtered query
-- (API reads, live calibration, ensemble re-weighting) still relied on the
-- pre-multi-symbol (horizon, ...) indexes.

CREATE INDEX IF NOT EXISTS idx_raw_obs_provider_time
    ON raw_observations (provider_code, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_obs_collected_at
    ON raw_observations (collected_at);

CREATE INDEX IF NOT EXISTS idx_predictions_symbol_horizon_predicted
    ON predictions (symbol, horizon, predicted_at DESC);

CREATE INDEX IF NOT EXISTS idx_predictions_symbol_horizon_target
    ON predictions (symbol, horizon, target_time DESC)
    WHERE actual_value IS NOT NULL;
