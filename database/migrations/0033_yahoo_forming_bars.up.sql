-- 0033: the Yahoo backfill's forming bars, stored as closes before the fix.
--
-- WHAT HAPPENED. app/jobs/backfill.py stamps every daily close 23:00 UTC on
-- the bar's own date, and during a session Yahoo's chart serves TODAY's bar
-- with its price so far. A run during the day therefore stored a close that
-- did not exist yet: measured on the local rehearsal of 2026-09-29, a run at
-- 12:18 UTC stored DXY 101.25 and BRENT_OIL 96.36 at 2026-09-29 23:00, and
-- from that stamp on /prices/current served Brent's forming price as the
-- day's close (-7.6% against 104.34). The code now skips such a bar; a row a
-- run before the fix stored was skipped by every later run too ("the day
-- already has a row"), so it stayed a wrong close for good.
--
-- WHY observed_at > collected_at IS THE PREDICATE. A backfill close is
-- stamped after its session and collected afterwards, so a yahoo_backfill row
-- collected BEFORE its own stamp was written from the future: on the
-- rehearsal it matches exactly those two rows and nothing else in `prices`.
--
-- WHAT THIS DOES. It flags them 'suspect' — every reader serves only
-- quality = 'ok' — so none is served from deploy on, and the next backfill run
-- (POST /internal/backfill/history) replaces each with its settled close and
-- sets it 'ok' again (backfill._replace_forming). Flagged rather than deleted
-- so the down migration is an exact inverse. raw_observations is left as it
-- was: its rows are the provider's record (what Yahoo served at 12:18), and
-- the live collector's suspect-run walk reads that provider's raw rows, which
-- a new 'suspect' there would join.
UPDATE prices
SET quality = 'suspect'
WHERE source = 'yahoo_backfill'
  AND observed_at > collected_at
  AND quality = 'ok';
