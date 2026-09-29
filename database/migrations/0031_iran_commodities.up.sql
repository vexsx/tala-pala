-- 0031: Iranian silver and the other Iranian gold instruments, as TGJU's daily
-- settled closes.
--
-- WHY THIS EXISTS
--
-- Relative value and Purchasing power could compare Tehran 18k gold, the Emami
-- coin, the dollar and the global futures -- and nothing else Iranian. Iranian
-- silver had no local series at all (XAGUSD is the COMEX front month in USD per
-- troy ounce, not the toman price of a gram in Tehran), and the Bahar Azadi,
-- half, quarter and gram coins, 24k gold and melted gold per mesghal were not
-- registered. Every one of them is published by TGJU as a daily close with a
-- long history, and a registry row plus `prices` rows is all either page needs:
-- both read every enabled IRT/USD row of this table.
--
-- WHERE THE NUMBERS COME FROM
--
-- One endpoint, read server-side by app/jobs/tgju_daily.py twice a day:
--   https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}
-- Measured 2026-09-29 (from this Mac and from the production host, which
-- answers in ~2 s with the honest User-Agent): rows newest first,
-- [open, low, high, close, change, change%, Gregorian date, Jalali date], every
-- number a comma-formatted RIAL string; no paging parameter returns the whole
-- table, length=N the N newest rows, and length=-1 silently drops the oldest
-- row. The day in progress is never in the table.
--
--   slug        rows  first       last close 2026-09-28 (rial)
--   silver_999  1674  2020-10-31      5,065,700 per gram
--   sekeb       3496  2013-07-22  2,408,000,000 per coin
--   nim         3438  2013-07-22  1,270,000,000 per coin
--   rob         3423  2013-07-22    680,000,000 per coin
--   gerami      3360  2013-07-22    360,000,000 per coin
--   geram24     3374  2014-05-02    325,346,000 per gram
--   mesghal     3517  2013-07-22  1,057,020,000 per mesghal
--
-- The job stores CLOSE / 10 as toman with source 'tgju_history', stamped 23:00
-- UTC on the bar's own Gregorian date (02:30 Tehran the next morning, after the
-- bazaar has closed -- the convention jobs/tgju_backfill.py already uses), and
-- only for days strictly before the current Tehran date. Every stored close has
-- a raw_observations row holding TGJU's own rial number. These are NOT live
-- quotes and are deliberately not added to live collection (JOB_SYMBOLS, the
-- provider SLUG_MAPs): one settled close per day is what TGJU's table is, and
-- what these rows claim to be.
--
-- TWO OF THE SEVEN ARE ARITHMETIC, AND THE NEW COLUMN SAYS SO
--
-- TGJU does not observe 24k gold or melted gold per mesghal; it publishes them
-- as fixed multiples of its 18k gram price. Measured over 2,963 shared days
-- (2015-08-30..2026-09-28), geram24 / geram18 has median 1.33332 (x 4/3) and
-- mesghal / geram18 median 4.3316 (x 4.3318, i.e. 4.6083 g at 705 per mille).
-- So measured in grams of 18k gold they are CONSTANT BY CONSTRUCTION, and any
-- movement left in such a ratio here is the difference between TGJU's 18k and
-- the live 18k feed in `prices`, not a market fact.
--
-- `is_derived` cannot carry that: it means "computed by us", and these are
-- observed from TGJU. A free-text note alone cannot carry it either, because
-- the performance table has to act on it -- it already publishes null instead
-- of 0.00% for 18k gold measured in 18k gold, and must do the same for 24k
-- measured in 18k. Hence `derived_from`: the instrument whose price this one's
-- SOURCE multiplies, NULL for everything observed in its own right. Nullable,
-- so every existing row and every existing INSERT is untouched.
--
-- QUALITY TIERS
--
-- The coins, 24k and mesghal take 'official_mirror', exactly as IR_COIN_EMAMI
-- and IR_GOLD_18K did in 0024: a public mirror of the Tehran bazaar price.
-- Silver takes 'commercial': TGJU is its ONLY source here, nothing corroborates
-- it, and its table carries junk bars (below) that a mirror of a real market
-- would not.
--
-- SILVER'S JUNK BARS
--
-- silver_999 holds three closes no market printed: 8,000 rial on 2021-08-10 and
-- 8,200 on 2021-08-29 (against ~200,000 on either side), and 2,008,800 on
-- 2022-09-30 (x10). The job flags any close more than x1.6 away from the median
-- of the seven closes either side of it; over the whole of every table above
-- that rule flags exactly those three and nothing else. A flagged close is kept
-- in raw_observations as quality 'suspect', with the median it was judged
-- against, and never written to `prices`.
--
-- CALENDAR
--
-- 'tehran_bazaar', as IR_COIN_EMAMI: markethours (Go) and core/market_hours.py
-- (Python) list the seven codes on the Iranian bazaar calendar in the same
-- change.
--
-- ON CONFLICT DO NOTHING, as in 0024: a row that already exists keeps its notes,
-- because tgju_backfill and tgju_daily append their own sentences to `notes`
-- after the fact and a re-run migration must not erase them. `derived_from` is
-- set separately, and only where it is still NULL, for the same reason.

