-- 0028: Tehran equity daily bars, stored RAW, with corporate actions and a
-- versioned adjustment whose verdict is a row rather than an assumption.
--
-- WHY THESE ARE NOT economic_observations
--
-- 0024 built `economic_observations` for revisable period statistics: one
-- NUMERIC value describing a reference PERIOD, restated by its publisher, read
-- point-in-time by vintage. A daily equity bar is none of those things. It is
-- five prices, two counts and a turnover for one SESSION; TSETMC does not
-- revise it; and it has no publication lag to model — the session ends and the
-- numbers are final. Forcing it into that table would mean either five rows
-- per bar (destroying the OHLC relationship the table cannot express) or a
-- JSON blob in a NUMERIC column. Migration 0024's own principle applies:
-- "equities and revisable macro get their own shapes because their keys
-- genuinely differ."
--
-- WHY `equity_instruments` IS A SEPARATE REGISTRY FROM `instruments`
--
-- This was the deliberate decision of this migration, not an oversight.
-- `instruments` is the vocabulary of the things this system PRICES AND MODELS
-- — thirteen hand-curated rows, each carrying a `quality_tier` and a `notes`
-- string a human wrote and the UI renders, and the single registry that seven
-- in-code symbol lists were collapsed into. Pouring ~700 Tehran-listed
-- companies into it would:
--
--   * swamp every existing consumer of GET /api/v1/instruments — the chart
--     symbol picker, SANITY_RANGES, JOB_SYMBOLS — with rows none of them can
--     serve a price for;
--   * demand a per-company `quality_tier` and `is_proxy` judgement that nobody
--     has made and that TSETMC does not supply; and
--   * make "which symbols does this system actually model?" unanswerable from
--     the table, which is the exact defect 0024 created it to remove.
--
-- So the equity roster is its own registry, and the two are JOINABLE rather
-- than merged: `equity_instruments.instrument_code` is a NULLABLE reference
-- into `instruments`. It is NULL for every row seeded here, and it exists
-- because the link is already real — IR_GOLD_FUND_AYAR, IR_GOLD_FUND_TALA and
-- IR_GOLD_FUND_KAHRABA are Tehran-listed instruments with `instruments` rows
-- today. When a listed equity becomes a first-class modelled instrument, one
-- UPDATE connects the two; until then, neither table lies about the other.
--
-- WHAT WAS MEASURED, 2026-09-10, and why it decides the schema
--
-- All twenty roster payloads were fetched and analysed before this file was
-- written. Every number below is from that measurement, not from a document.
--
-- 1. cdn.tsetmc.com is NOT reachable from the production host: DNS resolves to
--    94.182.113.115 / 46.102.143.218 / 212.16.75.245 and TCP 443 fails
--    outright — harder than the SCI block of 0027, where TCP opened and the
--    payload was dropped. So there is NO CRON for this data and there must not
--    be one. scripts/tsetmc_fetch.py fetches where TSETMC answers, copies the
--    payloads over the existing SSH access and calls
--    POST /internal/equities/bars inside the compose network, exactly as
--    scripts/sci_fetch.py does for the CPI workbook.
--
-- 2. The raw close series is nonsense and the adjusted one is not. فولاد's raw
--    close goes 1,900 -> 2,881 over nineteen years (x1.52) while the
--    corporate-action-adjusted series is x907.86. Four separate raw days drop
--    more than 37%. 2022-08-09 shows a raw close of 9,470 -> 5,460, a -42.3%
--    session that never happened; that row's `priceYesterday` is 5,240, and on
--    the adjusted series the day is +4.20%.
--
-- 3. TSETMC states the adjustment itself, in TWO different ways, and a
--    detector that knows only the first is wrong about one symbol in three.
--
--      a. `reference_restated` (443 of the 472 actions measured across the
--         roster): a row's `priceYesterday` differs from the previous row's
--         closing price. The exchange restated the opening reference, and the
--         ratio is exactly priceYesterday / previous_close.
--      b. `close_restated` (29 detected, in six symbols): a bar on which NOTHING TRADED
--         whose own `pClosing` differs from its own `priceYesterday`. No
--         session happened, so that difference is not a price move — the
--         action was applied to the closing price in place. شپنا 2014-03-16 is
--         the clearest case (27,031 -> 11,251 on a zero-volume bar) and وبملت
--         2012-09-26 the most consequential (1,533 -> 926).
--
--    Mechanism (b) was found by running the detector over all twenty symbols
--    rather than over the one it was written against, and the correction is
--    not marginal: وبملت's adjusted history moves from x146.95 to x803.70,
--    شپنا's from x407.85 to x1,643.21, کچاد's from x79.88 to x1,024.04. فولاد
--    has none of them, which is exactly why one validation case is not enough.
--
--    Nothing else is trustworthy: no source publishes a usable adjusted series
--    (BrsApi's "adjusted" and "unadjusted" samples are byte-identical across
--    4,038 rows and round a 3,704-rial close to 6; GetPriceAdjustList's event
--    dates land on suspension days).
--
-- 4. The closing price is NOT bounded by the session's high and low. 285 of
--    فولاد's 4,221 traded bars have a closing price outside [low, high], by up
--    to 3.9%, because TSE's قیمت پایانی is a volume-weighted average blended
--    with the previous close when traded volume is under the base volume. A
--    CHECK asserting low <= close <= high would refuse 285 genuine bars, so
--    this migration does not assert it. `low <= high` does hold everywhere and
--    is asserted.
--
-- 5. TSETMC emits placeholder bars. Of 77,344 bars across the twenty roster
--    symbols, 9,521 have zero volume and zero trades: the instrument was
--    halted, and the bar carries the previous closing price forward
--    (close = price_yesterday on every one of them). They are stored, because
--    this table stores what TSETMC served and a halt is a fact about the
--    session — and `volume = 0` is the machine-readable form of it. On such a
--    bar open/high/low are frequently 0, which is why no positivity CHECK is
--    placed on them: a zero there means "no trade occurred", and rewriting it
--    to NULL would be this table inventing a value.
--
-- 6. Before an instrument's first trade TSETMC serves a run of those
--    placeholder bars at the 1,000-rial par value — 161 of them for نوری,
--    which listed 2019-07-13 but has bars from 2018-11-10. They are prices of
--    nothing. They are stored raw and excluded from the adjusted series by the
--    engine, because a series that begins at par shows a +3,025% "return" on
--    its first trading day.
--
-- ROW-COUNT ARITHMETIC, so the next person can size the expansion
--
--   Measured 2026-09-10 over the twenty roster payloads:  77,344 bars,
--   mean 3,867 per symbol, max 5,874 (خودرو, from 2001-03-25).
--   The nineteen ENABLED below (کچاد is seeded disabled; see its note):
--     77,344 - 5,304 = 72,040 bars.
--   ~700 listed companies x ~4,600 bars = ~3.2M rows — a step change for a
--   154 MB database, and the reason this increment seeds a curated roster
--   instead of the whole exchange. Widening it is an INSERT into
--   `equity_instruments`, not a code change: scripts/tsetmc_fetch.py reads the
--   roster from GET /internal/equities/roster, and the ingest refuses any
--   insCode the roster does not carry.
--
--   At ~120 bytes of tuple plus the unique index, 72,040 bars is roughly
--   15 MB. The next tranche worth adding is the rest of the TEDPIX-30
--   constituents (~10 symbols, ~40k rows); the whole exchange is a decision
--   that needs a disk-space conversation, not a migration.

