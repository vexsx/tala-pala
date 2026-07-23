-- BitMax USDT/toman watcher becomes the PRIMARY free-market USD source
-- (24/7, keyless). TGJU's bazaar dollar remains the fallback.
INSERT INTO data_providers (code, name, base_url, category, priority, enabled)
VALUES ('bitmax', 'BitMax USDT/IRT watcher (24/7)',
        'https://api.bitmax.ir/', 'fx', 1, TRUE)
ON CONFLICT (code) DO UPDATE SET priority = 1, enabled = TRUE, updated_at = now();
