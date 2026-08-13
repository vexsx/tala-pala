-- Restore the tenfold-too-small US10Y series exactly as 0022 found it.
--
-- Each predicate is the inverse of its .up.sql partner over the same fenced
-- row set (symbol US10Y, observed_at before the 2026-08-12 cut). After the up
-- migration those rows span 1.174 to 4.988, so `< 10.0` selects all of them
-- and nothing else; rows collected after the cut were never touched and are
-- not touched here either.
--
-- DEPLOY ORDER, MIRRORED. The .up.sql header states the rule: the code ships
-- with or before the migration. Rolling back inverts it -- the code must be
-- rolled back with or BEFORE this file, because core.normalize.tnx_to_pct at
-- HEAD reads Yahoo's current plain-yield quote (4.627 on 2026-08-13) as
-- 4.627 while this file puts the stored history back at ~0.47, which is the
-- 885% jump that started the incident. That direction at least has an exit:
-- the repetition path in collect.py re-levels a single-source series in about
-- 90 minutes. Applying only the .up.sql against the OLD code has none.

UPDATE prices
SET value = value / 10
WHERE symbol = 'US10Y'
  AND observed_at < TIMESTAMPTZ '2026-08-12 00:00:00+00'
  AND value < 10.0;

UPDATE raw_observations
SET raw_value = raw_value / 10
WHERE symbol = 'US10Y'
  AND observed_at < TIMESTAMPTZ '2026-08-12 00:00:00+00'
  AND raw_payload ->> 'kind' = 'backfill'
  AND raw_value < 10.0;

-- Backfill rows were always labelled ('pct','PCT') and must keep that label;
-- the kind test is what separates them from the live rows relabelled above.
UPDATE raw_observations
SET unit = 'TNX_index',
    currency = 'INDEX'
WHERE symbol = 'US10Y'
  AND observed_at < TIMESTAMPTZ '2026-08-12 00:00:00+00'
  AND raw_payload ->> 'kind' IS DISTINCT FROM 'backfill'
  AND currency = 'PCT';
