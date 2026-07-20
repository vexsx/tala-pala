-- 0007: HTML-parsing fallback providers (public server-rendered pages, no keys).
-- alanchand already exists (0001) — its adapter now has a keyless HTML mode.
-- milligold is new: milli.gold renders the 18k gram price server-side in rial.

INSERT INTO data_providers (code, name, base_url, category, priority) VALUES
  ('milligold', 'Milli Gold (milli.gold)', 'https://milli.gold', 'iran_gold', 35)
ON CONFLICT (code) DO NOTHING;

UPDATE data_providers
SET last_error = NULL,
    consecutive_failures = 0,
    updated_at = now()
WHERE code = 'alanchand';
