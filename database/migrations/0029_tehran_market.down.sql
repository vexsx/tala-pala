-- Reverse of 0029. Children first, so nothing leans on the foreign keys for
-- ordering. equity_client_flows references 0028's registry and is dropped here
-- with the rest of this migration; 0028's own tables are untouched.
DROP TABLE IF EXISTS sector_breadth_snapshots;
DROP TABLE IF EXISTS market_snapshots;
DROP TABLE IF EXISTS equity_client_flows;
DROP TABLE IF EXISTS market_values;
DROP TABLE IF EXISTS market_index_checks;
DROP TABLE IF EXISTS market_index_values;
DROP TABLE IF EXISTS market_indices;
