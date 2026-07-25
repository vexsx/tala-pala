-- News/event subsystem skeleton (leakage-safe storage only).
--
-- The point of these tables is to keep FOUR clocks apart, because a feature
-- built on the wrong one leaks:
--   * event_time    - when the thing happened / takes effect;
--   * published_at  - when the source first made it public;
--   * ingested_at   - when THIS system first saw it.  The only cutoff that is
--                     safe to build features on: we cannot have known a
--                     headline before we held it, whatever its dateline says;
--   * revised_at    - when the text last changed.  Edits are appended as
--                     versions, never overwritten, so a feature computed for
--                     an old timestamp can be recomputed against the text as
--                     it read AT that timestamp.
--
-- There is NO historical news archive: accumulation starts at the first
-- successful ingest.  News features therefore cannot improve forecasts yet,
-- and nothing in the feature/model pipeline reads these tables.
--
-- Source policy (docs/CONTRACTS.md: public, policy-compliant sources only):
--   * federalreserve.gov press RSS is an official public syndication feed of a
--     government body, reachable from the collector host, no auth -> APPROVED,
--     currently the only fetched source;
--   * GDELT's public API rate-limited this host (HTTP 429) -> registered as
--     exploratory and disabled, with NO fetcher implemented;
--   * Iranian outlets tested (isna.ir, cbi.ir) are either unreachable from the
--     datacenter IP or behind a bot challenge whose robots policy cannot be
--     verified -> deliberately absent, not merely disabled.

