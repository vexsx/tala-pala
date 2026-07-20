-- 0006: Alanchand requires a paid Bearer token (ALANCHAND_TOKEN); the keyless
-- HTML fallback no longer yields data (verified 2026-07-20: /api/gold 404,
-- no embedded prices). Clear the accumulated failure state; the provider now
-- reports "needs API key" until a token is configured.

UPDATE data_providers
SET consecutive_failures = 0,
    last_error = 'requires API token (set ALANCHAND_TOKEN)',
    updated_at = now()
WHERE code = 'alanchand';
