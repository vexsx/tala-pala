-- 0024: The instrument vocabulary, and a bitemporal store for revisable
-- economic data.
--
-- WHY THIS EXISTS
--
-- 1. `instruments` replaces seven in-code symbol registries. Today the symbol
--    vocabulary lives in backend-go/internal/prices/handlers.go (KnownSymbols),
--    core/validation.py (SANITY_RANGES), core/market_hours.py (three
--    frozensets), jobs/collect.py (JOB_SYMBOLS), models/training.py
--    (FORECAST_SYMBOLS), jobs/features.py (FEATURE_SYMBOLS) and
--    frontend/src/chart/prefs.ts (CHART_SYMBOLS) — all of which must be edited
--    in lockstep. A symbol that reaches `prices` without a matching Go map
--    entry is invisible to the API until Go is recompiled. This table is the
--    single vocabulary those readers project.
--
--    No foreign key is added from prices.symbol yet, deliberately. Every
--    existing symbol is seeded below, so a FK would pass today — but it would
--    also turn "a provider emitted an unregistered symbol" from a silent new
--    series into a hard collect failure in production. The FK lands once every
--    writer goes through the registry; until then the registry is advisory and
--    read-only consumers treat an unknown symbol as unserveable.
--
-- 2. `economic_series` + `economic_observations` store revisable, low-frequency
--    economic data, which `prices` structurally cannot hold. `prices` is keyed
--    (symbol, observed_at, source): one value, one instant, never revised. A
--    macro observation has THREE times that all differ —
--      * the reference period it describes  (Mordad 1405)
--      * the moment it was published        (~5.5 months later, for CBI)
--      * the revision it belongs to         (vintage)
--    Storing that in `prices` would require lying about at least one of them.
--
--    Measured, 2026-09-08, and the reason this is not over-engineering: CBI's
--    monetary bulletin for Esfand 1404 (period ending 2026-03-20) was the
--    latest available — a 5.5-month publication lag. Codal republished a
--    monthly report 485 days after its period end. A backtest that reads the
--    latest revised number at a historical cutoff is reading the future.
--
--    This generalizes the pattern migration 0017 already established for
--    scheduled macro events (macro_event_releases + macro_event_revisions,
--    "so a historical fold can only ever see the number that existed at its
--    cutoff"). Those tables stay as they are — they are keyed to a scheduled
--    event, not to an arbitrary series.
--
-- 3. `source_documents` archives the artifact a value was read from. This is
--    not optional bookkeeping for this domain: the Statistical Centre of Iran
--    publishes the monthly CPI as PDFs whose filenames are non-deterministic
--    and whose older paths 404 (verified 2026-09-08). A value whose source
--    document cannot be retrieved later is not point-in-time defensible, and
--    back-filling the document after the fact is impossible.

-- ---------------------------------------------------------------- instruments
CREATE TABLE instruments (
    code            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN
                        ('market_price','economic_series','equity','index','basket','fx')),
    name_en         TEXT NOT NULL,
    name_fa         TEXT NOT NULL DEFAULT '',
    domain          TEXT NOT NULL,          -- gold | fx | macro | housing | auto | equity | global | fund
    quote_currency  TEXT NOT NULL,          -- IRT | USD | INDEX | PCT | RATIO
    unit            TEXT NOT NULL,          -- gram | ozt | usd | coin | bbl | index | pct | unit | sqm
    decimals        INT  NOT NULL DEFAULT 0,
    -- Which market-hours class governs freshness. Mirrors the classes in
    -- core/market_hours.py and internal/markethours; 'none' means the concept
    -- does not apply (an economic series is not open or closed).
    calendar_class  TEXT NOT NULL DEFAULT 'always_open' CHECK (calendar_class IN
                        ('always_open','tehran_bazaar','tse_session','global','none')),
    -- How much this number deserves to be trusted, and whether it is a
    -- measurement at all. Rendered by the UI; never merely stored.
    quality_tier    TEXT NOT NULL DEFAULT 'official' CHECK (quality_tier IN
                        ('official','official_mirror','commercial','proxy','estimate','experimental')),
    is_proxy        BOOLEAN NOT NULL DEFAULT FALSE,
    is_derived      BOOLEAN NOT NULL DEFAULT FALSE,  -- computed by us, not observed
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_instruments_kind_domain ON instruments (kind, domain) WHERE enabled;

-- Seed: exactly the symbols that exist in production today, with the currency
-- and unit those rows actually carry (verified against
-- SELECT symbol, currency, unit FROM prices GROUP BY 1,2,3 on 2026-09-08),
-- plus IR_GOLD_FUND_KAHRABA which is configured in TSETMC_FUNDS but has never
-- collected a row.
--
-- Note IR_GOLD_FUND_FLOW: it is PCT/pct, not a price. It is the retail/
-- institutional flow ratio, and calling it a fund price would be wrong.
INSERT INTO instruments
  (code, kind, name_en, name_fa, domain, quote_currency, unit, decimals, calendar_class, quality_tier, is_proxy, notes)
VALUES
  ('IR_GOLD_18K','market_price','Iranian 18k gold, per gram','طلای ۱۸ عیار','gold','IRT','gram',0,
     'always_open','official_mirror',FALSE,'Primary source Hamrah Gold; quotes 24/7 every day.'),
  ('XAUUSD','market_price','Gold, COMEX front month (spot proxy)','انس طلا','global','USD','ozt',2,
     'global','proxy',TRUE,
     'Collected from Yahoo ticker GC=F — COMEX gold futures, used as the XAUUSD proxy (app/providers/yahoo.py). A front-month future carries contango/roll and its own settlement calendar; it is NOT the London spot fix. LBMA''s official price is licensed and not freely available.'),
  ('XAGUSD','market_price','Silver, COMEX front month (spot proxy)','انس نقره','global','USD','ozt',3,
     'global','proxy',TRUE,
     'Collected from Yahoo ticker SI=F — COMEX silver futures used as the XAGUSD proxy. Same futures caveat as XAUUSD.'),
  ('USD_IRT','fx','US dollar, free market','دلار آزاد','fx','IRT','usd',0,
     'always_open','proxy',TRUE,
     'Collected from the 24/7 USDT/toman market (BitMax) as the documented free-market proxy. Its small premium over cash dollars is genuine market information, not error.'),
  ('IR_COIN_EMAMI','market_price','Emami gold coin','سکه امامی','gold','IRT','coin',0,
     'tehran_bazaar','official_mirror',FALSE,''),
  ('BRENT_OIL','market_price','Brent crude, front month','نفت برنت','global','USD','bbl',2,
     'global','proxy',TRUE,
     'Collected from Yahoo ticker BZ=F — the Brent front-month future, not a dated-Brent assessment.'),
  ('DXY','index','US dollar index','شاخص دلار','global','INDEX','index',3,
     'global','official_mirror',FALSE,''),
  ('US10Y','market_price','US 10-year Treasury yield','بازده ۱۰ ساله آمریکا','global','PCT','pct',3,
     'global','official_mirror',FALSE,'Percent, not basis points. Yahoo ^TNX is 10x-scaled; see migration 0022.'),
  ('IR_GOLD_FUND_AYAR','market_price','Ayar gold ETF','صندوق عیار','fund','IRT','unit',0,
     'tse_session','official_mirror',FALSE,''),
  ('IR_GOLD_FUND_TALA','market_price','Tala gold ETF','صندوق طلا','fund','IRT','unit',0,
     'tse_session','official_mirror',FALSE,''),
  ('IR_GOLD_FUND_KAHRABA','market_price','Kahraba gold ETF','صندوق کهربا','fund','IRT','unit',0,
     'tse_session','official_mirror',FALSE,'Configured in TSETMC_FUNDS; no observations collected yet.'),
  -- kind='index', deliberately NOT 'market_price'. The whole point of this
  -- table is that a client filters on the machine-readable field instead of
  -- hard-coding a symbol list, so a free-text warning in `notes` while `kind`
  -- says "price" would reproduce the very defect the registry exists to remove.
  ('IR_GOLD_FUND_FLOW','index','Gold-fund retail/institutional flow ratio','نسبت جریان حقیقی/حقوقی','fund','PCT','pct',2,
     'tse_session','official_mirror',FALSE,'A flow RATIO in percent, not a price. Excluded from kind=market_price for exactly that reason.')
ON CONFLICT (code) DO NOTHING;

-- ----------------------------------------------------------- source_documents
CREATE TABLE source_documents (
    id             BIGSERIAL PRIMARY KEY,
    provider_code  TEXT NOT NULL,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    media_type     TEXT NOT NULL DEFAULT '',
    byte_size      INT  NOT NULL DEFAULT 0,
    content_sha256 TEXT NOT NULL,
    storage_path   TEXT NOT NULL DEFAULT '',   -- '' while only the hash is kept
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Same content re-fetched from the same provider is the same document.
    CONSTRAINT source_documents_unique UNIQUE (provider_code, content_sha256)
);
CREATE INDEX idx_source_documents_fetched ON source_documents (fetched_at DESC);

-- ------------------------------------------------------------ economic_series
CREATE TABLE economic_series (
    id                   BIGSERIAL PRIMARY KEY,
    code                 TEXT NOT NULL UNIQUE REFERENCES instruments(code) ON DELETE RESTRICT,
    frequency            TEXT NOT NULL CHECK (frequency IN ('D','W','M','Q','A')),
    -- Reference periods for Iranian official data are Jalali months. The
    -- stored dates are always Gregorian UTC; this records which calendar the
    -- SOURCE used to define the period, so a label like '1405-06' can be
    -- rendered without re-deriving it.
    calendar             TEXT NOT NULL DEFAULT 'gregorian' CHECK (calendar IN ('gregorian','jalali')),
    -- Part of series identity, not a display option. Iran publishes both
    -- point-to-point and twelve-month-average inflation and they diverge by
    -- 20+ points while inflation accelerates (87.9% vs 66.0%, Tir 1405), so
    -- they are two different series.
    measure              TEXT NOT NULL CHECK (measure IN
                             ('index','level','yoy_pct','mom_pct','ratio','rate')),
    seasonal_adjustment  TEXT NOT NULL DEFAULT 'nsa' CHECK (seasonal_adjustment IN ('nsa','sa','unknown')),
    base_period          TEXT NOT NULL DEFAULT '',   -- e.g. '1400=100'
    provider_code        TEXT NOT NULL,
    provider_series_id   TEXT NOT NULL DEFAULT '',   -- the source's own key
    publication_lag_days INT,                        -- NULL = not characterised
    revisable            BOOLEAN NOT NULL DEFAULT TRUE,
    -- Index LEVELS are not spliceable across a rebase: SCI restated the whole
    -- level history at the 1400 rebase and publishes no concordance. Chaining
    -- growth rates is the only correct treatment.
    splice_policy        TEXT NOT NULL DEFAULT 'none' CHECK (splice_policy IN ('none','chain_growth')),
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_economic_series_provider ON economic_series (provider_code) WHERE enabled;

-- ------------------------------------------------------ economic_observations
CREATE TABLE economic_observations (
    id                 BIGSERIAL PRIMARY KEY,
    series_id          BIGINT NOT NULL REFERENCES economic_series(id) ON DELETE CASCADE,
    ref_period_start   DATE NOT NULL,
    ref_period_end     DATE NOT NULL,
    ref_period_label   TEXT NOT NULL DEFAULT '',   -- '1405-06', '2025', '2025-Q2'
    value              NUMERIC NOT NULL,
    -- When the source published it. NULL when the source does not say, which
    -- is common; available_at then carries the whole point-in-time claim.
    published_at       TIMESTAMPTZ,
    -- When THIS system could first have known the value. Never NULL: every
    -- point-in-time read filters on this column and nothing else. Same rule as
    -- news_articles.available_at (migration 0017).
    available_at       TIMESTAMPTZ NOT NULL,
    -- 1 = first print. A revision inserts a NEW row with vintage+1; nothing is
    -- ever updated in place, so the first print stays readable forever.
    vintage            INT NOT NULL DEFAULT 1 CHECK (vintage >= 1),
    is_nowcast         BOOLEAN NOT NULL DEFAULT FALSE,
    -- IMF WEO carries projections to 2031 in the same series as history. A
    -- projection is not an observation and must never be scored as one.
    is_projection      BOOLEAN NOT NULL DEFAULT FALSE,
    -- RESTRICT, not SET NULL: this table is append-only, and SET NULL would be
    -- the one path by which a stored observation is mutated after insert —
    -- silently erasing the citation that makes the value defensible. A cleanup
    -- that wants a document gone must deal with the rows that cite it.
    source_document_id BIGINT REFERENCES source_documents(id) ON DELETE RESTRICT,
    collected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT economic_obs_unique UNIQUE (series_id, ref_period_start, vintage),
    CONSTRAINT economic_obs_period CHECK (ref_period_end >= ref_period_start)
);
-- The point-in-time read: newest vintage of each period that was available at
-- a cutoff. Leading series_id, then period, then availability.
CREATE INDEX idx_econ_obs_pit ON economic_observations
    (series_id, ref_period_start DESC, available_at DESC, vintage DESC);
-- "what became knowable in this window" — drives the What Changed feed.
CREATE INDEX idx_econ_obs_available ON economic_observations (available_at DESC);

-- ------------------------------------------------------------------ providers
-- Two reference-macro sources, both verified answering on 2026-09-08:
--   World Bank  api.worldbank.org/v2 — Iran CPI index FP.CPI.TOTL through 2025
--   IMF WEO     imf.org/external/datamapper/api/v1 — PCPIPCH 1980..2031
-- Both are annual and both revise, which is exactly why they are the first
-- consumers of the vintage machinery above.
INSERT INTO data_providers (code, name, base_url, category, priority, enabled) VALUES
  ('worldbank','World Bank Indicators API','https://api.worldbank.org/v2','global_macro',10,TRUE),
  ('imf_weo','IMF DataMapper (WEO)','https://www.imf.org/external/datamapper/api/v1','global_macro',20,TRUE)
ON CONFLICT (code) DO NOTHING;
