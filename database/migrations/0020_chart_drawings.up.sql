-- Addendum 21: per-user chart drawings.
--
-- Drawings are user content, not market data. They are anchored to a
-- (symbol, interval) pair because a trend line drawn on the 5m chart means
-- nothing on the 1d one, and they are owned by a user because one trader's
-- annotations must never be readable or mutable by another. Every API path
-- filters on user_id; the FK cascade means deleting an account takes its
-- drawings with it instead of leaving rows keyed to a dead uuid.
--
-- points and style are JSONB rather than columns because their shape depends
-- on drawing_type (one anchor for a horizontal line, two corners for a
-- rectangle) and because style is presentation the API has no reason to
-- interpret. drawing_type is the one part the database does enforce: an
-- unknown type would render as nothing at all, which is invisible to debug.
--
-- "interval" is quoted throughout: INTERVAL is a type-name keyword in
-- Postgres, so the bare identifier is not usable in every expression
-- position. Quoted and lower-case, it is the same column either way.

CREATE TABLE IF NOT EXISTS chart_drawings (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    "interval"   TEXT NOT NULL,
    drawing_type TEXT NOT NULL
        CHECK (drawing_type IN ('trend_line', 'horizontal_line', 'vertical_line',
                                'rectangle', 'price_range', 'date_range',
                                'measure', 'fib_retracement', 'text')),
    -- [{"t": <unix seconds, UTC>, "price": <number>}, ...]. The anchor count
    -- is fixed per drawing_type and validated in the API, not here: the rule
    -- is a lookup table there, and a CHECK repeating it would drift.
    points       JSONB NOT NULL,
    style        JSONB NOT NULL DEFAULT '{}'::jsonb,
    locked       BOOLEAN NOT NULL DEFAULT FALSE,
    visible      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The only read pattern the API has: "every drawing this user made on this
-- chart". id trails the key so the id-ordered read is served by the same
-- scan, and so the per-chart cap counts rows without touching the heap.
CREATE INDEX IF NOT EXISTS idx_chart_drawings_user_chart
    ON chart_drawings (user_id, symbol, "interval", id);
