-- 0022: the whole stored US10Y series is a factor of ten too small.
--
-- WHAT HAPPENED. Yahoo's ^TNX ticker is published under two conventions: the
-- legacy CBOE index (ten times the yield, 46.82 = 4.682%) and the plain yield
-- (4.697 = 4.697%). The feed carried the SECOND form while
-- core.normalize.tnx_to_pct divided unconditionally by ten, so every US10Y
-- value ever stored is one tenth of the real yield. Measured 2026-08-13:
-- 1663 rows in `prices`, 2021-07-26 to 2026-08-11, spanning 0.1174 to 0.4988
-- and not one row above 2 — those are real yields of 1.17% to 4.99%. On
-- 2026-08-11 Yahoo switched to the index form; the correctly normalized 4.682
-- that then arrived read as a 896% jump against a last-good of 0.4697, so
-- validation held every new observation as suspect and the series stopped
-- dead. Two defects, one symptom: the scale was always wrong, and the
-- suspect guard had no exit for a symbol with one provider (fixed separately
-- in app/core/validation.py + app/jobs/collect.py).
--
-- WHY value < 1.0 IS THE SAFE PREDICATE. A broken row is true_yield / 10, and
-- the lowest yield anywhere in this series' span is 1.174%, so every broken
-- row sits at or below 0.499 while every corrected one starts at 1.174. The
-- band between them is empty, which makes the update idempotent (re-running
-- it finds nothing) and makes it impossible to touch a correctly normalized
-- row that lands after the code fix ships (~4.7 today). The observed_at cut
-- at 2026-08-12 fences the repair to rows that existed before the fix, so the
-- .down.sql can be an exact inverse rather than a guess.
--
-- WHY prices AND ONLY PART OF raw_observations. `prices` stores the
-- normalized value and is wrong throughout. `raw_observations` is the
-- provider record and holds two different kinds of row for this symbol:
--
--   * live collect rows (unit 'TNX_index'): raw_value is Yahoo's own number,
--     4.697 — a faithful record of what the provider said, and NOT wrong.
--     Rescaling those would falsify the audit trail that exists precisely so
--     a normalization can be checked against its input. They keep their
--     numbers; only the LABEL is corrected, because a quote of 4.697 was a
--     percent and calling it 'TNX_index' is the same misreading written in
--     the unit column. Rows at/above the 25% plausibility ceiling (the
--     post-flip 46.8x quotes) really are index values and stay labelled so.
--   * backfill rows (unit 'pct', raw_payload->>'kind' = 'backfill'): these
--     were written by app/jobs/backfill.py from the ALREADY normalized value,
--     so they carry the same tenfold error and are corrected with `prices`.
--
-- The 81 suspect rows collected since the flip are left exactly as they are:
-- they record the classification that was made at the time, which is the
-- history of the incident. The days missed in between are recoverable through
-- the idempotent POST /internal/backfill/history job.
--
-- DEPLOY ORDER: THE CODE SHIPS WITH OR BEFORE THIS MIGRATION. Never after.
--
-- This migration does not unfreeze anything by itself, and applied on its own
-- it re-freezes the series in the opposite direction. Yahoo is back on the
-- PLAIN-YIELD convention -- a live fetch of
-- query1.finance.yahoo.com/v8/finance/chart/^TNX on 2026-08-13 returned
-- regularMarketPrice 4.627, chartPreviousClose 4.682 -- so the old code's
-- unconditional /10 turns today's quote into 0.4627, and against the last
-- good value this migration has just rewritten to 4.697 that is a 90.1% jump.
-- Replayed through core.validation.classify_observation on the live quote:
--
--   code   migration       stored    verdict
--   old    not applied     0.4627    ok        (wrong scale, but flowing)
--   old    APPLIED         0.4627    suspect   jump of 90.1% vs 4.697  <-- frozen
--   new    not applied     4.6270    suspect   jump of 885.1% vs 0.4697
--   new    APPLIED         4.6270    ok
--
-- Only the two orders that put the code first are safe. Code-only is the
-- benign one: the first quotes are held as suspect, but US10Y has one
-- provider and no second source can arrive, so the repetition path
-- (validation.sustained_by_repetition, 5 observations over 90 minutes) accepts
-- the new level on its own at the 10-minute collect cron and the series
-- self-heals inside about an hour and a half, with a WARNING in app_issues
-- while it waits. Migration-only has no such exit: every subsequent quote is
-- another 90% jump against a level the code will never produce, so the series
-- stays shut until the code is deployed. In the compose stack the api applies
-- migrations at startup and prediction-service is recreated behind it, so a
-- full `docker compose up -d --build` is already safe; what is NOT safe is
-- `make migrate` (or redeploying the api alone) against an unchanged
-- prediction-service image.

UPDATE prices
SET value = value * 10
WHERE symbol = 'US10Y'
  AND observed_at < TIMESTAMPTZ '2026-08-12 00:00:00+00'
  AND value < 1.0;

UPDATE raw_observations
SET raw_value = raw_value * 10
WHERE symbol = 'US10Y'
  AND observed_at < TIMESTAMPTZ '2026-08-12 00:00:00+00'
  AND raw_payload ->> 'kind' = 'backfill'
  AND raw_value < 1.0;

UPDATE raw_observations
SET unit = 'pct',
    currency = 'PCT'
WHERE symbol = 'US10Y'
  AND observed_at < TIMESTAMPTZ '2026-08-12 00:00:00+00'
  AND raw_payload ->> 'kind' IS DISTINCT FROM 'backfill'
  AND currency = 'INDEX'
  AND raw_value <= 25.0;
