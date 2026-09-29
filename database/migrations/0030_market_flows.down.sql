-- Reverse of 0030. Children first, so nothing leans on the foreign keys for
-- ordering.
--
-- The fund closes go with the funds' registry: `prices` source 'tsetmc_cdn'
-- and their raw_observations audit rows are written by nothing but 0030's
-- fund ingest, and left behind they would be two silver symbols no registry
-- row names plus gold-fund history no 0029-era job could maintain.
DELETE FROM raw_observations
WHERE provider_code = 'tsetmc_cdn' AND dedupe_key LIKE 'tsetmc_cdn|%|close|%';
DELETE FROM prices WHERE source = 'tsetmc_cdn';

DROP TABLE IF EXISTS commodity_funds;
DELETE FROM instruments WHERE code IN ('IR_SILVER_FUND_SILVER','IR_SILVER_FUND_SIMIN');
UPDATE instruments SET notes = '' WHERE code IN ('IR_GOLD_FUND_AYAR','IR_GOLD_FUND_TALA');
UPDATE instruments SET notes = 'Configured in TSETMC_FUNDS; no observations collected yet.'
WHERE code = 'IR_GOLD_FUND_KAHRABA';

DROP TABLE IF EXISTS market_session_files;
DROP TABLE IF EXISTS market_share_sessions;

-- 0029's key referenced the roster and cascaded. Flow rows of shares outside
-- the roster cannot satisfy it, so they go first — they are exactly the rows
-- 0029 had no way to store.
DROP INDEX IF EXISTS idx_equity_client_flows_date;
ALTER TABLE equity_client_flows DROP CONSTRAINT equity_client_flows_ins_code_fkey;
DELETE FROM equity_client_flows
WHERE ins_code NOT IN (SELECT ins_code FROM equity_instruments);
ALTER TABLE equity_client_flows ADD CONSTRAINT equity_client_flows_ins_code_fkey
    FOREIGN KEY (ins_code) REFERENCES equity_instruments(ins_code) ON DELETE CASCADE;

DROP TABLE IF EXISTS market_shares;
DROP TABLE IF EXISTS market_sectors;