-- ------------------------------------------------------- equity_instruments
CREATE TABLE equity_instruments (
    -- TSETMC's own primary key for a listed instrument. TEXT, not BIGINT: it
    -- is an opaque identifier that TSETMC serves as a JSON STRING on some
    -- endpoints and a JSON NUMBER on others, and the values run to 17 digits.
    -- Storing it as text keeps the two representations from disagreeing and
    -- makes it useless for arithmetic, which is correct — it is a name.
    ins_code        TEXT PRIMARY KEY,
    -- The trading symbol (نماد), folded to Persian orthography with ZWNJ and
    -- the other invisible marks REMOVED. This is the lookup key the read API
    -- resolves /api/v1/stocks/{symbol} against, so it must be stable under the
    -- Arabic/Persian confusables a caller's keyboard produces: TSETMC serves
    -- فملي with Arabic YEH (U+064A) and كچاد with Arabic KAF (U+0643), while a
    -- Persian keyboard types U+06CC and U+06A9. Folding at rest and folding
    -- the request path is the only way both spellings reach the same row.
    symbol_fa       TEXT NOT NULL,
    -- The company name. Folded for the same confusables, but ZWNJ is KEPT:
    -- here it is a word separator, and stripping it renders
    -- معدنی‌وصنعتی‌چادرملو as one glued-together word. A key and a label want
    -- different normalisations, so they get different ones.
    name_fa         TEXT NOT NULL,
    market          TEXT NOT NULL CHECK (market IN ('bourse','farabourse')),
    board           TEXT NOT NULL DEFAULT '',   -- 'بازار اول (تابلوی اصلی) بورس'
    sector_code     TEXT NOT NULL DEFAULT '',   -- TSETMC cSecVal, e.g. '27'
    sector_fa       TEXT NOT NULL DEFAULT '',   -- lSecVal, e.g. 'فلزات اساسی'
    isin            TEXT NOT NULL DEFAULT '',
    -- The optional bridge into the modelled-instrument vocabulary; see the
    -- header. NULL means "listed, but this system does not model it", which is
    -- true of every row seeded here.
    instrument_code TEXT REFERENCES instruments(code) ON DELETE RESTRICT,
    -- Coverage. NULL until the first ingest: this migration deliberately does
    -- not pre-fill what it has not stored, so an empty table reads as empty
    -- rather than as a claim about bars that are not there.
    first_bar       DATE,
    last_bar        DATE,
    bar_count       INT  NOT NULL DEFAULT 0,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One row per symbol. TSETMC reuses a symbol across insCodes when an
    -- instrument is re-listed, so this is a constraint that can genuinely be
    -- hit; hitting it is the right outcome, because the read API resolves a
    -- symbol to exactly one instrument and must never pick between two.
    CONSTRAINT equity_instruments_symbol_unique UNIQUE (symbol_fa)
);
CREATE INDEX idx_equity_instruments_enabled ON equity_instruments (symbol_fa) WHERE enabled;
CREATE INDEX idx_equity_instruments_sector ON equity_instruments (sector_code);