ALTER TABLE instruments
    ADD COLUMN derived_from TEXT REFERENCES instruments(code) ON DELETE RESTRICT,
    ADD CONSTRAINT instruments_derived_from_not_self
        CHECK (derived_from IS NULL OR derived_from <> code);

INSERT INTO instruments
  (code, kind, name_en, name_fa, domain, quote_currency, unit, decimals,
   calendar_class, quality_tier, is_proxy, is_derived, derived_from, notes)
VALUES
  ('IR_SILVER_999','market_price','Silver 999 (gram, Tehran)','نقره ۹۹۹ (گرم)','silver','IRT','gram',0,
     'tehran_bazaar','commercial',FALSE,FALSE,NULL,
     'TGJU silver_999: the Tehran price of one gram of 999 silver, published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. TGJU is the only source (history from 2020-10-31), so nothing corroborates it. Its table holds three junk closes (8,000 rial on 2021-08-10, 8,200 on 2021-08-29, 2,008,800 on 2022-09-30); any close more than x1.6 from the median of its seven neighbours either side is held as suspect in raw_observations and not stored. It is not XAGUSD: measured against XAGUSD x USD_IRT / 31.1035 it carried a median premium of +4.3% (1st/99th percentile -6.3% / +34%, and +46% to +52% in January-February 2026), and both of those series are proxies in their own right (COMEX futures; the USDT/toman market).'),
  ('IR_COIN_BAHAR','market_price','Bahar Azadi gold coin','سکه بهار آزادی','gold','IRT','coin',0,
     'tehran_bazaar','official_mirror',FALSE,FALSE,NULL,
     'TGJU sekeb: the Tehran bazaar price of one full Bahar Azadi coin (old design), published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. History from 2013-07-22. Priced per COIN, including the coin''s premium over its gold content; it is not a gold-per-gram price.'),
  ('IR_COIN_HALF','market_price','Half Azadi coin','نیم سکه','gold','IRT','coin',0,
     'tehran_bazaar','official_mirror',FALSE,FALSE,NULL,
     'TGJU nim: the Tehran bazaar price of one half Azadi coin, published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. History from 2013-07-22. Priced per COIN, including its premium over its gold content, which is proportionally larger on the smaller coins.'),
  ('IR_COIN_QUARTER','market_price','Quarter Azadi coin','ربع سکه','gold','IRT','coin',0,
     'tehran_bazaar','official_mirror',FALSE,FALSE,NULL,
     'TGJU rob: the Tehran bazaar price of one quarter Azadi coin, published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. History from 2013-07-22. Priced per COIN, including its premium over its gold content, which is proportionally larger on the smaller coins.'),
  ('IR_COIN_GERAMI','market_price','Gram gold coin','سکه گرمی','gold','IRT','coin',0,
     'tehran_bazaar','official_mirror',FALSE,FALSE,NULL,
     'TGJU gerami: the Tehran bazaar price of one gram coin, published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. History from 2013-07-22. Priced per COIN, including its premium over its gold content; it is not the price of a gram of gold.'),
  ('IR_GOLD_24K','market_price','24k gold (gram)','طلای ۲۴ عیار','gold','IRT','gram',0,
     'tehran_bazaar','official_mirror',FALSE,FALSE,'IR_GOLD_18K',
     'TGJU geram24, published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. History from 2014-05-02. TGJU DERIVES it from its 18k gram price (x 4/3; median ratio 1.33332 over 2,963 days), so measured against 18k gold it is constant by construction: its return in grams of 18k gold is withheld, and any gap against IR_GOLD_18K reflects the difference between TGJU''s 18k and the live 18k feed, not the market.'),
  ('IR_GOLD_MESGHAL','market_price','Melted gold (mesghal)','مثقال طلا (آبشده)','gold','IRT','mesghal',0,
     'tehran_bazaar','official_mirror',FALSE,FALSE,'IR_GOLD_18K',
     'TGJU mesghal: melted (آبشده) gold priced per MESGHAL -- 4.6083 g at 705 per mille -- published in rials and stored as toman (/10). One daily settled close per day, stamped 23:00 UTC on its own date and written the next morning -- not a live quote. History from 2013-07-22. TGJU DERIVES it from its 18k gram price (x 4.3318; median ratio 4.3316), so measured against 18k gold it is constant by construction: its return in grams of 18k gold is withheld, and any gap against IR_GOLD_18K reflects the difference between TGJU''s 18k and the live 18k feed, not the market. Never compare it per gram without that conversion.')
ON CONFLICT (code) DO NOTHING;

-- A pre-existing 24k or mesghal row keeps its own notes (ON CONFLICT above) but
-- still gets the relationship: without it, the performance table would publish
-- a return in grams of 18k gold that is a definitional constant.
UPDATE instruments
SET derived_from = 'IR_GOLD_18K',
    updated_at = now()
WHERE code IN ('IR_GOLD_24K', 'IR_GOLD_MESGHAL')
  AND derived_from IS NULL;
