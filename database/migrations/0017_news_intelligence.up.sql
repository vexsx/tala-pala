-- Addendum 18: production news + intelligence schema.
--
-- Extends 0016 (news_sources / news_articles / news_article_versions /
-- news_events) rather than rewriting it: 0016 is applied in production.
--
-- Design rules this schema enforces, each learned from a specific hazard:
--
--   * TIMESTAMP HONESTY. Fetch time is never allowed to masquerade as
--     publication time. `source_published_at` may be NULL, and when it is,
--     `published_at_is_estimated` records that fact explicitly. Historical
--     feature builders may ONLY filter on `available_at` (when the
--     information could first have been acted upon), never on
--     `source_published_at`, so a late-arriving article cannot leak into an
--     earlier fold.
--
--   * EVIDENCE BEFORE INTERPRETATION. Raw payloads are stored first and every
--     normalized row points back at the payload it came from, so any
--     classification can be re-derived and audited.
--
--   * SYNDICATION IS NOT CONFIRMATION. One wire story republished by twenty
--     sites is one event with one independent source, not twenty. Duplicate
--     groups carry `independent_source_count` separately from `article_count`.
--
--   * HYPOTHESES ARE LABELLED. Every impact score is stored with its rule id,
--     classifier version, evidence and a `hypothesis_only` flag. Nothing here
--     asserts causality.

-- ---------------------------------------------------------------- sources --

-- Policy provenance, kept separate from the operational row so an approval
-- decision has its own auditable history.
CREATE TABLE IF NOT EXISTS news_source_policies (
    id                    SERIAL PRIMARY KEY,
    source_code           TEXT NOT NULL REFERENCES news_sources(code) ON DELETE CASCADE,
    access_method         TEXT NOT NULL DEFAULT 'rss',      -- rss|api|html
    auth_type             TEXT NOT NULL DEFAULT 'none',     -- none|api_key|oauth
    approval_state        TEXT NOT NULL DEFAULT 'policy_review_required'
        CHECK (approval_state IN ('approved', 'experimental', 'disabled',
                                  'rejected', 'credential_required',
                                  'policy_review_required')),
    user_agent_policy     TEXT NOT NULL DEFAULT 'honest',   -- honest|source_required
    backfill_allowed      BOOLEAN NOT NULL DEFAULT FALSE,
    store_full_body       BOOLEAN NOT NULL DEFAULT FALSE,   -- else metadata+excerpt
    attribution_required  BOOLEAN NOT NULL DEFAULT TRUE,
    rate_limit_per_day    INT,
    min_interval_seconds  INT NOT NULL DEFAULT 900,
    policy_note           TEXT NOT NULL DEFAULT '',
    reviewed_at           TIMESTAMPTZ,
    reviewed_by           TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_code)
);