-- --------------------------------------------------------------- equity_bars
--
-- RAW ONLY. Every column is exactly the number TSETMC served for that session.
-- No adjusted value is ever written here, and that is not merely a convention:
-- an adjusted price is a function of a corporate-action chain that changes
-- whenever a new action is detected, so storing one would mean silently
-- restating history on every ingest. The adjustment lives in
-- `corporate_actions` as a factor and is applied at READ time
-- (tests/test_equity_adjust.py asserts this table never receives one).
CREATE TABLE equity_bars (
    id              BIGSERIAL PRIMARY KEY,
    ins_code        TEXT NOT NULL REFERENCES equity_instruments(ins_code) ON DELETE CASCADE,
    -- TSETMC's `dEven`, an integer GREGORIAN date (20260909), parsed to a real
    -- DATE. It is NOT Jalali despite everything else on the site being so, and
    -- reading it as Jalali would place the whole series 621 years early.
    trade_date      DATE NOT NULL,
    -- priceFirst / priceMax / priceMin. Zero on a halted session, where they
    -- mean "no trade occurred"; see note 5 in the header.
    open            NUMERIC NOT NULL,
    high            NUMERIC NOT NULL,
    low             NUMERIC NOT NULL,
    -- pDrCotVal: the LAST TRADE of the session. NOT always inside [low, high]
    -- (verified across all 77,344 measured bars).
    close           NUMERIC NOT NULL,
    -- pClosing: TSE's official قیمت پایانی, a base-volume-weighted average.
    -- This — not `close` — is the number the corporate-action detection and
    -- every return is computed on, because `price_yesterday` is stated on the
    -- same basis. It is NOT bounded by [low, high]; see note 4.
    final_close     NUMERIC NOT NULL CHECK (final_close > 0),
    -- priceYesterday: the reference price the exchange opened this session
    -- against. Its disagreement with the previous session's `final_close` IS
    -- the corporate action. Zero only on the very first bar of an instrument,
    -- where there is no yesterday.
    price_yesterday NUMERIC NOT NULL CHECK (price_yesterday >= 0),
    volume          BIGINT  NOT NULL CHECK (volume >= 0),        -- qTotTran5J, shares
    trade_count     BIGINT  NOT NULL CHECK (trade_count >= 0),   -- zTotTran
    value           NUMERIC NOT NULL CHECK (value >= 0),         -- qTotCap, rials
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT equity_bars_unique UNIQUE (ins_code, trade_date),
    -- Asserted because it is true of every measured bar. low <= close <= high
    -- is NOT asserted, because it is false for 285 of فولاد's traded bars and
    -- the reason is a real exchange rule, not corruption.
    CONSTRAINT equity_bars_band CHECK (low <= high),
    CONSTRAINT equity_bars_nonneg CHECK (open >= 0 AND high >= 0 AND low >= 0 AND close >= 0)
);
-- The read path: one symbol, a date window, newest first.
CREATE INDEX idx_equity_bars_read ON equity_bars (ins_code, trade_date DESC);

