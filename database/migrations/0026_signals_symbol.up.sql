-- 0026: `signals` names the asset it describes, and loses the default that
-- made forgetting to name one look exactly like gold.
--
-- WHY THIS EXISTS
--
-- 1. The buy/hold/sell table has been single-asset since 0002, and nothing in
--    a row says so. app/signals/engine.py hardcoded IR_GOLD_18K in every
--    place it could: the price query was the literal tuple
--    ('IR_GOLD_18K','USD_IRT','XAUUSD') scored on the gold leg, the forecast
--    loader defaulted to IR_GOLD_18K, and the freshness and market-hours
--    calls named it too. So all 1,222 rows in production are readings of
--    Iranian 18k gold — that is not an inference about them, it is the only
--    thing the writer was capable of producing.
--
--    The engine now scores every asset that has real data behind it: seven
--    symbols, listed and justified one by one in
--    prediction-python/app/signals/universe.py. A row that cannot say which
--    asset it is about stops being readable the moment the second symbol is
--    written.
--
-- 2. The backfill and the default are the SAME statement, deliberately.
--    ADD COLUMN ... NOT NULL DEFAULT on PostgreSQL 11+ stamps every existing
--    row without rewriting the table, which is precisely the backfill point 1
--    justifies. The default then exists for exactly one more statement.
--
-- 3. Dropping it is the point of this migration, not an afterthought. Three
--    tables in this schema carry `symbol TEXT NOT NULL DEFAULT 'IR_GOLD_18K'`
--    — feature_snapshots (0001), predictions (0002), model_versions (0010) —
--    and the audit flagged exactly that shape. While gold was the only symbol
--    the default was harmless. Once several symbols are written it becomes a
--    trap: a writer that forgets to set the symbol does not fail, it files
--    its row under gold, and the row is then served to a reader as a buy/sell
--    call on an asset it was never computed for. A mislabelled signal is
--    worse than a missing one, because nothing downstream can detect it.
--    With NOT NULL and no default, that same bug is an INSERT error at the
--    first write instead of a wrong recommendation months later.
--
--    The three older columns are deliberately left alone. Dropping their
--    defaults changes behaviour for writers this migration does not otherwise
--    touch, and each needs its own audit of who inserts into it; that is a
--    separate migration, not a drive-by here.
--
-- 4. The index mirrors idx_predictions_symbol_horizon_predicted (0014), for
--    the same reason 0014 existed: `predictions` grew a symbol column in 0010
--    while every symbol-filtered read still leaned on a pre-multi-symbol
--    index. Every read of `signals` is now "the latest row(s) for THIS
--    symbol" — /api/v1/signals/current?symbol=, the overview's latest-per-
--    symbol scan, and /api/v1/signals/history?symbol=. The existing
--    idx_signals_time (generated_at DESC) leads on the wrong column for all
--    three: it would walk the whole table newest-first discarding six symbols
--    out of seven to find one row. idx_signals_time is kept — an unfiltered
--    "latest across everything" read is still a legitimate query.

ALTER TABLE signals
    ADD COLUMN symbol TEXT NOT NULL DEFAULT 'IR_GOLD_18K';

-- The rows are stamped; the default has done its whole job. From here a
-- writer that omits the symbol must ERROR (see 3 above).
ALTER TABLE signals
    ALTER COLUMN symbol DROP DEFAULT;

CREATE INDEX IF NOT EXISTS idx_signals_symbol_time
    ON signals (symbol, generated_at DESC);
