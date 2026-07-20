-- 0005: Disable the Stooq provider by default.
-- Stooq now serves a JavaScript anti-bot challenge to scripted clients
-- (verified 2026-07-20: /q/l/ returns 404/challenge), so every collection
-- cycle fails with noise. The adapter is kept; re-enable manually if Stooq
-- restores plain CSV access:
--   UPDATE data_providers SET enabled = TRUE WHERE code = 'stooq';

UPDATE data_providers
SET enabled = FALSE,
    consecutive_failures = 0,
    last_error = 'disabled by default: anti-bot challenge blocks scripted CSV access',
    updated_at = now()
WHERE code = 'stooq';