-- ---------------------------------------------------------- corporate_actions
--
-- One detected action, kept with BOTH numbers that imply it, so the ratio is
-- auditable rather than asserted. `prev_close` and `price_yesterday` are
-- copied out of the two `equity_bars` rows rather than joined at read time on
-- purpose: an action is a claim about a specific pair of sessions, and a
-- re-ingest that changed either bar must show up as a contradiction here
-- instead of quietly re-deriving a different ratio.
CREATE TABLE corporate_actions (
    id                 BIGSERIAL PRIMARY KEY,
    ins_code           TEXT NOT NULL REFERENCES equity_instruments(ins_code) ON DELETE CASCADE,
    -- The session that OPENED on the new basis. The action itself happened
    -- between prev_trade_date's close and this session's open; TSETMC does not
    -- date it more precisely, and this column does not pretend it does.
    effective_date     DATE NOT NULL,
    prev_trade_date    DATE NOT NULL,
    prev_close         NUMERIC NOT NULL CHECK (prev_close > 0),
    price_yesterday    NUMERIC NOT NULL CHECK (price_yesterday > 0),
    -- Which of TSETMC's two signals this came from; see note 3 in the header.
    -- 'reference_restated' — the session opened on a new basis, so the audit
    --     pair is (prev_close, price_yesterday) and ratio is their quotient.
    -- 'close_restated'     — a halted bar's own close was restated, so the
    --     audit pair is (price_yesterday, restated_close) and `restated_close`
    --     is NOT NULL. Without this column the ratio on those 32 rows could
    --     not be checked against anything.
    kind               TEXT NOT NULL DEFAULT 'reference_restated'
                       CHECK (kind IN ('reference_restated','close_restated')),
    restated_close     NUMERIC CHECK (restated_close IS NULL OR restated_close > 0),
    -- Below 1 for a capital increase or a dividend (the price steps down);
    -- above 1 for a reverse action. Stored rather than computed so a read never
    -- has to divide, and so a stored ratio that disagrees with its own inputs
    -- is visible.
    ratio              NUMERIC NOT NULL CHECK (ratio > 0),
    -- The product of this action's ratio and every LATER action's ratio, under
    -- `adjustment_version`. A back-adjusted close is
    --     final_close * cumulative_factor(first action with effective_date >
    --                                     that bar's trade_date)
    -- and 1.0 when no action follows the bar. Verified bit-for-bit equal to
    -- the newest-first chaining walk over فولاد's 4,636 bars (max relative
    -- difference 0.0).
    --
    -- WHY THIS AND NOT A MATERIALISED `equity_bars_adjusted`: the adjusted
    -- series for فولاد is 31 numbers, not 4,636. Materialising it per version
    -- would multiply the largest table in this migration by the number of
    -- adjustment versions to store values that are one multiplication away
    -- from the raw ones — and would then need re-writing in full every time a
    -- new action is detected at the front of the series.
    cumulative_factor  NUMERIC NOT NULL CHECK (cumulative_factor > 0),
    -- Which engine version found and chained this. Part of the key, so a v2
    -- detector can be introduced beside v1 rather than destroying it, and a
    -- read that asks for one version can never see half of the other's chain.
    adjustment_version TEXT NOT NULL,
    detected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT corporate_actions_unique UNIQUE (ins_code, effective_date, adjustment_version),
    CONSTRAINT corporate_actions_order CHECK (prev_trade_date < effective_date),
    -- The audit pair each kind is explained by must actually be present. A
    -- 'close_restated' row without its restated close is a ratio nobody can
    -- check, which is the one thing this table exists to prevent.
    CONSTRAINT corporate_actions_evidence CHECK (
        (kind = 'reference_restated' AND restated_close IS NULL)
        OR (kind = 'close_restated' AND restated_close IS NOT NULL))
);
CREATE INDEX idx_corporate_actions_read
    ON corporate_actions (ins_code, adjustment_version, effective_date);

