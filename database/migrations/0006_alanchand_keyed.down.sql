UPDATE data_providers
SET last_error = NULL, updated_at = now()
WHERE code = 'alanchand';
