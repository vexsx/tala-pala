-- Reverse 0026. The DELETE is not tidying: without it, every non-gold signal
-- would lose the only field distinguishing it and survive as an indistinguish-
-- able IR_GOLD_18K row — a silver or dollar recommendation served as a gold
-- one. Same reasoning as 0010's down migration, which deletes non-gold
-- model_versions before dropping that column.
DROP INDEX IF EXISTS idx_signals_symbol_time;
DELETE FROM signals WHERE symbol <> 'IR_GOLD_18K';
ALTER TABLE signals DROP COLUMN IF EXISTS symbol;