-- --------------------------------------------------------- equity_adjustments
--
-- The gate's verdict, per symbol per version. docs/REDESIGN.md makes this the
-- explicit gate for this phase: "adjustment must be validated before any
-- return, ratio or score is computed from it." A verdict that lives only in a
-- log cannot enforce that; a row can, and the read API refuses to serve an
-- adjusted series whose row here is not 'validated'.
--
-- WHAT THE GATE CHECKS: TWO TIERS, BECAUSE THERE ARE TWO POPULATIONS
--
-- Measured 2026-09-10 over the twenty roster symbols, on the ADJUSTED series.
--
-- GENUINE SESSIONS — both bars traded, at most four calendar days apart.
-- 65,802 pairs across twenty-five years:
--
--     p99      5.00%   <- the exchange's daily price limit, visible in the data
--     p99.9    6.94%
--     p99.99  12.26%
--     max     13.64%   (فملی, 2007-09-10)
--
-- and the only pairs beyond 20% in the entire set are کچاد's 2007-01-09
-- (-60.4%) and 2007-01-10 (+152.7%), where a single 227-million-share block
-- trade in two transactions dragged the official closing price to 5,576
-- against a market of ~14,090 and back. So the session bound is 25%: nearly
-- double the worst legitimate move measured, and far below the -42.3% that an
-- unadjusted فولاد shows on 2022-08-09.
--
-- REOPENINGS — either bar halted, or a longer gap. 11,269 pairs:
--
--     p99     14.23%
--     p99.9   60.29%
--     max    242.63%   (شپنا, 2013-02-09, after six halted sessions)
--
-- The exchange lifts the price limit for a reopening auction, and 48 of these
-- exceed 25% across FIFTEEN of the twenty symbols — including فولاد's own
-- -39.2% on 2008-10-26, after a 43-calendar-day suspension, on a bar whose
-- `priceYesterday` agrees with the previous close. Judging reopenings by the
-- session bound would refuse the very symbol this engine is validated against.
-- So they are held to a RETURN of 3.0, i.e. a 4.0x multiple instead, which is a bound on the ARITHMETIC and not
-- on the market: a broken factor chain produces jumps of ten times or more,
-- and the worst reopening in 77,344 measured bars is a RETURN of 2.4263, i.e. a 3.43x multiple.
--
-- WHAT THE GATE CANNOT DO, recorded here rather than left to be discovered
--
-- 30 of فولاد's 31 actions fall on a reopening, because an Iranian capital
-- increase happens during the AGM suspension. If TSETMC ever states nothing
-- for such an action, the resulting move is indistinguishable from a genuine
-- unlimited auction and no bound can separate them. The defence against that
-- is the DETECTOR, not the gate — which is why note 3 above matters as much as
-- this table does, and why `close_restated` was worth finding.

