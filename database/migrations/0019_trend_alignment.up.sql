-- Addendum 20: multi-timeframe trend alignment state + transition events.
--
-- Two tables, because they answer different questions:
--   * trend_alignment_states — one row per symbol: what is true NOW, plus the
--     closed candles that conclusion was drawn from.
--   * trend_alignment_events — an append-only log of ENTRIES into a full
--     alignment. Being aligned is not an event; becoming aligned is.
--
-- Idempotency is enforced by the database, not by application memory. The
-- evaluator is scheduled, restartable and may run concurrently after a deploy,
-- so "previous != current" held in a process cannot be the guard: a restart
-- would re-fire an alert the user already saw. The unique index below makes a
-- duplicate physically impossible for the same (symbol, alignment, closed
-- candle triple).

CREATE TABLE IF NOT EXISTS trend_alignment_states (
    symbol                 TEXT PRIMARY KEY,
    alignment              TEXT NOT NULL
        CHECK (alignment IN ('full_bullish', 'full_bearish', 'not_aligned')),
    previous_alignment     TEXT
        CHECK (previous_alignment IN ('full_bullish', 'full_bearish', 'not_aligned')),
    -- Per-timeframe evidence as evaluated (trend, price, MAs, freshness).
    timeframes             JSONB NOT NULL DEFAULT '{}'::jsonb,
    ma_type                TEXT NOT NULL DEFAULT 'ema',
    fast_period            INT  NOT NULL DEFAULT 26,
    mid_period             INT  NOT NULL DEFAULT 48,
    slow_period            INT  NOT NULL DEFAULT 220,
    data_fresh             BOOLEAN NOT NULL DEFAULT FALSE,
    -- The closed candles this state was computed from. Together these are the
    -- idempotency key: unchanged candles mean nothing new to decide.
    latest_1h_candle_close TIMESTAMPTZ,
    latest_4h_candle_close TIMESTAMPTZ,
    latest_1d_candle_close TIMESTAMPTZ,
    last_bullish_alert_at  TIMESTAMPTZ,
    last_bearish_alert_at  TIMESTAMPTZ,
    state_version          INT NOT NULL DEFAULT 1,
    calculated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trend_alignment_events (
    id                     BIGSERIAL PRIMARY KEY,
    symbol                 TEXT NOT NULL,
    alignment              TEXT NOT NULL
        CHECK (alignment IN ('full_bullish', 'full_bearish')),
    previous_alignment     TEXT,
    occurred_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    latest_1h_candle_close TIMESTAMPTZ NOT NULL,
    latest_4h_candle_close TIMESTAMPTZ NOT NULL,
    latest_1d_candle_close TIMESTAMPTZ NOT NULL,
    timeframes             JSONB NOT NULL DEFAULT '{}'::jsonb,
    ma_type                TEXT NOT NULL DEFAULT 'ema',
    -- Set once the in-app alert_events row has been written, so a crash
    -- between the two inserts cannot lose the notification.
    alert_event_id         BIGINT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- THE duplicate guard: one entry per (symbol, direction, closed-candle triple).
-- A re-run over the same candles conflicts and is skipped; a genuinely new
-- candle set produces a new row, which is exactly the reset behaviour
-- (aligned -> broken -> aligned fires again, because the candles moved on).
CREATE UNIQUE INDEX IF NOT EXISTS uq_trend_alignment_event_identity
    ON trend_alignment_events (symbol, alignment,
                               latest_1d_candle_close,
                               latest_4h_candle_close,
                               latest_1h_candle_close);

CREATE INDEX IF NOT EXISTS idx_trend_alignment_events_symbol_time
    ON trend_alignment_events (symbol, occurred_at DESC);
