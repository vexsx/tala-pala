UPDATE data_providers
SET enabled = TRUE, last_error = NULL, updated_at = now()
WHERE code = 'stooq';
