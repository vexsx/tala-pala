-- 0002: Model registry, training runs, predictions, signals.

CREATE TABLE model_versions (
    id             SERIAL PRIMARY KEY,
    horizon        TEXT NOT NULL,          -- '1h','4h','eod','1d','3d','7d','30d'
    model_name     TEXT NOT NULL,          -- 'naive','sma','ses','arima','linear','rf','gbr','ensemble'
    version        TEXT NOT NULL,          -- e.g. '2026-07-20T10:00:00Z'
    trained_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    training_start TIMESTAMPTZ,
    training_end   TIMESTAMPTZ,
    n_observations INT,
    metrics        JSONB NOT NULL DEFAULT '{}'::jsonb,      -- walk-forward metrics: mae,rmse,smape,dir_acc,interval_coverage
    baseline_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,    -- naive baseline on same folds
    params         JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_path  TEXT,                   -- path inside the prediction-service models volume
    is_active      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_versions_unique UNIQUE (horizon, model_name, version)
);
CREATE INDEX idx_model_versions_active ON model_versions (horizon, is_active);

CREATE TABLE training_runs (
    id               SERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'running',   -- 'running'|'succeeded'|'failed'
    horizons         TEXT[] NOT NULL DEFAULT '{}',
    models_evaluated JSONB NOT NULL DEFAULT '[]'::jsonb,
    selected         JSONB NOT NULL DEFAULT '{}'::jsonb, -- horizon -> chosen model_name
    error            TEXT,
    notes            TEXT
);

CREATE TABLE predictions (
    id               BIGSERIAL PRIMARY KEY,
    symbol           TEXT NOT NULL DEFAULT 'IR_GOLD_18K',
    horizon          TEXT NOT NULL,
    model_version_id INT REFERENCES model_versions(id) ON DELETE SET NULL,
    model_name       TEXT NOT NULL,
    predicted_at     TIMESTAMPTZ NOT NULL,      -- when the forecast was made
    target_time      TIMESTAMPTZ NOT NULL,      -- the time being forecast
    point_forecast   NUMERIC NOT NULL,          -- IRT per gram
    lower_bound      NUMERIC NOT NULL,
    upper_bound      NUMERIC NOT NULL,
    expected_change_pct DOUBLE PRECISION NOT NULL,
    direction        TEXT NOT NULL,             -- 'up'|'down'|'flat'
    confidence       DOUBLE PRECISION NOT NULL, -- 0..1
    regime           TEXT NOT NULL DEFAULT 'unknown', -- 'trending_up'|'trending_down'|'ranging'|'high_volatility'|'unknown'
    drivers          JSONB NOT NULL DEFAULT '[]'::jsonb,
    data_fresh       BOOLEAN NOT NULL DEFAULT TRUE,
    warnings         JSONB NOT NULL DEFAULT '[]'::jsonb,
    actual_value     NUMERIC,                   -- filled in later for live-accuracy tracking
    actual_recorded_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_predictions_horizon_time ON predictions (horizon, predicted_at DESC);
CREATE INDEX idx_predictions_target ON predictions (horizon, target_time) WHERE actual_value IS NULL;

CREATE TABLE signals (
    id           BIGSERIAL PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal       TEXT NOT NULL,        -- 'strong_buy'|'buy'|'hold'|'sell'|'strong_sell'
    score        INT NOT NULL,         -- 0..100 (higher = more bullish)
    confidence   DOUBLE PRECISION NOT NULL,
    explanation  TEXT NOT NULL,
    supporting   JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicting  JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks        JSONB NOT NULL DEFAULT '[]'::jsonb,
    invalidation TEXT NOT NULL DEFAULT '',
    review_at    TIMESTAMPTZ,
    data_fresh   BOOLEAN NOT NULL DEFAULT TRUE,
    inputs       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_signals_time ON signals (generated_at DESC);

CREATE TABLE backtest_runs (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    horizon     TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- fees, spread, slippage, min_holding, etc.
    period_start TIMESTAMPTZ,
    period_end   TIMESTAMPTZ,
    results     JSONB NOT NULL DEFAULT '{}'::jsonb,   -- strategy + benchmarks metrics
    status      TEXT NOT NULL DEFAULT 'succeeded',
    error       TEXT
);