CREATE TABLE equity_adjustments (
    id                 BIGSERIAL PRIMARY KEY,
    ins_code           TEXT NOT NULL REFERENCES equity_instruments(ins_code) ON DELETE CASCADE,
    adjustment_version TEXT NOT NULL,
    -- 'validated' — every session return is inside the bound, so returns may
    -- be computed from this series.
    -- 'refused'   — at least one is not. A missed action silently corrupts
    -- every return computed from the series, so the series is not served
    -- adjusted at all and the reason is on the row.
    status             TEXT NOT NULL CHECK (status IN ('validated','refused')),
    actions_applied    INT  NOT NULL DEFAULT 0,
    bars_total         INT  NOT NULL DEFAULT 0,
    -- Leading never-traded placeholder bars, excluded from the adjusted series
    -- (header note 6). Reported rather than dropped silently: the number is
    -- how a reader knows the adjusted series starts later than the raw one.
    pre_listing_bars   INT  NOT NULL DEFAULT 0,
    sessions_checked   INT  NOT NULL DEFAULT 0,
    -- The largest |return| the gate actually saw, and where. Kept even on a
    -- pass, because "it passed at 13.6% against a 25% bound" and "it passed at
    -- 2%" are different amounts of comfort.
    worst_return       NUMERIC,
    worst_return_date  DATE,
    -- Moves beyond the bound across a halt or a multi-day gap. Not failures —
    -- see the header — but a return computed across one of these days is not a
    -- session return, and a reader is told how many there are.
    reopenings         INT  NOT NULL DEFAULT 0,
    -- The two bounds this verdict was reached under, stored so a row can be
    -- re-checked against the constants that produced it after they change.
    max_session_return NUMERIC NOT NULL,
    max_reopening_return NUMERIC NOT NULL DEFAULT 3.0,
    session_gap_days   INT  NOT NULL,      -- the session-boundary definition used
    first_bar          DATE,
    last_bar           DATE,
    refusal_reason     TEXT NOT NULL DEFAULT '',
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT equity_adjustments_unique UNIQUE (ins_code, adjustment_version),
    -- A refusal must say why. An empty reason on a refused series would leave
    -- an operator with nothing to act on and no way to tell a real refusal
    -- from a bug in the writer.
    CONSTRAINT equity_adjustments_reason CHECK (
        status <> 'refused' OR length(refusal_reason) > 0)
);

-- ------------------------------------------------------------------ provider
--
-- category 'iran_equity': a national exchange's own market data, which is
-- neither the 'iran_fund' TSETMC ETF collection (a handful of live NAV quotes
-- on a schedule) nor 'iran_macro'. `priority` is inert for the same reason it
-- is inert for 'sci' in 0027 — no collect job consults this category, and it
-- must not: a scheduled fetch from the production host would fail every tick
-- forever and park the circuit breaker on a provider that is not down.
INSERT INTO data_providers (code, name, base_url, category, priority, enabled) VALUES
  ('tsetmc_cdn','Tehran Securities Exchange Technology Management (CDN API)',
   'https://cdn.tsetmc.com','iran_equity',40,TRUE)
ON CONFLICT (code) DO NOTHING;

-- -------------------------------------------------------------- the roster
--
-- Twenty of the most-traded Tehran symbols, every insCode resolved live
-- through GetInstrumentSearch on 2026-09-10 and confirmed against
-- GetInstrumentInfo — not transcribed from a list. Coverage columns are left
-- NULL for the ingest to fill; the measured bar counts are in the comment on
-- each row so the arithmetic above can be checked without a database.
--
-- فولاد is here first and foremost as the VALIDATION CASE: its measured
-- x907.86 adjusted / x1.52 raw over 31 actions, and the 2022-08-09 artefact,
-- are what tests/test_equity_adjust.py asserts the engine reproduces.
INSERT INTO equity_instruments
  (ins_code, symbol_fa, name_fa, market, board, sector_code, sector_fa, isin, enabled, notes)
