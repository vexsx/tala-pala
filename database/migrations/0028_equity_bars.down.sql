-- Reverse of 0028. Dropped children-first so the foreign keys into
-- equity_instruments do not have to be relied on for ordering, and the
-- provider row goes with them: nothing else in the schema references
-- 'tsetmc_cdn', and leaving an enabled provider behind whose tables are gone
-- would show an operator a data source this database can no longer store.
DROP TABLE IF EXISTS equity_adjustments;
DROP TABLE IF EXISTS corporate_actions;
DROP TABLE IF EXISTS equity_bars;
DROP TABLE IF EXISTS equity_instruments;

DELETE FROM data_providers WHERE code = 'tsetmc_cdn';
