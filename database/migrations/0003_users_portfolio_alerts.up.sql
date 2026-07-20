-- 0003: Users, portfolio, alerts, audit log, settings.

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,               -- bcrypt
    role          TEXT NOT NULL DEFAULT 'user', -- 'user' | 'admin'
    risk_tolerance TEXT NOT NULL DEFAULT 'medium', -- 'low'|'medium'|'high'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE portfolio_transactions (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tx_type        TEXT NOT NULL DEFAULT 'buy',   -- 'buy' | 'sell'
    grams          NUMERIC NOT NULL CHECK (grams > 0),
    karat          INT NOT NULL DEFAULT 18 CHECK (karat IN (18, 21, 22, 24)),
    price_per_gram NUMERIC NOT NULL CHECK (price_per_gram > 0),  -- in `currency` per gram
    currency       TEXT NOT NULL DEFAULT 'IRT' CHECK (currency IN ('IRT','IRR')),
    fees           NUMERIC NOT NULL DEFAULT 0 CHECK (fees >= 0), -- in same currency
    tx_date        DATE NOT NULL,
    notes          TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_portfolio_user ON portfolio_transactions (user_id, tx_date);

CREATE TABLE alerts (
    id               BIGSERIAL PRIMARY KEY,
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    alert_type       TEXT NOT NULL,  -- 'price_above'|'price_below'|'signal_change'|'confidence_above'|'volatility_spike'|'premium_above'|'stale_data'|'provider_failure'|'model_degradation'
    condition        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"threshold": 5000000}
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    cooldown_minutes INT NOT NULL DEFAULT 60,
    last_triggered_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_alerts_user ON alerts (user_id) WHERE enabled;

CREATE TABLE alert_events (
    id           BIGSERIAL PRIMARY KEY,
    alert_id     BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    message      TEXT NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    acknowledged BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_alert_events_user ON alert_events (user_id, triggered_at DESC);

CREATE TABLE audit_logs (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,   -- 'user.register','auth.login','portfolio.create',...
    entity     TEXT NOT NULL DEFAULT '',
    entity_id  TEXT NOT NULL DEFAULT '',
    details    JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip         TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_logs_time ON audit_logs (created_at DESC);

CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO app_settings (key, value) VALUES
  ('display_currency', '"IRT"'),
  ('stale_price_threshold_minutes', '30'),
  ('premium_alert_zscore', '2.0');
