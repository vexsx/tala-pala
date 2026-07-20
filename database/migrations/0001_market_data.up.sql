-- 0001: Core market data tables.
-- Convention: all timestamps are stored in UTC (timestamptz).
-- Currency codes: 'IRT' = Iranian toman (1 toman = 10 rials), 'IRR' = Iranian rial, 'USD'.
-- Normalized prices ALWAYS use IRT for Iranian instruments; raw observations keep the
-- provider's original unit so the conversion is auditable.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE data_providers (
    id                   SERIAL PRIMARY KEY,
    code                 TEXT NOT NULL UNIQUE,          -- e.g. 'tgju', 'alanchand', 'yahoo', 'frankfurter'
    name                 TEXT NOT NULL,
    base_url             TEXT NOT NULL DEFAULT '',
    category             TEXT NOT NULL,                 -- 'iran_gold' | 'global_gold' | 'fx' | 'macro'
    priority             INT  NOT NULL DEFAULT 100,     -- lower = tried first
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    last_success_at      TIMESTAMPTZ,
    last_error_at        TIMESTAMPTZ,
    last_error           TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every fetched datapoint exactly as the provider returned it.
CREATE TABLE raw_observations (
    id            BIGSERIAL PRIMARY KEY,
    provider_code TEXT NOT NULL,
    symbol        TEXT NOT NULL,                 -- canonical symbol, e.g. 'IR_GOLD_18K'
    raw_value     NUMERIC NOT NULL,
    unit          TEXT NOT NULL,                 -- provider's unit, e.g. 'IRR/gram', 'USD/ozt'
    currency      TEXT NOT NULL,                 -- provider's currency: 'IRR' | 'IRT' | 'USD'
    raw_payload   JSONB,
    observed_at   TIMESTAMPTZ NOT NULL,          -- when the value was valid at the source
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    quality       TEXT NOT NULL DEFAULT 'ok',    -- 'ok' | 'suspect' | 'outlier' | 'stale'
    dedupe_key    TEXT NOT NULL UNIQUE           -- provider|symbol|observed_at|value hash
);
CREATE INDEX idx_raw_obs_symbol_time ON raw_observations (symbol, observed_at DESC);

-- Normalized, validated series used by features/models/UI.
CREATE TABLE prices (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT NOT NULL,        -- 'IR_GOLD_18K','XAUUSD','USD_IRT','IR_COIN_EMAMI','XAGUSD','BRENT_OIL','DXY','US10Y'
    value        NUMERIC NOT NULL,     -- normalized value
    currency     TEXT NOT NULL,        -- 'IRT' | 'USD' | 'INDEX' | 'PCT'
    unit         TEXT NOT NULL,        -- 'gram' | 'ozt' | 'coin' | 'usd' | 'index' | 'pct' | 'bbl'
    source       TEXT NOT NULL,        -- provider code that produced this row
    observed_at  TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    quality      TEXT NOT NULL DEFAULT 'ok',
    CONSTRAINT prices_positive CHECK (value > 0 OR currency IN ('INDEX','PCT')),
    CONSTRAINT prices_unique UNIQUE (symbol, observed_at, source)
);
CREATE INDEX idx_prices_symbol_time ON prices (symbol, observed_at DESC);
CREATE INDEX idx_prices_symbol_quality_time ON prices (symbol, quality, observed_at DESC);

-- Point-in-time feature snapshots used for training and prediction (audited, replayable).
CREATE TABLE feature_snapshots (
    id       BIGSERIAL PRIMARY KEY,
    symbol   TEXT NOT NULL DEFAULT 'IR_GOLD_18K',
    as_of    TIMESTAMPTZ NOT NULL,     -- features computed using ONLY data observed at/before this time
    features JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT feature_snapshots_unique UNIQUE (symbol, as_of)
);
CREATE INDEX idx_feature_snapshots_time ON feature_snapshots (symbol, as_of DESC);

-- Seed the provider registry (enabled/priority configurable at runtime).
INSERT INTO data_providers (code, name, base_url, category, priority) VALUES
  ('tgju',        'TGJU (tgju.org)',                'https://api.tgju.org',            'iran_gold',   10),
  ('alanchand',   'Alanchand',                      'https://alanchand.com',           'iran_gold',   20),
  ('navasan',     'Navasan API',                    'https://api.navasan.tech',        'fx',          15),
  ('yahoo',       'Yahoo Finance (GC=F, SI=F, ...)','https://query1.finance.yahoo.com','global_gold', 10),
  ('stooq',       'Stooq CSV',                      'https://stooq.com',               'global_gold', 20),
  ('metals_dev',  'metals.dev API',                 'https://api.metals.dev',          'global_gold', 30),
  ('frankfurter', 'Frankfurter ECB rates',          'https://api.frankfurter.app',     'fx',          40);
