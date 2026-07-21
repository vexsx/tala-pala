DROP INDEX IF EXISTS idx_model_versions_symbol_active;
ALTER TABLE model_versions DROP CONSTRAINT IF EXISTS model_versions_unique;
DELETE FROM model_versions WHERE symbol <> 'IR_GOLD_18K';
ALTER TABLE model_versions DROP COLUMN IF EXISTS symbol;
ALTER TABLE model_versions
    ADD CONSTRAINT model_versions_unique UNIQUE (horizon, model_name, version);
