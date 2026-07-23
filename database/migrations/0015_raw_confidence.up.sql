-- Addendum 14: pre-gate confidence for meta-gate training.
--
-- The meta-gate previously trained on predictions.confidence, which already
-- contains the PREVIOUS gate's 50/50 blend — a self-referential feature that
-- drifts as the gate updates. raw_confidence stores the confidence as it was
-- BEFORE the gate touched it (validation + live-calibration blend only), so
-- the gate learns from a stable input. Nullable: rows predating this
-- migration fall back to the blended value.
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS raw_confidence double precision;
