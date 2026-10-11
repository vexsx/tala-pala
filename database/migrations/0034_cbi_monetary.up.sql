-- 0034: The Central Bank of Iran's monetary aggregates — base money (پایه
-- پولی), liquidity (نقدینگی, M2), money (پول, M1) and quasi-money (شبه پول).
--
-- WHY THIS EXISTS
--
-- The Money & liquidity page shows these four as monthly end-of-month stocks
-- with their publication dates, and the dollar scenarios are paced by M2's
-- growth. Nothing in this system held them: 0024 built the bitemporal store
-- for exactly this kind of series (its header measured the CBI bulletin's
-- 5.5-month lag) and 0027 registered the first Iranian publisher, SCI. This
-- registers the second, and adds the three facts about a CBI number that the
-- existing columns cannot state.
--
-- WHERE THE NUMBERS COME FROM (measured 2026-10-11)
--
--   Monthly  گزیده آمارهای اقتصادی, بخش پولی و بانکی — https://www.cbi.ir/
--            category/2692.aspx → year page → month page → «فرمت Excel» →
--            a legacy .xls (OLE2). 69 Excel editions, Tir 1399 → Esfand 1404;
--            before Tir 1399 the editions are PDF only. The first sheet
--            («متغیرهای عمده», «متغیرهای پولی» or «Sheet1») prints each
--            aggregate at three month-ends: the same month last year, last
--            Esfand, and the current month (an Esfand edition: Esfand Y-2,
--            Y-1, Y) — so the editions together give every month from Tir
--            1398, plus Esfand 1397. Unit هزار میلیارد ریال (1e12 rial),
--            stored exactly as printed. Esfand 1404: base money 21,959.3,
--            M2 155,812.2. Read by app/economic/cbi.py on the production host
--            (www.cbi.ir answers there; ~6-20 s a request because the host's
--            resolver times out first), daily, by the Go job `cbi-monetary`.
--   History  tsdview.cis.cbi.ir, the CBI's time-series viewer: quarter-end
--            months 1352 → Azar 1404 from an unauthenticated JSON API that
--            answers from outside Iran and NOT from the production host (the
--            connection times out). scripts/cbi_history_fetch.py fetches it
--            off-server, copies it in and posts it to
--            /internal/economic/cbi-history, the sci_fetch.py pattern. Only
--            quarters no Excel edition can carry (before Esfand 1397, and
--            Khordad 1398) are written; where the two overlap they agree to
--            the last printed digit (M1 and quasi-money at all 27 quarter-ends
--            the captured editions cover, M2 at 26 and within 0.1 at Azar
--            1404).
--
-- WHAT THE EXISTING COLUMNS COULD NOT SAY
--
-- 1. source_documents.edition_label. A CBI workbook is an EDITION of a
--    monthly publication ('1404-12'), and the ingest must refuse an older
--    edition arriving after a newer one: available_at is when THIS system saw
--    the file, so an old workbook ingested today would pass every other guard
--    and write its older values as the newest vintage. The filename cannot
--    carry it (pol9904.xls, pol00.1.xls, pol1401,12.xls, 140412.xls) and CBI
--    replaces files in place (every 1403/01-06 workbook was re-saved
--    2024-12-30), so the label is read from the workbook's own title and
--    recorded on its archive row. The time-series viewer's payload is
--    labelled 'tsdview'.
--
-- 2. source_documents.document_saved_at. CBI states no publication date. The
--    only date the document carries about itself is the OLE "last saved"
--    timestamp (140412.xls: 2026-06-21 05:41:31 UTC, the day the CBI news
--    item announcing it was posted). It is a lower bound on when these bytes
--    were published, it becomes published_at, and it is the base of the
--    backfill's reconstructed availability (point 4).
--
-- 3. economic_observations.method. CBI redefined base money — CBI's foreign
--    assets revalued in 1402 and the components reclassified to the IMF's
--    MFSMCG 2016 — for months from Esfand 1399 / 1400 on (the footnote says
--    both, in different editions). Base money at Esfand 1400 was 6,039.7 in
--    the editions of 1400-1401 and is 5,981.1 in the Esfand 1402 edition;
--    at Esfand 1401, 8,599.9 → 8,548.0. Growth across the two definitions is
--    not growth, so every base-money row says which one it is ('old' /
--    'revised') and readers never compute growth across them. NULL for every
--    series whose source states no method — every other row in this table.
--
-- 4. economic_observations.available_at_approx. available_at is when THIS
--    system could first have known a value. For the 69 editions published
--    before this system read the CBI, that answer would be "the day of the
--    backfill" — true, and useless to a backtest. So the backfill
--    RECONSTRUCTS it as the workbook's last-saved time plus one day (never
--    earlier than the edition ingested before it, so availability stays
--    monotone across editions), and this flag says so on every row it wrote.
--    A live run stamps the moment it fetched the file and leaves it FALSE.
--
-- WHY priority 40, category 'iran_macro'
--
-- Same reasoning as 0027's SCI row: no collect job consults 'iran_macro', and
-- the CBI ingest addresses this provider by code. 40 only keeps the ordering
-- readable after SCI's 30.
--
-- Separate ALTER statements, one column each, so scripts/check_migrations.py
-- and tests/test_migration_hygiene.py see every added column and can hold the
-- Python mirror (app/db.py) to it.

INSERT INTO data_providers (code, name, base_url, category, priority, enabled) VALUES
  ('cbi','Central Bank of Iran (Selected Economic Indicators, monetary)','https://www.cbi.ir','iran_macro',40,TRUE)
ON CONFLICT (code) DO NOTHING;

ALTER TABLE source_documents ADD COLUMN edition_label TEXT NOT NULL DEFAULT '';
ALTER TABLE source_documents ADD COLUMN document_saved_at TIMESTAMPTZ;

ALTER TABLE economic_observations ADD COLUMN method TEXT;
ALTER TABLE economic_observations ADD COLUMN available_at_approx BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE economic_observations ADD CONSTRAINT economic_obs_method
    CHECK (method IS NULL OR method IN ('old','revised'));
