-- Multi-symbol forecasting (Addendum 8): the model registry becomes
-- per-symbol so XAUUSD (global gold) trains alongside IR_GOLD_18K.
ALTER TABLE model_versions
    ADD COLUMN symbol TEXT NOT NULL DEFAULT 'IR_GOLD_18K';

ALTER TABLE model_versions
    DROP CONSTRAINT IF EXISTS model_versions_unique;
ALTER TABLE model_versions
    ADD CONSTRAINT model_versions_unique UNIQUE (symbol, horizon, model_name, version);

CREATE INDEX IF NOT EXISTS idx_model_versions_symbol_active
    ON model_versions (symbol, horizon) WHERE is_active;
