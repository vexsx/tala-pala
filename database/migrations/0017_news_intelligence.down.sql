-- Reverse of 0017. Drops only what 0017 created; the 0016 tables and their
-- data survive. Added columns are dropped last so dependent indexes go with
-- their tables.
DROP TABLE IF EXISTS news_research_runs;
DROP TABLE IF EXISTS news_feature_snapshots;
DROP TABLE IF EXISTS event_impact_stats;
DROP TABLE IF EXISTS intelligence_deltas;
DROP TABLE IF EXISTS intelligence_snapshot_events;
DROP TABLE IF EXISTS intelligence_snapshots;
DROP TABLE IF EXISTS macro_event_revisions;
DROP TABLE IF EXISTS macro_event_releases;
DROP TABLE IF EXISTS scheduled_macro_events;
DROP TABLE IF EXISTS news_impact_hypotheses;
DROP TABLE IF EXISTS news_event_classifications;
DROP TABLE IF EXISTS news_classifier_versions;
DROP TABLE IF EXISTS news_event_entities;
DROP TABLE IF EXISTS news_event_articles;
DROP TABLE IF EXISTS news_article_entities;
DROP TABLE IF EXISTS news_entities;
DROP TABLE IF EXISTS news_article_duplicates;
DROP TABLE IF EXISTS news_duplicate_groups;
DROP TABLE IF EXISTS news_raw_payloads;
DROP TABLE IF EXISTS news_source_queries;
DROP TABLE IF EXISTS news_source_health_snapshots;
DROP TABLE IF EXISTS news_collection_attempts;
DROP TABLE IF EXISTS news_source_policies;

ALTER TABLE news_events DROP COLUMN IF EXISTS conflicting;
ALTER TABLE news_events DROP COLUMN IF EXISTS consolidation_version;
ALTER TABLE news_events DROP COLUMN IF EXISTS consolidation_method;
ALTER TABLE news_events DROP COLUMN IF EXISTS independent_source_count;
ALTER TABLE news_events DROP COLUMN IF EXISTS duplicate_group_id;
ALTER TABLE news_events DROP COLUMN IF EXISTS available_at;

ALTER TABLE news_articles DROP COLUMN IF EXISTS query_id;
ALTER TABLE news_articles DROP COLUMN IF EXISTS relevance_score;
ALTER TABLE news_articles DROP COLUMN IF EXISTS body_excerpt;
ALTER TABLE news_articles DROP COLUMN IF EXISTS original_language;
ALTER TABLE news_articles DROP COLUMN IF EXISTS source_timezone;
ALTER TABLE news_articles DROP COLUMN IF EXISTS parser_version;
ALTER TABLE news_articles DROP COLUMN IF EXISTS raw_payload_id;
ALTER TABLE news_articles DROP COLUMN IF EXISTS effective_event_at;
ALTER TABLE news_articles DROP COLUMN IF EXISTS available_at;
ALTER TABLE news_articles DROP COLUMN IF EXISTS source_updated_at;
