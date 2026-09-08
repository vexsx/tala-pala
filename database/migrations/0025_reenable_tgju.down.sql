-- Restore the 0023 state: tgju disabled with its stated reason.
UPDATE data_providers
SET enabled = FALSE,
    priority = 10,
    consecutive_failures = 0,
    last_error = 'disabled by default: tgju.org answers scripted clients with access denied (2047 consecutive failures since 2026-08-02)',
    updated_at = now()
WHERE code = 'tgju';