VALUES
  ('46348559193224090','فولاد','فولاد مبارکه اصفهان','bourse','بازار اول (تابلوی اصلی) بورس','27','فلزات اساسی','IRO1FOLD0009',TRUE,
    'The adjustment engine''s validation case. 4,636 bars from 2007-03-11; 31 corporate actions; raw x1.52, adjusted x907.86. Contains the 2022-08-09 artefact (raw -42.3%, adjusted +4.20%) and a genuine -39.2% reopening on 2008-10-26 after a 43-day suspension.'),
  ('65883838195688438','خودرو','ایران‌ خودرو','bourse','بازار دوم (تابلوی فرعی) بورس','34','خودرو و ساخت قطعات','IRO1IKCO0008',TRUE,
    '5,874 bars from 2001-03-25 — the longest history in the roster. Nine reopening moves beyond the session bound, including an exact x2 on 2020-05-16 after a five-session halt.'),
  ('44891482026867833','خساپا','سایپا','bourse','بازار دوم (تابلوی فرعی) بورس','34','خودرو و ساخت قطعات','IRO1SIPA0001',TRUE,
    '5,794 bars from 2001-05-06. Only 10 corporate actions over 25 years, the fewest in the roster.'),
  ('18027801615184692','کچاد','معدنی‌وصنعتی‌چادرملو','bourse','بازار اول (تابلوی اصلی) بورس','13','استخراج کانه های فلزی','IRO1CHML0000',FALSE,
    'SEEDED DISABLED, deliberately, and this is the measurement rather than a policy: 5,304 bars, 47 actions, and the adjusted series still contains -60.4% on 2007-01-09 and +152.7% on 2007-01-10. That is not a missed corporate action — a single block trade of 227,000,010 shares in two transactions set the official closing price to 5,576 against a market of ~14,090, and it recovered the next session. The gate refuses the symbol either way, because a gate that exempts the one series it cannot explain is not a gate. Enable it when the closing-price artefact is handled explicitly.'),
  ('35700344742885862','کگل','معدنی و صنعتی گل گهر','bourse','بازار اول (تابلوی اصلی) بورس','13','استخراج کانه های فلزی','IRO1GOLG0005',TRUE,
    '5,034 bars from 2004-08-29; 60 corporate actions, the most in the roster.'),
  ('35425587644337450','فملی','ملی‌ صنایع‌ مس‌ ایران‌','bourse','بازار اول (تابلوی اصلی) بورس','27','فلزات اساسی','IRO1MSMI0000',TRUE,
    '4,648 bars from 2007-02-04; 32 actions; adjusted x6,131.86, the largest in the roster.'),
  ('48990026850202503','خگستر','گسترش‌سرمایه‌گذاری‌ایران‌خودرو','bourse','بازار اول (تابلوی فرعی) بورس','34','خودرو و ساخت قطعات','IRO1GOST0003',TRUE,
    '4,380 bars from 2006-12-23; 22 actions.'),
  ('22811176775480091','اخابر','مخابرات ایران','bourse','بازار دوم (تابلوی فرعی) بورس','64','مخابرات','IRO1MKBT0008',TRUE,
    '4,348 bars from 2008-08-09; 23 actions, three of them close_restated. The 2020-08-10 reopening (+66.8% after a five-session halt) is the roster''s clearest example of a real move a naive gate would call an error.'),
  ('7745894403636165','شپنا','پالایش نفت اصفهان','bourse','بازار اول (تابلوی اصلی) بورس','23','فراورده های نفتی، کک و سوخت هسته ای','IRO1PNES0000',TRUE,
    '4,341 bars from 2008-06-29; 28 actions, five of them close_restated; 879 halted sessions, the most in the roster. Detecting only the priceYesterday signal understates its adjusted history by 4x (x407.85 against x1,643.21) and leaves a -58.4% artefact on 2014-03-16.'),
  ('778253364357513','وبملت','بانک ملت','bourse','بازار اول (تابلوی اصلی) بورس','57','بانکها و موسسات اعتباری','IRO1BMLT0007',TRUE,
    '4,234 bars from 2009-02-15; 26 actions, nine of them close_restated — the most in the roster. Its 2012-09-26 capital increase (1,533 -> 926 on a zero-volume bar) is invisible to a priceYesterday-only detector, which understates the whole history by 5.5x.'),
  ('63917421733088077','وتجارت','بانک تجارت','bourse','بازار اول (تابلوی فرعی) بورس','57','بانکها و موسسات اعتباری','IRO1BTEJ0001',TRUE,
    '4,185 bars from 2009-05-05; 21 actions; 9 pre-listing placeholder bars.'),
  ('28320293733348826','وبصادر','بانک صادرات ایران','bourse','بازار دوم (تابلوی فرعی) بورس','57','بانکها و موسسات اعتباری','IRO1BSDR0003',TRUE,
    '4,161 bars from 2009-06-08; 20 actions.'),
  ('9536587154100457','وپاسار','بانک پاسارگاد','bourse','بازار اول (تابلوی اصلی) بورس','57','بانکها و موسسات اعتباری','IRO1BPAS0008',TRUE,
    '3,630 bars from 2011-08-13; 23 actions, six of them close_restated.'),
  ('35366681030756042','شبندر','پالایش نفت بندرعباس','bourse','بازار اول (تابلوی اصلی) بورس','23','فراورده های نفتی، کک و سوخت هسته ای','IRO1PNBA0003',TRUE,
    '3,468 bars from 2012-04-16, of which 45 are pre-listing placeholders at par; first trade 2012-06-24. 21 actions; adjusted x1,819.88.'),
  ('25244329144808274','فارس','صنایع پتروشیمی خلیج فارس','bourse','بازار اول (تابلوی فرعی) بورس','44','محصولات شیمیایی','IRO1PKLJ0005',TRUE,
    '3,251 bars from 2013-03-10, of which 20 are pre-listing placeholders; first trade 2013-04-16; 21 actions. Without excluding them the series shows a +650% first day.'),
  ('68635710163497089','همراه','شرکت ارتباطات سیار ایران','bourse','بازار دوم (تابلوی فرعی) بورس','64','مخابرات','IRO1HMRZ0007',TRUE,
    '3,146 bars from 2013-08-20; 22 actions; no reopening beyond the session bound in thirteen years.'),
  ('51617145873056483','شتران','پالایش نفت تهران','bourse','بازار اول (تابلوی اصلی) بورس','23','فراورده های نفتی، کک و سوخت هسته ای','IRO1PTEH0007',TRUE,
    '2,369 bars from 2016-10-27, of which 3 are pre-listing placeholders; 21 actions.'),
  ('19040514831923530','نوری','پتروشیمی نوری','bourse','بازار دوم (تابلوی اصلی) بورس','44','محصولات شیمیایی','IRO1NORI0008',TRUE,
    '1,876 bars from 2018-11-10, of which 161 are pre-listing placeholders at 1,000 rials — the longest such run in the roster; first trade 2019-07-13 at 31,250. Including them would show a +3,025% first day.'),
  ('2400322364771558','شستا','سرمایه گذاری تامین اجتماعی','bourse','بازار اول (تابلوی فرعی) بورس','39','شرکتهای چند رشته ای صنعتی','IRO1TAMN0006',TRUE,
    '1,535 bars from 2020-04-13; first trade 2020-04-15; 11 actions.'),
  ('71483646978964608','ذوب','ذوب آهن اصفهان','bourse','بازار دوم (تابلوی فرعی) بورس','27','فلزات اساسی','IRO1ZOBI0002',TRUE,
    '1,130 bars from 2021-12-15, of which 7 are pre-listing placeholders; only 4 actions — the shortest history in the roster.')
ON CONFLICT (ins_code) DO NOTHING;