-- Source registry: policy provenance and request budget per feed.
CREATE TABLE IF NOT EXISTS news_sources (
    id                   SERIAL PRIMARY KEY,
    code                 TEXT NOT NULL UNIQUE,            -- e.g. 'fed_press'
    name                 TEXT NOT NULL,
    feed_url             TEXT NOT NULL DEFAULT '',
    homepage_url         TEXT NOT NULL DEFAULT '',
    kind                 TEXT NOT NULL DEFAULT 'rss',     -- 'rss' | 'api' | 'html'
    jurisdiction         TEXT NOT NULL DEFAULT 'global',  -- 'us' | 'ir' | 'global'
    language             TEXT NOT NULL DEFAULT 'en',
    enabled              BOOLEAN NOT NULL DEFAULT FALSE,
    -- Why the source may (or may not) be fetched at all.  Only 'approved'
    -- rows are ever polled; the others exist so the decision is auditable.
    policy_status        TEXT NOT NULL DEFAULT 'exploratory'
        CHECK (policy_status IN ('approved', 'exploratory', 'excluded')),
    policy_note          TEXT NOT NULL DEFAULT '',
    policy_checked_at    TIMESTAMPTZ,
    quota_per_day        INT,                             -- NULL = unmetered
    min_interval_seconds INT NOT NULL DEFAULT 900,        -- courtesy cadence
    -- Attempt marker, stamped success or failure alike: a broken feed must
    -- not re-poll on every tick (same lesson as the TSE-funds quota guard).
    last_polled_at       TIMESTAMPTZ,
    last_success_at      TIMESTAMPTZ,
    last_error_at        TIMESTAMPTZ,
    last_error           TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per distinct story.  Dedupe is by canonical URL within a source;
-- content_hash detects edits, title_key finds the same story re-issued under
-- a different URL (then duplicate_of points at the original).
CREATE TABLE IF NOT EXISTS news_articles (
    id            BIGSERIAL PRIMARY KEY,
    -- No ON DELETE CASCADE on purpose: removing a source row must not silently
    -- destroy the archive accumulated from it (the archive is unrecoverable —
    -- there is no historical news backfill anywhere).
    source_code   TEXT NOT NULL REFERENCES news_sources (code),
    external_id   TEXT NOT NULL DEFAULT '',        -- feed <guid>
    canonical_url TEXT NOT NULL,                   -- normalized; the dedupe key
    url           TEXT NOT NULL DEFAULT '',        -- exactly as published
    title         TEXT NOT NULL,                   -- latest observed version
    title_key     TEXT NOT NULL DEFAULT '',        -- normalized title (near-dup lookup)
    summary       TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT 'en',
    content_hash  TEXT NOT NULL,                   -- sha256 of normalized title+summary
    -- FIRST publication time as stated by the source; never overwritten by a
    -- later revision (each revision's stated time lives on its version row).
    published_at  TIMESTAMPTZ NOT NULL,
    -- TRUE when the source gave no parseable timestamp and ingestion time was
    -- substituted.  Conservative on purpose: an over-late publication time can
    -- only make a future feature ignore the item, never use it too early.
    published_at_estimated BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revised_at    TIMESTAMPTZ,                     -- NULL until an edit is observed
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_versions    INT NOT NULL DEFAULT 1,
    duplicate_of  BIGINT REFERENCES news_articles (id) ON DELETE SET NULL,
    raw_payload   JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT news_articles_unique UNIQUE (source_code, canonical_url)
);

-- Append-only history of an article's text.  Version 1 is written at first
-- ingest, so the original wording survives every later edit.
CREATE TABLE IF NOT EXISTS news_article_versions (
    id           BIGSERIAL PRIMARY KEY,
    article_id   BIGINT NOT NULL REFERENCES news_articles (id) ON DELETE CASCADE,
    version      INT NOT NULL,                     -- 1-based, monotonic
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,             -- as stated for THIS version
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when we observed it
    raw_payload  JSONB,
    CONSTRAINT news_article_versions_unique UNIQUE (article_id, version)
);

-- Classified events.  Nothing writes this table yet: classification is future
-- work and no event study exists, so the taxonomy priors in
-- app/news/taxonomy.py are hypotheses only.
CREATE TABLE IF NOT EXISTS news_events (
    id                    BIGSERIAL PRIMARY KEY,
    article_id            BIGINT REFERENCES news_articles (id) ON DELETE SET NULL,
    source_code           TEXT NOT NULL DEFAULT '',
    category              TEXT NOT NULL,           -- app/news/taxonomy.py code
    -- 'aligned' = the event carries the polarity the taxonomy states its
    -- directional priors for; 'opposed' = the mirror case (flip every prior).
    polarity              TEXT NOT NULL DEFAULT 'unknown'
        CHECK (polarity IN ('aligned', 'opposed', 'neutral', 'unknown')),
    event_time            TIMESTAMPTZ NOT NULL,    -- when it happened/takes effect
    event_time_precision  TEXT NOT NULL DEFAULT 'unknown'
        CHECK (event_time_precision IN ('exact', 'hour', 'day', 'unknown')),
    published_at          TIMESTAMPTZ NOT NULL,    -- first public disclosure
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    revised_at            TIMESTAMPTZ,
    severity              TEXT NOT NULL DEFAULT 'unknown'
        CHECK (severity IN ('low', 'medium', 'high', 'unknown')),
    surprise              NUMERIC,                 -- actual vs expected, when measurable
    classifier            TEXT NOT NULL DEFAULT '',  -- what assigned the category
    classifier_confidence DOUBLE PRECISION,
    details               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT news_events_unique UNIQUE (article_id, category)
);

-- Indexes follow the queries this subsystem will actually run: "what was
-- published in window W" (display), "what did we KNOW at time T" (the
-- leakage-safe feature cutoff), per-source health, and hash/title dedupe
-- lookups on every ingested item.
CREATE INDEX IF NOT EXISTS idx_news_articles_published
    ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_source_published
    ON news_articles (source_code, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_ingested
    ON news_articles (ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_content_hash
    ON news_articles (content_hash);
CREATE INDEX IF NOT EXISTS idx_news_articles_title_key
    ON news_articles (source_code, title_key);
CREATE INDEX IF NOT EXISTS idx_news_article_versions_article
    ON news_article_versions (article_id, ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_events_category_ingested
    ON news_events (category, ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_events_event_time
    ON news_events (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_news_events_published
    ON news_events (published_at DESC);

INSERT INTO news_sources (code, name, feed_url, homepage_url, kind, jurisdiction,
                          language, enabled, policy_status, policy_note,
                          quota_per_day, min_interval_seconds)
VALUES
  ('fed_press', 'Federal Reserve Board press releases',
   'https://www.federalreserve.gov/feeds/press_all.xml',
   'https://www.federalreserve.gov/newsevents/pressreleases.htm',
   'rss', 'us', 'en', TRUE, 'approved',
   'Official public syndication feed of a government body: no account, no key, no CAPTCHA, not robots-disallowed. Fetched with the project User-Agent at the courtesy cadence below.',
   NULL, 900),
  ('gdelt', 'GDELT 2.0 document API',
   'https://api.gdeltproject.org/api/v2/doc/doc',
   'https://www.gdeltproject.org/', 'api', 'global', 'en', FALSE, 'exploratory',
   'Public API, but it returned HTTP 429 (rate limited) from this host. Registered for the record only: no fetcher is implemented and the source stays disabled.',
   NULL, 3600)
ON CONFLICT (code) DO NOTHING;