-- Every fetch attempt, success or failure. This is what makes a quota claim
-- checkable and a circuit breaker honest.
CREATE TABLE IF NOT EXISTS news_collection_attempts (
    id                BIGSERIAL PRIMARY KEY,
    source_code       TEXT NOT NULL REFERENCES news_sources(code) ON DELETE CASCADE,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    outcome           TEXT NOT NULL DEFAULT 'error'
        CHECK (outcome IN ('ok', 'error', 'throttled', 'skipped', 'empty')),
    http_status       INT,
    bytes_received    BIGINT,
    items_seen        INT NOT NULL DEFAULT 0,
    items_new         INT NOT NULL DEFAULT 0,
    items_updated     INT NOT NULL DEFAULT 0,
    items_duplicate   INT NOT NULL DEFAULT 0,
    error_class       TEXT,
    error_detail      TEXT,
    query_id          INT,
    parser_version    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_news_attempts_source_started
    ON news_collection_attempts (source_code, started_at DESC);

CREATE TABLE IF NOT EXISTS news_source_health_snapshots (
    id                     BIGSERIAL PRIMARY KEY,
    source_code            TEXT NOT NULL REFERENCES news_sources(code) ON DELETE CASCADE,
    captured_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    health                 TEXT NOT NULL DEFAULT 'unknown'
        CHECK (health IN ('healthy', 'degraded', 'down', 'disabled', 'unknown')),
    last_success_at        TIMESTAMPTZ,
    last_publication_at    TIMESTAMPTZ,
    publication_latency_s  DOUBLE PRECISION,
    consecutive_failures   INT NOT NULL DEFAULT 0,
    duplicate_ratio        DOUBLE PRECISION,
    circuit_open           BOOLEAN NOT NULL DEFAULT FALSE,
    note                   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_news_health_source_time
    ON news_source_health_snapshots (source_code, captured_at DESC);

-- Configurable queries (GDELT etc). The exact query that produced a result is
-- persisted so a result set is reproducible.
CREATE TABLE IF NOT EXISTS news_source_queries (
    id             SERIAL PRIMARY KEY,
    source_code    TEXT NOT NULL REFERENCES news_sources(code) ON DELETE CASCADE,
    code           TEXT NOT NULL,
    query_text     TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    max_records    INT NOT NULL DEFAULT 75,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_code, code)
);

-- ------------------------------------------------------------ raw content --

CREATE TABLE IF NOT EXISTS news_raw_payloads (
    id             BIGSERIAL PRIMARY KEY,
    source_code    TEXT NOT NULL REFERENCES news_sources(code) ON DELETE CASCADE,
    query_id       INT REFERENCES news_source_queries(id) ON DELETE SET NULL,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_url    TEXT NOT NULL,
    http_status    INT,
    content_type   TEXT NOT NULL DEFAULT '',
    body_sha256    TEXT NOT NULL,
    body_bytes     BIGINT NOT NULL DEFAULT 0,
    body           TEXT,                      -- truncated to a configured cap
    truncated      BOOLEAN NOT NULL DEFAULT FALSE,
    parser_version TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_news_raw_source_fetched
    ON news_raw_payloads (source_code, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_raw_sha ON news_raw_payloads (body_sha256);

-- --------------------------------------------------------- article extras --
-- 0016's news_articles covers the core columns; these add the point-in-time
-- and provenance semantics the feature builder depends on.

ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ;
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS effective_event_at TIMESTAMPTZ;
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS raw_payload_id BIGINT
    REFERENCES news_raw_payloads(id) ON DELETE SET NULL;
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS parser_version TEXT NOT NULL DEFAULT '';
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS source_timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS original_language TEXT NOT NULL DEFAULT '';
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS body_excerpt TEXT NOT NULL DEFAULT '';
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS relevance_score DOUBLE PRECISION;
ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS query_id INT
    REFERENCES news_source_queries(id) ON DELETE SET NULL;

-- available_at is the ONLY column historical features may filter on. Backfill
-- it for existing rows: the moment we could first have acted on the item is
-- when we ingested it, or its publication time if that is later known and
-- earlier than ingestion is impossible.
UPDATE news_articles
   SET available_at = COALESCE(available_at, ingested_at)
 WHERE available_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_news_articles_available
    ON news_articles (available_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_effective
    ON news_articles (effective_event_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_language
    ON news_articles (language);

-- ------------------------------------------------------------- duplicates --

CREATE TABLE IF NOT EXISTS news_duplicate_groups (
    id                       BIGSERIAL PRIMARY KEY,
    primary_article_id       BIGINT REFERENCES news_articles(id) ON DELETE SET NULL,
    method                   TEXT NOT NULL DEFAULT '',   -- which rule matched
    method_version           TEXT NOT NULL DEFAULT '',
    article_count            INT NOT NULL DEFAULT 1,
    -- Distinct SOURCES, not articles: syndication must not inflate this.
    independent_source_count INT NOT NULL DEFAULT 1,
    syndication_count        INT NOT NULL DEFAULT 0,
    source_diversity         DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_published_at       TIMESTAMPTZ,
    first_seen_at            TIMESTAMPTZ,
    last_updated_at          TIMESTAMPTZ,
    conflicting              BOOLEAN NOT NULL DEFAULT FALSE,
    confidence               DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news_article_duplicates (
    group_id         BIGINT NOT NULL REFERENCES news_duplicate_groups(id) ON DELETE CASCADE,
    article_id       BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    similarity       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    match_reason     TEXT NOT NULL DEFAULT '',
    method_version   TEXT NOT NULL DEFAULT '',
    is_primary       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (group_id, article_id)
);

CREATE INDEX IF NOT EXISTS idx_news_article_dupes_article
    ON news_article_duplicates (article_id);

-- ---------------------------------------------------------------- entities --

CREATE TABLE IF NOT EXISTS news_entities (
    id            SERIAL PRIMARY KEY,
    kind          TEXT NOT NULL,              -- country|central_bank|person|...
    code          TEXT NOT NULL,              -- stable slug, e.g. 'iran'
    display_name  TEXT NOT NULL,
    display_fa    TEXT NOT NULL DEFAULT '',
    aliases       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Only set when the SOURCE supplies a real, validated location. Never
    -- inferred, never jittered: a fabricated coordinate is worse than none.
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    location_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, code)
);

CREATE TABLE IF NOT EXISTS news_article_entities (
    article_id   BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    entity_id    INT NOT NULL REFERENCES news_entities(id) ON DELETE CASCADE,
    matched_term TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (article_id, entity_id)
);

-- ----------------------------------------------------------------- events --

ALTER TABLE news_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ;
ALTER TABLE news_events ADD COLUMN IF NOT EXISTS duplicate_group_id BIGINT
    REFERENCES news_duplicate_groups(id) ON DELETE SET NULL;
ALTER TABLE news_events ADD COLUMN IF NOT EXISTS independent_source_count INT NOT NULL DEFAULT 1;
ALTER TABLE news_events ADD COLUMN IF NOT EXISTS consolidation_method TEXT NOT NULL DEFAULT '';
ALTER TABLE news_events ADD COLUMN IF NOT EXISTS consolidation_version TEXT NOT NULL DEFAULT '';
ALTER TABLE news_events ADD COLUMN IF NOT EXISTS conflicting BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE news_events SET available_at = COALESCE(available_at, ingested_at)
 WHERE available_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_news_events_available
    ON news_events (available_at DESC);

CREATE TABLE IF NOT EXISTS news_event_articles (
    event_id    BIGINT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    article_id  BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'supporting'
        CHECK (role IN ('primary', 'supporting', 'conflicting')),
    PRIMARY KEY (event_id, article_id)
);

CREATE TABLE IF NOT EXISTS news_event_entities (
    event_id   BIGINT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    entity_id  INT NOT NULL REFERENCES news_entities(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, entity_id)
);

CREATE TABLE IF NOT EXISTS news_classifier_versions (
    id            SERIAL PRIMARY KEY,
    version       TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL DEFAULT 'deterministic',
    description   TEXT NOT NULL DEFAULT '',
    rule_count    INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news_event_classifications (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            BIGINT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    classifier_version  TEXT NOT NULL,
    category            TEXT NOT NULL,
    confidence          DOUBLE PRECISION NOT NULL DEFAULT 0,
    rule_id             TEXT NOT NULL DEFAULT '',
    supporting_terms    JSONB NOT NULL DEFAULT '[]'::jsonb,
    contradicting_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    classified_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, classifier_version, category)
);

-- Separate impact channels. A single "sentiment" number cannot express that a
-- hawkish Fed surprise is bearish for XAU while an Iran shock is bullish for
-- USD/IRT and the local premium at the same moment.
CREATE TABLE IF NOT EXISTS news_impact_hypotheses (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            BIGINT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    classifier_version  TEXT NOT NULL,
    channel             TEXT NOT NULL
        CHECK (channel IN ('xau_usd', 'usd_irt', 'local_premium',
                           'liquidity_spread', 'gold_funds', 'combined_ir_gold')),
    score               DOUBLE PRECISION NOT NULL,     -- bounded [-1, 1]
    confidence          DOUBLE PRECISION NOT NULL DEFAULT 0,
    rule_id             TEXT NOT NULL DEFAULT '',
    rule_version        TEXT NOT NULL DEFAULT '',
    supporting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    contradicting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    sample_support      INT,
    expected_horizon    TEXT NOT NULL DEFAULT '',
    decay_hours         DOUBLE PRECISION,
    -- Always true in P1: these are rule-derived hypotheses, never measured
    -- causal effects. The UI must render them as such.
    hypothesis_only     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, classifier_version, channel)
);

CREATE INDEX IF NOT EXISTS idx_news_impact_event ON news_impact_hypotheses (event_id);

-- ----------------------------------------------------------- macro events --

CREATE TABLE IF NOT EXISTS scheduled_macro_events (
    id                SERIAL PRIMARY KEY,
    code              TEXT NOT NULL,            -- 'us_cpi', 'fomc_decision'
    title             TEXT NOT NULL,
    region            TEXT NOT NULL DEFAULT 'us',
    importance        TEXT NOT NULL DEFAULT 'medium'
        CHECK (importance IN ('low', 'medium', 'high')),
    scheduled_at      TIMESTAMPTZ NOT NULL,
    scheduled_precision TEXT NOT NULL DEFAULT 'exact'
        CHECK (scheduled_precision IN ('exact', 'day', 'week', 'unknown')),
    source_code       TEXT REFERENCES news_sources(code) ON DELETE SET NULL,
    source_url        TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (code, scheduled_at)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_macro_time
    ON scheduled_macro_events (scheduled_at);

-- First print. Revisions go to macro_event_revisions so a historical fold can
-- only ever see the number that existed at its cutoff.
CREATE TABLE IF NOT EXISTS macro_event_releases (
    id                  BIGSERIAL PRIMARY KEY,
    scheduled_event_id  INT NOT NULL REFERENCES scheduled_macro_events(id) ON DELETE CASCADE,
    released_at         TIMESTAMPTZ NOT NULL,
    available_at        TIMESTAMPTZ NOT NULL,
    previous_value      DOUBLE PRECISION,
    consensus_value     DOUBLE PRECISION,
    first_value         DOUBLE PRECISION,
    unit                TEXT NOT NULL DEFAULT '',
    surprise            DOUBLE PRECISION,
    source_code         TEXT REFERENCES news_sources(code) ON DELETE SET NULL,
    raw_payload_id      BIGINT REFERENCES news_raw_payloads(id) ON DELETE SET NULL,
    UNIQUE (scheduled_event_id, released_at)
);

CREATE TABLE IF NOT EXISTS macro_event_revisions (
    id            BIGSERIAL PRIMARY KEY,
    release_id    BIGINT NOT NULL REFERENCES macro_event_releases(id) ON DELETE CASCADE,
    revised_value DOUBLE PRECISION NOT NULL,
    revised_at    TIMESTAMPTZ NOT NULL,
    available_at  TIMESTAMPTZ NOT NULL,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_macro_revisions_release
    ON macro_event_revisions (release_id, available_at);

-- ----------------------------------------------------------- intelligence --

CREATE TABLE IF NOT EXISTS intelligence_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    calc_version      TEXT NOT NULL,
    -- Bounded pressure scores in [-1, 1]; NULL means "not enough evidence",
    -- which the UI must render as unknown rather than as neutral.
    scores            JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
    inputs            JSONB NOT NULL DEFAULT '{}'::jsonb,
    supporting_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicting_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_reliability DOUBLE PRECISION,
    data_freshness_s  DOUBLE PRECISION,
    stale             BOOLEAN NOT NULL DEFAULT FALSE,
    limitations       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_intel_snapshots_time
    ON intelligence_snapshots (captured_at DESC);

CREATE TABLE IF NOT EXISTS intelligence_snapshot_events (
    snapshot_id  BIGINT NOT NULL REFERENCES intelligence_snapshots(id) ON DELETE CASCADE,
    event_id     BIGINT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    role         TEXT NOT NULL DEFAULT 'supporting',
    weight       DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, event_id)
);

CREATE TABLE IF NOT EXISTS intelligence_deltas (
    id            BIGSERIAL PRIMARY KEY,
    from_snapshot BIGINT REFERENCES intelligence_snapshots(id) ON DELETE SET NULL,
    to_snapshot   BIGINT NOT NULL REFERENCES intelligence_snapshots(id) ON DELETE CASCADE,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT NOT NULL,   -- new_event|escalation|source_failure|...
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    magnitude     DOUBLE PRECISION,
    event_id      BIGINT REFERENCES news_events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_deltas_to
    ON intelligence_deltas (to_snapshot, computed_at DESC);

-- -------------------------------------------------------------- research --

CREATE TABLE IF NOT EXISTS event_impact_stats (
    id                 BIGSERIAL PRIMARY KEY,
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    category           TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    window_label       TEXT NOT NULL,   -- 15m|1h|4h|eod|1d|3d|7d|30d
    n_events           INT NOT NULL,
    n_independent      INT NOT NULL DEFAULT 0,
    mean_move_pct      DOUBLE PRECISION,
    median_move_pct    DOUBLE PRECISION,
    hit_rate           DOUBLE PRECISION,
    vol_change_pct     DOUBLE PRECISION,
    spread_change_pct  DOUBLE PRECISION,
    premium_change_pct DOUBLE PRECISION,
    ci_low             DOUBLE PRECISION,
    ci_high            DOUBLE PRECISION,
    regime             TEXT NOT NULL DEFAULT 'all',
    -- Below this the row is reported as unsupported rather than as a finding.
    sufficient_support BOOLEAN NOT NULL DEFAULT FALSE,
    method_version     TEXT NOT NULL DEFAULT '',
    UNIQUE (category, symbol, window_label, regime, method_version)
);

CREATE TABLE IF NOT EXISTS news_feature_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    as_of         TIMESTAMPTZ NOT NULL,
    builder_version TEXT NOT NULL,
    features      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, as_of, builder_version)
);

CREATE INDEX IF NOT EXISTS idx_news_feature_snapshots_asof
    ON news_feature_snapshots (symbol, as_of DESC);

CREATE TABLE IF NOT EXISTS news_research_runs (
    id             BIGSERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    kind           TEXT NOT NULL,     -- ablation|event_study|shadow_prediction
    status         TEXT NOT NULL DEFAULT 'running',
    feature_sets   JSONB NOT NULL DEFAULT '[]'::jsonb,
    results        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Research output NEVER activates a model. This column records the review
    -- decision only.
    decision       TEXT NOT NULL DEFAULT 'shadow'
        CHECK (decision IN ('shadow', 'rejected', 'insufficient_data',
                            'eligible_for_review')),
    notes          TEXT NOT NULL DEFAULT ''
);
