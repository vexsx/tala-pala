-- Reverse 0024. Order matters: observations reference series and documents,
-- series references instruments.
DELETE FROM data_providers WHERE code IN ('worldbank','imf_weo');
DROP TABLE IF EXISTS economic_observations;
DROP TABLE IF EXISTS economic_series;
DROP TABLE IF EXISTS source_documents;
DROP TABLE IF EXISTS instruments;
