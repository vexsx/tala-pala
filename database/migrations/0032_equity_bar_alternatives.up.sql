-- 0032: the other copies TSETMC served of a stored equity bar.
--
-- WHY THIS EXISTS
--
-- TSETMC's CDN served two copies of the roster's histories five minutes apart
-- on 2026-09-29, and they disagree on a handful of settled bars by a trade or
-- two. equity_bars never overwrites a bar (app/equities/ingest.py), so the
-- copy stored first is the one kept, and the corporate actions are derived
-- from the STORED bars. One copy also disagrees with ITSELF: کگل's 2021-12-15
-- close is 20,620 there while the next session's reference is 20,610 in both
-- copies, and the other copy's close is 20,610. Stored from that copy first,
-- the break in the reference chain is detected as a corporate action
-- (x0.999515, 0.05% on every adjusted price before it); stored from the other
-- copy first, there is none — and since the stored bar never changes, which
-- one a deployment holds depended for good on which copy answered first
-- (replayed both orders of the three archived runs: exactly that difference).
--
-- A decision taken from each payload alone would flip with every copy served.
-- So the ingest keeps, beside the stored bar, every other statement of it a
-- copy has served (a restated bar, in the ingest report's words), and a break
-- in the reference chain that some served copy of the two bars chains is
-- CONTESTED: not an action, and named in the ingest report. The stored bar is
-- still never overwritten; this table is evidence, not a vintage of the bar.
CREATE TABLE equity_bar_alternatives (
    id              BIGSERIAL PRIMARY KEY,
    ins_code        TEXT NOT NULL REFERENCES equity_instruments(ins_code) ON DELETE CASCADE,
    trade_date      DATE NOT NULL,
    -- The served copy's pClosing, priceYesterday and qTotTran5J: the three
    -- fields a stored bar is compared on.
    final_close     NUMERIC NOT NULL CHECK (final_close > 0),
    price_yesterday NUMERIC NOT NULL CHECK (price_yesterday >= 0),
    volume          BIGINT  NOT NULL CHECK (volume >= 0),
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT equity_bar_alternatives_unique
        UNIQUE (ins_code, trade_date, final_close, price_yesterday, volume)
);
