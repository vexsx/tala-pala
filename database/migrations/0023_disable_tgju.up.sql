-- 0023: Disable the TGJU provider by default.
-- call2/call3/call4.tgju.org have answered scripted clients with "access
-- denied" since 2026-08-02 10:55 — 2047 consecutive failures as of
-- 2026-08-13, with no successful fetch in between. The Addendum 19 circuit
-- breaker keeps the cost down (one attempt an hour once the cooldown caps),
-- but an enabled provider that cannot succeed makes the roster lie: the
-- Issues tab and /internal/providers/health both report it as a failing
-- source to chase rather than a source that is gone.
--
-- Same treatment as stooq in 0005, for the same reason and with the same
-- reversibility. The adapter, its tests and the provider's history all stay;
-- re-enable manually if TGJU restores public access:
--   UPDATE data_providers SET enabled = TRUE WHERE code = 'tgju';
--
-- Coverage after this (verified against the registry rows and each adapter's
-- symbol map): IR_GOLD_18K -> hamrahgold (priority 1) then milligold (5);
-- USD_IRT -> bitmax (1, 24/7 USDT market); XAUUSD -> yahoo (10) with
-- gold_api/metals_dev behind it; IR_COIN_EMAMI -> alanchand (20), brsapi (25),
-- pricedb (30). Nothing loses its only source, and nothing changes in
-- practice — a provider failing 2047 times in a row was already contributing
-- no data. app/seed/seed_history.py constructs TGJUProvider directly rather
-- than through the registry, so one-off history seeding is unaffected by the
-- enabled flag.
--
-- consecutive_failures is reset for the same reason 0005 reset stooq's: the
-- counter is the circuit breaker's input, and a provider that is manually
-- re-enabled deserves a fair first attempt instead of an hour-long cooldown
-- inherited from an outage that has since been fixed.

UPDATE data_providers
SET enabled = FALSE,
    consecutive_failures = 0,
    last_error = 'disabled by default: tgju.org answers scripted clients with access denied (2047 consecutive failures since 2026-08-02)',
    updated_at = now()
WHERE code = 'tgju';
