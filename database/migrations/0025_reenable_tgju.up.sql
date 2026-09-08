-- 0025: Re-enable the TGJU provider. Migration 0023 disabled it; the outage
-- that justified 0023 is over.
--
-- 0023 disabled tgju on 2026-08-13 after 2047 consecutive failures: the live
-- snapshot hosts had answered scripted clients with "access denied" since
-- 2026-08-02. That was correct at the time — an enabled provider that cannot
-- succeed makes the roster lie.
--
-- Re-tested 2026-09-08 FROM THE PRODUCTION HOST ITSELF (85.137.30.142), which
-- is the only machine whose reachability matters, because 0023's cause was an
-- IP-level block rather than a global shutdown:
--
--   https://call2.tgju.org/ajax.json   http=200  bytes=179253
--   https://call3.tgju.org/ajax.json   http=200  bytes=179253
--   https://call4.tgju.org/ajax.json   http=200  bytes=179183
--   https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl
--                                      http=200  bytes=646272
--                                      recordsTotal: 3945 daily OHLC rows,
--                                      2011-11-26 .. 2026-09-07, in rials
--
-- All three live hosts and the history endpoint serve. Nothing about the
-- adapter changed; it kept its tests and its symbol map through the outage.
--
-- PRIORITY IS MOVED 10 -> 22, and that correction matters. Re-enabling tgju at
-- its old priority 10 would NOT have been source-neutral, contrary to the first
-- draft of this migration: tgju emits `sekee` -> IR_COIN_EMAMI
-- (app/providers/tgju.py:50), JOB_SYMBOLS['iran_gold'] contains IR_COIN_EMAMI,
-- and neither hamrahgold nor milligold emits the coin at all. collect.py walks
-- providers in (priority, code) order and stops once a symbol is satisfied, so
-- tgju at 10 would have taken IR_COIN_EMAMI away from alanchand (20) — an
-- unrequested change of primary source for a live symbol, bundled into an
-- unrelated feature.
--
-- At priority 22 every symbol keeps exactly the source it has today, verified
-- against each adapter's symbol map and JOB_PROVIDER_CATEGORIES:
--   IR_GOLD_18K   -> hamrahgold (1)   [tgju 22 is behind milligold 5]
--   IR_COIN_EMAMI -> alanchand  (20)  [20 < 22, unchanged]
--   USD_IRT       -> bitmax     (1)   [job 'fx' consults category fx first]
--   XAUUSD        -> yahoo      (10)  [job 'global' consults global_gold first]
-- tgju therefore returns strictly as a FALLBACK: it contributes only when the
-- current primary fails, which is the redundancy that has been absent for five
-- weeks. Every value it does contribute is normalised rial->toman and recorded
-- raw in raw_observations, as before.
--
-- consecutive_failures is reset for the same reason 0005 and 0023 reset theirs:
-- the counter is the circuit breaker's input, and a provider re-enabled on
-- fresh evidence deserves a fair first attempt rather than an hour-long
-- cooldown inherited from an outage that is over.
--
-- If it fails again the breaker will park it within five attempts, and
-- disabling it is one UPDATE — the same reversibility 0023 relied on.

UPDATE data_providers
SET enabled = TRUE,
    priority = 22,
    consecutive_failures = 0,
    last_error = NULL,
    last_error_at = NULL,
    updated_at = now()
WHERE code = 'tgju';
