-- 0004: Additional free data providers (keyless pricedb + gold-api.com, optional keyed BrsApi).
-- Idempotent: safe on databases where rows already exist.

INSERT INTO data_providers (code, name, base_url, category, priority) VALUES
  ('pricedb',  'margani/pricedb (GitHub dataset)', 'https://raw.githubusercontent.com/margani/pricedb', 'iran_gold',   30),
  ('gold_api', 'gold-api.com spot price',          'https://api.gold-api.com',                          'global_gold', 25),
  ('brsapi',   'BrsApi.ir gold & currency',        'https://brsapi.ir',                                 'iran_gold',   25)
ON CONFLICT (code) DO NOTHING;
