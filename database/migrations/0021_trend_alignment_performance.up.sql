-- Addendum 22: the measured track record of the trend-alignment indicator.
--
-- One row per (symbol, window_days): what the indicator would have said at
-- every bar close inside that window, and what price did over the next day.
--
-- THE REASON `basis` EXISTS. `prices` holds ticks, and until 2026-07-20 there
-- was exactly ONE tick per day for the evaluated symbols; the 5-minute stream
-- starts there. The 4H and 1H legs of the alignment therefore have a few weeks
-- of history and the 1D leg has years. A 90-day track record of the THREE-
-- timeframe alignment does not exist and cannot be manufactured, so every row
-- names the basis it was actually computed on:
--
--   'full_mtf'   — the real 1D+4H+1H alignment. Only inside the intraday window,
--                  and only once all three legs have warmed up there.
--   'daily_only' — the 1D leg alone (price vs the three MAs on daily candles).
--
-- The two are never mixed inside a row and a daily-only row is never labelled
-- as the multi-timeframe alignment. Nothing is padded, forward-filled or
-- interpolated to reach a window: a window the data cannot support comes back
-- on the weaker basis with `note` saying so.
--
-- Statistic columns are NULLABLE on purpose. "The alignment was never bullish
-- in this window" and "it was bullish and returned 0.0%" are different facts,
-- and a NOT NULL DEFAULT 0 would collapse them into the second one — which
-- reads as a measurement of the indicator rather than an absence of one. The
-- counts (samples, *_bars, *_episodes) are NOT NULL because a count of zero IS
-- the measurement; a rate over zero bars is not.

CREATE TABLE IF NOT EXISTS trend_alignment_performance (
    symbol                  TEXT NOT NULL,
    window_days             INT  NOT NULL,
    basis                   TEXT NOT NULL
        CHECK (basis IN ('full_mtf', 'daily_only')),
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Bounds of the bar closes actually replayed. NULL when the window held no
    -- usable bar at all, which is a different statement from a zero-width span.
    evaluated_from          TIMESTAMPTZ,
    evaluated_to            TIMESTAMPTZ,

    -- samples is the denominator of every rate below: bars whose forward
    -- window was fully covered by data. bullish_bars + bearish_bars +
    -- unaligned_bars = samples. The episode counts are contiguous runs of a
    -- state over every replayed bar, so a hit rate can always be read next to
    -- "and that was N distinct episodes, not N independent trades".
    samples                 INT NOT NULL DEFAULT 0,
    bullish_episodes        INT NOT NULL DEFAULT 0,
    bearish_episodes        INT NOT NULL DEFAULT 0,
    bullish_bars            INT NOT NULL DEFAULT 0,
    bearish_bars            INT NOT NULL DEFAULT 0,
    unaligned_bars          INT NOT NULL DEFAULT 0,

    -- Mean forward return over the next 1 day from the bar's close, in percent,
    -- attributed to the state in force at that close. The horizon is the same
    -- for both bases so rows stay comparable.
    fwd_return_bullish_pct  DOUBLE PRECISION,
    fwd_return_bearish_pct  DOUBLE PRECISION,
    -- Every bar in the window, whatever the state. Stored beside the
    -- conditional returns because a conditional return alone invites the
    -- reader to credit the indicator for a market that was rising anyway.
    fwd_return_baseline_pct DOUBLE PRECISION,
    -- Fraction in [0,1]. Bullish hits when the forward return is > 0, bearish
    -- when it is < 0: a bearish call is right when the price falls.
    hit_rate_bullish        DOUBLE PRECISION,
    hit_rate_bearish        DOUBLE PRECISION,

    -- Plain-language limitation for THIS row: which basis, why that basis, and
    -- how much of the window the data supports. Per row, because the answer
    -- differs per window.
    note                    TEXT NOT NULL DEFAULT '',
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One row per symbol and window: the refresh is an upsert, so a re-run
    -- restates the window rather than appending a second version of it.
    PRIMARY KEY (symbol, window_days)
);

-- The only read pattern the API has: every window for one symbol, longest
-- first. The primary key already orders by (symbol, window_days) ascending;
-- this index lets the DESC read walk it without a sort.
CREATE INDEX IF NOT EXISTS idx_trend_alignment_performance_symbol_window
    ON trend_alignment_performance (symbol, window_days DESC);
