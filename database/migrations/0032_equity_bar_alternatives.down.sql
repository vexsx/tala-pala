-- Reverse 0032. The alternatives are evidence beside the stored bars, not the
-- bars themselves; dropping them only means the next ingest can no longer
-- tell a contested break from a corporate action, and detects it as one.
DROP TABLE IF EXISTS equity_bar_alternatives;
