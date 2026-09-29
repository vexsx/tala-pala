-- Reverse 0033: the flagged forming bars are 'ok' again, exactly the rows the
-- up migration flagged (the backfill never writes a 'suspect' row, and a row
-- the backfill has since replaced is no longer collected before its stamp).
UPDATE prices
SET quality = 'ok'
WHERE source = 'yahoo_backfill'
  AND observed_at > collected_at
  AND quality = 'suspect';
