-- Reverse 0031: unregister the seven TGJU daily-close instruments and drop
-- `derived_from`.
--
-- Only what this migration added is removed. The `prices` rows the tgju_daily
-- job wrote (source 'tgju_history'), their raw_observations audit rows and its
-- app_settings register are deliberately left in place, the rule 0027's down
-- migration states: deleting measured history to reverse a REGISTRATION would
-- be a much larger act than the one being undone, and `prices` has no foreign
-- key into `instruments`, so the rows simply stop being served.
--
-- A link someone made from the equity roster to one of these codes is cut
-- first, because equity_instruments.instrument_code is ON DELETE RESTRICT and
-- would otherwise make this rollback fail halfway.
UPDATE equity_instruments
SET instrument_code = NULL,
    updated_at = now()
WHERE instrument_code IN ('IR_SILVER_999', 'IR_COIN_BAHAR', 'IR_COIN_HALF',
                          'IR_COIN_QUARTER', 'IR_COIN_GERAMI', 'IR_GOLD_24K',
                          'IR_GOLD_MESGHAL');

DELETE FROM instruments
WHERE code IN ('IR_SILVER_999', 'IR_COIN_BAHAR', 'IR_COIN_HALF',
               'IR_COIN_QUARTER', 'IR_COIN_GERAMI', 'IR_GOLD_24K',
               'IR_GOLD_MESGHAL');

-- Dropping the column drops its foreign key with it.
ALTER TABLE instruments
    DROP CONSTRAINT IF EXISTS instruments_derived_from_not_self,
    DROP COLUMN IF EXISTS derived_from;
