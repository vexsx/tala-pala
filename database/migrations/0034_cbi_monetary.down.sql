-- Reverse 0034: unregister the CBI provider and drop the four columns it added.
--
-- The columns go, the rows stay, by the rule 0027's down migration states:
-- economic_observations is append-only and source_document_id is ON DELETE
-- RESTRICT, so reversing a REGISTRATION does not delete measured history.
--
-- Be clear about what is lost. After this, a CBI base-money row no longer says
-- which definition it was compiled under ('old' / 'revised'), a backfilled row
-- no longer says its available_at was reconstructed, and a CBI workbook's
-- archive row no longer names its edition. Nothing reads those columns once the
-- code that wrote them is rolled back too, but a reader that later computes
-- base-money growth from the bare rows would cross the 1399-12/1400 break
-- without knowing it. An operator rolling back for good should delete the CBI
-- data explicitly, in this order (observations cite the document, series own
-- the observations):
--   DELETE FROM economic_observations WHERE series_id IN
--     (SELECT id FROM economic_series WHERE provider_code = 'cbi');
--   DELETE FROM economic_series WHERE provider_code = 'cbi';
--   DELETE FROM instruments WHERE code LIKE 'CBI\_%';
--   DELETE FROM source_documents WHERE provider_code = 'cbi';

ALTER TABLE economic_observations DROP CONSTRAINT IF EXISTS economic_obs_method;
ALTER TABLE economic_observations DROP COLUMN IF EXISTS available_at_approx;
ALTER TABLE economic_observations DROP COLUMN IF EXISTS method;
ALTER TABLE source_documents DROP COLUMN IF EXISTS document_saved_at;
ALTER TABLE source_documents DROP COLUMN IF EXISTS edition_label;

DELETE FROM data_providers WHERE code = 'cbi';
