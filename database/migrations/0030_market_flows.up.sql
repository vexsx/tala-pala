-- 0030: the whole market's money flow, share by share, and the commodity
-- funds' settled closes.
--
-- 0029 stored the حقیقی/حقوقی money flow of twenty companies and said so on
-- every surface ("the roster, not the market"). The question the product has
-- to answer — which industries and which shares money is moving between — is
-- a question about every listed share, so this migration makes the market the
-- unit: a mirror of TSETMC's share listing, the flow of every share in it, the
-- exchange's own per-session value for each, and the sector vocabulary those
-- roll up into. Fed exactly like 0028/0029: fetched OFF the server by
-- scripts/tsetmc_fetch.py, copied in over SSH, ingested inside the network.
--
-- WHY A MIRROR AND NOT MORE ROWS IN equity_instruments
--
-- equity_instruments is the curated roster: bars, corporate actions, the
-- adjustment gate, /stocks/{symbol}, the screener, alerts and relative value
-- all read it. Enrolling ~1,160 shares there would enroll them in all of that
-- (the fetch alone would pull ~1,000 bar histories of up to 1.4 MB each), and
-- its UNIQUE symbol cannot hold a listing that re-uses a retired symbol. So
-- `market_shares` is a separate table and deliberately a MIRROR, the one
-- exception to "never create a registry row from a payload": each market-watch
-- ingest inserts what TSETMC lists, updates what it renamed or re-sectored, and
-- marks what it no longer lists `listed = FALSE`. Nothing here is ever deleted
-- — the flow history of a delisted share is still true — which is why the
-- foreign keys into it are ON DELETE RESTRICT rather than 0029's CASCADE: on a
-- table whose membership churns, a cascade is a way to lose history silently.
--
-- WHAT A SHARE IS, MEASURED ON GetMarketWatch (2026-09-29, 3,786 rows)
--
--   insID prefix  market      rows   what it is
--   IRO1          bourse       648   shares
--   IRO3          farabourse   354   shares
--   IRO7          paye         150   پایه (the Farabourse's base market)
--   IRO5          sme           10   shares on the Farabourse's نوآفرین (growth)
--                                    market: GetInstrumentInfo on خگلپا
--                                    (19492025284339283, IRO5GLPA0001) gives
--                                    flowTitle "بازار فرابورس", cgrValCotTitle
--                                    "بازار نوآفرین - رشد", 1.8 billion shares
--                                    outstanding and sector 34 — a company.
--   everything else (IRO9/IROF options, IRB* bonds, IRT*/IRTK funds, IRR*
--   rights, IRO4 futures, IRO6 housing facilities) is NOT a share. Their `csv`
--   carries the UNDERLYING's or the issuer's sector (an option on a bank is
--   sector 57), so letting one in would put its turnover into that sector.
--
-- The classification is by insID and never by yVal/flow/cGrValCot: two
-- responses to the same URL minutes apart differed, and from 10:20 UTC onward
-- five in a row carried none of those three fields. insID is NOT the ISIN
-- (فولاد: insID IRO1FOLD0001, cIsin IRO1FOLD0009), but its first eight
-- characters name the company and its last four the board:
--
--   0001  main       737 rows, 95.4% of the shares' traded value
--   0002  block       21 rows (بلوک; وبملت2 = IRO1BMLT0002)
--   0003  secondary  393 rows (فولاد3 = IRO1FOLD0003, its own order book and
--                    its own flow history from 2026-08-08)
--   else  other        1 row  (چکاپا, IRO1KAPA0011)
--
-- Every board is its own insCode with its own trades, so every board is
-- stored; `company_code` (insID[:8]) is what rolls a company's boards up
-- without double-counting its shares outstanding.
--
-- THE FLOOR, AND WHY THE ROSTER IS EXEMPT FROM IT
--
-- A money-flow row costs ~221 bytes with its index (0029's 57,913 rows are
-- 12 MB). Every share's whole history would be ~1.5-2M rows, ~0.4-0.5 GB, for
-- a 321 MB database. So a share outside the roster ships flows and sessions
-- only from 2025-03-21 (1 Farvardin 1404). TEDPIX has 305 distinct sessions
-- from there to 2026-09-28 (363 rows, 58 of them closure repeats) and a share
-- trades on fewer (فولاد: 246), so that is ~1,150 x ~250 ~ 300k flow rows,
-- ~65 MB, and as many session rows; the roster keeps the full history it has.
-- The floor is enforced by the fetch, which is the thing that would spend the
-- disk; this schema stores whatever it is given.
--
-- THE EXCHANGE'S OWN SESSION ROW, FOR SHARES THAT HAVE NO BARS
--
-- 0029's flow check compares a session's flow total with its bar, and only the
-- roster has bars. GetInstrmentsHistoryInDay/{yyyymmdd} gives the whole market's
-- closing price, reference price, value, volume and trade count for one
-- session in ~180 KB (gzipped), which is what `market_share_sessions` keeps.
-- Measured, and each point decides something below:
--
--   * It lists TRADED instruments only (2,290 on 2026-09-28; a Friday gives an
--     empty list), so a missing row means "did not trade", never zero.
--   * `dEven` is 0 on every row: the file does not say which session it
--     describes. The date is the one the fetch requested, carried in the file
--     name, and the ingest cross-checks it against the stored flows before it
--     believes it.
--   * `insCode` is a bare JSON NUMBER there, and 2,012 of 2,290 exceed 2^53 —
--     a float64 (or JavaScript) parse silently corrupts it. TEXT, like every
--     insCode in this schema.
--   * Its value equals the flow history's buy total EXACTLY for all eight
--     instruments compared on 2026-09-28 (bourse, Farabourse, پایه, a
--     secondary board, a fund): the same identity 0029 checks against a bar.
--   * Its priceYesterday is TSETMC's corporate-action-ADJUSTED reference, not
--     the previous close: on فولاد's ex-dates it is 3,982 on 2025-03-12
--     (previous close 5,530) and 4,800 on 2024-07-22 (previous close 5,200),
--     identical to GetClosingPriceDailyList's. So close / price_yesterday is a
--     session return that a capital increase does not break.
--
-- THE COMMODITY FUNDS
--
-- `instruments` already carries three gold funds whose only prices so far are
-- BrsApi's intraday mirror of TSETMC's last trade (tse_funds, since
-- 2026-07-21). GetClosingPriceDailyList/{insCode}/0 gives every fund's whole
-- settled history, and TSETMC's own GetInstrumentIdentity classifies each one:
--
--   عیار   34144395039913458  IRTKMOFD0001  6822 صندوق کالایی مبتنی بر طلا   from 2018-06-02
--   طلا    46700660505281786  IRTKLOTF0001  6822 صندوق کالایی مبتنی بر طلا   from 2017-06-10
--   کهربا  25559236668122210  IRTKROBA0001  6822 صندوق کالایی مبتنی بر طلا   from 2021-07-06
--   سیلور  18156575395080321  IRTKSILV0001  6823 صندوق کالایی مبتنی بر نقره  from 2025-12-20
--   سیمین  33761569293467411  IRTKSIMI0001  6823 صندوق کالایی مبتنی بر نقره  from 2025-12-20
--
-- Every one lists at a 10,000-rial unit. Across all five, 5,846 consecutive
-- session pairs, priceYesterday equals the previous closing price on all but
-- two, and those two differ by exactly one rial (rounding): no split and no
-- restated unit, so the closes are comparable across the whole history
-- unadjusted. That is true of the copy TSETMC's CDN served for that count:
-- the other copy it serves (measured the same day, five minutes apart) lacks
-- the 2023-03-27 session, so there AYAR's, TALA's and KAHRABA's 2023-03-28
-- reference is the missing session's close — a gap in the copy, not a
-- restatement, and app/bourse/funds.py judges it as one (against TEDPIX's
-- sessions). The 190 zero-trade rows are carry-forward fillers
-- (pClosing = priceYesterday on all 190, on the same closure windows as
-- TGJU's 2026 gaps) and are not prices of anything. Funds trade until 18:00
-- Tehran (their newest rows are stamped hEven 175959 and 180000), later than
-- the shares' 12:30 close.
--
-- NOT SEEDED, DELIBERATELY: سافرون (51200575796028449, IRTKSAHR0001). Its
-- ticker reads "saffron", but TSETMC's own data never says so: its name is
-- "صندوق س.کالای گنجینه زمین", its Latin name "SaharKhiz", its sub-sector 6824
-- "صندوق کالایی کشاورزی" (an AGRICULTURAL commodity fund, where the five above
-- are explicitly gold- or silver-based) and its board Q1 "بورس کالا" rather
-- than the QS "صندوق های کالایی" of the other five. Recording its underlying
-- as saffron would be this system's inference presented as the exchange's
-- statement, so it is left out; the registry is data, and adding it is an
-- INSERT once its underlying is established from a source that states it.

-- ------------------------------------------------------------ market_sectors
--
-- TSETMC's industrial groups (StaticData/GetStaticData, type IndustrialGroup:
-- 66 codes, which cover 52 of the 53 `csv` values in the market watch). The
-- Persian names are TSETMC's own, whitespace-stripped and with Arabic kaf/yeh
-- folded to Persian (app.equities.adjust.fold_name), and every market-watch
-- ingest refreshes them. The English names are this system's translations,
-- not a claim that the exchange publishes them. Code 84 appears on one share
-- (مهرمام, IRO5MOMS0001) and in no TSETMC list, so both of its names are left
-- empty rather than invented.
CREATE TABLE market_sectors (
    sector_code TEXT PRIMARY KEY CHECK (sector_code ~ '^[0-9]{2}$'),
    name_fa     TEXT NOT NULL DEFAULT '',
    name_en     TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO market_sectors (sector_code, name_fa, name_en) VALUES
  ('01','زراعت و خدمات وابسته','Agriculture and related services'),
  ('02','جنگلداری و ماهیگیری','Forestry and fishing'),
  ('10','استخراج زغال سنگ','Coal mining'),
  ('11','استخراج نفت گاز و خدمات جنبی جز اکتشاف','Oil and gas extraction and related services'),
  ('13','استخراج کانه های فلزی','Metal ore mining'),
  ('14','استخراج سایر معادن','Other mining'),
  ('15','حذف شده- فرآورده‌های غذایی و آشامیدنی','Food and beverages (retired code)'),
  ('17','منسوجات','Textiles'),
  ('19','دباغی، پرداخت چرم و ساخت انواع پاپوش','Leather and footwear'),
  ('20','محصولات چوبی','Wood products'),
  ('21','محصولات کاغذی','Paper products'),
  ('22','انتشار، چاپ و تکثیر','Publishing and printing'),
  ('23','فراورده های نفتی، کک و سوخت هسته ای','Refined petroleum, coke and nuclear fuel'),
  ('24','حذف شده-مواد و محصولات شیمیایی','Chemicals (retired code)'),
  ('25','لاستیک و پلاستیک','Rubber and plastics'),
  ('26','تولید محصولات کامپیوتری الکترونیکی ونوری','Computer, electronic and optical products'),
  ('27','فلزات اساسی','Basic metals'),
  ('28','ساخت محصولات فلزی','Fabricated metal products'),
  ('29','ماشین آلات و تجهیزات','Machinery and equipment'),
  ('31','ماشین آلات و دستگاه‌های برقی','Electrical machinery'),
  ('32','ساخت دستگاه‌ها و وسایل ارتباطی','Communication equipment'),
  ('33','ابزارپزشکی، اپتیکی و اندازه‌گیری','Medical, optical and measuring instruments'),
  ('34','خودرو و ساخت قطعات','Automobiles and parts'),
  ('35','سایر تجهیزات حمل و نقل','Other transport equipment'),
  ('36','مبلمان و مصنوعات دیگر','Furniture and other manufacturing'),
  ('38','قند و شکر','Sugar'),
  ('39','شرکتهای چند رشته ای صنعتی','Diversified industrials'),
  ('40','عرضه برق، گاز، بخاروآب گرم','Electricity, gas and steam'),
  ('41','جمع آوری، تصفیه و توزیع آب','Water supply'),
  ('42','محصولات غذایی و آشامیدنی به جز قند و شکر','Food and beverages (excl. sugar)'),
  ('43','مواد و محصولات دارویی','Pharmaceuticals'),
  ('44','محصولات شیمیایی','Chemicals'),
  ('45','پیمانکاری صنعتی','Industrial contracting'),
  ('46','تجارت عمده فروشی به جز وسایل نقلیه موتور','Wholesale trade'),
  ('47','خرده فروشی،باستثنای وسایل نقلیه موتوری','Retail trade'),
  ('49','کاشی و سرامیک','Tiles and ceramics'),
  ('50','تجارت عمده وخرده فروشی وسائط نقلیه موتور','Motor vehicle trade'),
  ('51','حمل و نقل هوایی','Air transport'),
  ('52','انبارداری و حمایت از فعالیتهای حمل و نقل','Storage and transport support'),
  ('53','سیمان، آهک و گچ','Cement, lime and plaster'),
  ('54','سایر محصولات کانی غیرفلزی','Other non-metallic minerals'),
  ('55','هتل و رستوران','Hotels and restaurants'),
  ('56','سرمایه گذاریها','Investment companies'),
  ('57','بانکها و موسسات اعتباری','Banks and credit institutions'),
  ('58','سایر واسطه گریهای مالی','Other financial intermediation'),
  ('59','اوراق حق تقدم استفاده از تسهیلات مسکن','Housing facility rights'),
  ('60','حمل ونقل، انبارداری و ارتباطات','Transport, storage and communications'),
  ('61','حمل و نقل آبی','Water transport'),
  ('63','فعالیت های پشتیبانی و کمکی حمل و نقل','Transport support services'),
  ('64','مخابرات','Telecommunications'),
  ('65','واسطه‌گری‌های مالی و پولی','Monetary intermediation'),
  ('66','بیمه وصندوق بازنشستگی به جزتامین اجتماعی','Insurance and pension funds'),
  ('67','فعالیتهای کمکی به نهادهای مالی واسط','Auxiliary financial activities'),
  ('68','صندوق سرمایه گذاری قابل معامله','Exchange-traded funds'),
  ('69','اوراق تامین مالی','Financing bonds'),
  ('70','انبوه سازی، املاک و مستغلات','Construction and real estate'),
  ('71','فعالیت مهندسی، تجزیه، تحلیل و آزمایش فنی','Engineering and technical testing'),
  ('72','رایانه و فعالیت‌های وابسته به آن','Computer services'),
  ('73','اطلاعات و ارتباطات','Information and communications'),
  ('74','خدمات فنی و مهندسی','Technical and engineering services'),
  ('76','اوراق بهادار مبتنی بر دارایی فکری','Intellectual-property securities'),
  ('77','فعالبت های اجاره و لیزینگ','Leasing'),
  ('82','فعالیت پشتیبانی اجرائی اداری وحمایت کسب','Administrative support'),
  ('84','',''),
  ('90','فعالیت های هنری، سرگرمی و خلاقانه','Arts and entertainment'),
  ('93','فعالیتهای فرهنگی و ورزشی','Cultural and sports activities'),
  ('98','گروه اوراق غیرفعال','Inactive securities')
ON CONFLICT (sector_code) DO NOTHING;

-- ------------------------------------------------------------- market_shares
CREATE TABLE market_shares (
    ins_code        TEXT PRIMARY KEY CHECK (ins_code ~ '^[0-9]{1,20}$'),
    -- The market-watch insID (IRO1FOLD0001). NOT the ISIN; see the header.
    ins_id          TEXT NOT NULL DEFAULT '',
    -- fold_symbol(lva). Deliberately NOT unique: boards share a root and
    -- symbols are re-used across listings.
    symbol_fa       TEXT NOT NULL,
    name_fa         TEXT NOT NULL DEFAULT '',     -- fold_name(lvc)
    market          TEXT NOT NULL CHECK (market IN ('bourse','farabourse','paye','sme')),
    board           TEXT NOT NULL CHECK (board IN ('main','block','secondary','other')),
    company_code    TEXT NOT NULL DEFAULT '',     -- insID[:8]
    -- csv.strip(): TSETMC's classification at the LAST ingested market watch,
    -- which a reader applying it to history must say it is doing.
    sector_code     TEXT NOT NULL DEFAULT '',
    -- Present in the newest market watch ingested. A row is never deleted.
    listed          BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Coverage, maintained by the ingest from the TABLES, never from a payload.
    flow_first_date DATE,
    flow_last_date  DATE,
    flow_count      INT NOT NULL DEFAULT 0,
    session_last_date DATE,
    session_count   INT NOT NULL DEFAULT 0,
    -- Moves whenever a row's content, listing or coverage changes, and only
    -- then: a read that caches on it re-reads when the data did.
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_market_shares_sector  ON market_shares (sector_code);
CREATE INDEX idx_market_shares_company ON market_shares (company_code);

-- The roster must exist in the universe before the foreign key can move onto
-- it. All twenty roster rows are main-board shares, and for every one of them
-- the market watch's insID and the seeded ISIN agree on their first eight
-- characters (measured 2026-09-29), so the company key is known now; the insID
-- itself is not derivable from an ISIN and waits for the first market watch,
-- as does `listed`. Coverage is taken from the rows 0029 already stored.
INSERT INTO market_shares
  (ins_code, ins_id, symbol_fa, name_fa, market, board, company_code, sector_code, listed)
SELECT ins_code, '', symbol_fa, name_fa, market, 'main',
       CASE WHEN isin ~ '^IR[A-Z0-9]{10}$' THEN left(isin, 8) ELSE '' END,
       sector_code, FALSE
FROM equity_instruments
ON CONFLICT (ins_code) DO NOTHING;

UPDATE market_shares m
SET flow_first_date = c.first_date, flow_last_date = c.last_date, flow_count = c.n
FROM (SELECT ins_code, min(trade_date) AS first_date, max(trade_date) AS last_date,
             count(*) AS n
      FROM equity_client_flows GROUP BY ins_code) c
WHERE m.ins_code = c.ins_code;

-- The name is Postgres's default for 0029's inline REFERENCES, and it is
-- named exactly rather than with IF EXISTS: a database where it differs must
-- fail this migration, not end up with the old CASCADE key still in place.
ALTER TABLE equity_client_flows DROP CONSTRAINT equity_client_flows_ins_code_fkey;
ALTER TABLE equity_client_flows ADD CONSTRAINT equity_client_flows_ins_code_fkey
    FOREIGN KEY (ins_code) REFERENCES market_shares(ins_code) ON DELETE RESTRICT;
-- The primary key serves one share's history; a market window scans a date.
CREATE INDEX idx_equity_client_flows_date ON equity_client_flows (trade_date);

-- ----------------------------------------------------- market_share_sessions
--
-- One row per share per TRADED session, exactly as GetInstrmentsHistoryInDay
-- served it, in RIALS. Never overwritten: a file that disagrees with a stored
-- row fails that whole day and names the shares.
CREATE TABLE market_share_sessions (
    ins_code        TEXT NOT NULL REFERENCES market_shares(ins_code) ON DELETE RESTRICT,
    trade_date      DATE NOT NULL,               -- the requested session, see the header
    close           NUMERIC NOT NULL CHECK (close > 0),            -- pClosing, the official close
    last_trade      NUMERIC CHECK (last_trade > 0),                -- pDrCotVal; NULL when not served
    -- TSETMC's reference for the session, ADJUSTED on an ex-date (header).
    price_yesterday NUMERIC NOT NULL CHECK (price_yesterday > 0),
    value           NUMERIC NOT NULL CHECK (value >= 0),           -- qTotCap
    volume          NUMERIC NOT NULL CHECK (volume >= 0),          -- qTotTran5J
    trades          BIGINT  NOT NULL CHECK (trades >= 0),          -- zTotTran
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_share_sessions_pkey PRIMARY KEY (ins_code, trade_date)
);
CREATE INDEX idx_market_share_sessions_date ON market_share_sessions (trade_date);

-- Which whole-market day files were ingested, so the fetch asks only for the
-- sessions it does not have and a reader can tell "no row" from "no file".
CREATE TABLE market_session_files (
    trade_date  DATE PRIMARY KEY,
    rows_total  INT NOT NULL CHECK (rows_total >= 0),    -- every instrument in TSETMC's file
    rows_shares INT NOT NULL CHECK (rows_shares >= 0),   -- those matched to market_shares
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------- commodity funds
--
-- The two silver funds join the registry beside the three gold ones. Their
-- prices are the settled closes the fund ingest writes into `prices`
-- (source 'tsetmc_cdn'); they are not collected live.
INSERT INTO instruments
  (code, kind, name_en, name_fa, domain, quote_currency, unit, decimals, calendar_class,
   quality_tier, is_proxy, is_derived, notes)
VALUES
  ('IR_SILVER_FUND_SILVER','market_price','Nova silver fund (Silver)','صندوق سیلور','fund','IRT','unit',0,
     'tse_session','official_mirror',FALSE,FALSE,
     'The exchange closing price of one unit of سیلور (صندوق س.بازده نقره نوا), a commodity fund traded on the Iran Mercantile Exchange that TSETMC classifies as silver-based (صندوق کالایی مبتنی بر نقره); its holdings are set by its prospectus and are not tracked here. It is the price units changed hands at, NOT the fund''s NAV: it carries the fund''s premium or discount to NAV and its costs. Settled daily closes only (TSETMC pClosing / 10, toman), fetched off-server by scripts/tsetmc_fetch.py and stamped 23:00 UTC on the session date; zero-trade carry-forward days are not stored. Listed 2025-12-20 at 10,000 rials, first traded 2025-12-22; not collected live.'),
  ('IR_SILVER_FUND_SIMIN','market_price','Simin silver fund','صندوق سیمین','fund','IRT','unit',0,
     'tse_session','official_mirror',FALSE,FALSE,
     'The exchange closing price of one unit of سیمین (صندوق س.نقره سیمین هوبر), a commodity fund traded on the Iran Mercantile Exchange that TSETMC classifies as silver-based (صندوق کالایی مبتنی بر نقره); its holdings are set by its prospectus and are not tracked here. It is the price units changed hands at, NOT the fund''s NAV: it carries the fund''s premium or discount to NAV and its costs. Settled daily closes only (TSETMC pClosing / 10, toman), fetched off-server by scripts/tsetmc_fetch.py and stamped 23:00 UTC on the session date; zero-trade carry-forward days are not stored. Listed 2025-12-20 at 10,000 rials, first traded 2025-12-22; not collected live.')
ON CONFLICT (code) DO NOTHING;

-- The gold funds' notes said nothing about what their price is, and
-- KAHRABA's said it had no observations — which the fund ingest makes false.
-- The down migration restores 0024's text.
UPDATE instruments SET notes =
  'The exchange price of one unit of عیار (صندوق طلای عیار مفید), a commodity fund traded on the Iran Mercantile Exchange that TSETMC classifies as gold-based (صندوق کالایی مبتنی بر طلا); its holdings are set by its prospectus and are not tracked here. NOT the fund''s NAV: the price carries its premium or discount to NAV and its costs. Intraday values are BrsApi''s mirror of TSETMC''s last trade (tse_funds); settled daily closes (TSETMC pClosing / 10, toman, source tsetmc_cdn, from its first traded session, 2018-06-09) are fetched off-server by scripts/tsetmc_fetch.py and stamped 23:00 UTC on the session date.'
WHERE code = 'IR_GOLD_FUND_AYAR';
UPDATE instruments SET notes =
  'The exchange price of one unit of طلا (صندوق س. کالای پارسیان), a commodity fund traded on the Iran Mercantile Exchange that TSETMC classifies as gold-based (صندوق کالایی مبتنی بر طلا); its holdings are set by its prospectus and are not tracked here. NOT the fund''s NAV: the price carries its premium or discount to NAV and its costs. Intraday values are BrsApi''s mirror of TSETMC''s last trade (tse_funds); settled daily closes (TSETMC pClosing / 10, toman, source tsetmc_cdn, from its first traded session, 2017-06-10) are fetched off-server by scripts/tsetmc_fetch.py and stamped 23:00 UTC on the session date.'
WHERE code = 'IR_GOLD_FUND_TALA';
UPDATE instruments SET notes =
  'The exchange closing price of one unit of کهربا (صندوق س. کالای کهربا), a commodity fund traded on the Iran Mercantile Exchange that TSETMC classifies as gold-based (صندوق کالایی مبتنی بر طلا); its holdings are set by its prospectus and are not tracked here. NOT the fund''s NAV: the price carries its premium or discount to NAV and its costs. Settled daily closes only (TSETMC pClosing / 10, toman, source tsetmc_cdn, from its first traded session, 2021-07-10), fetched off-server by scripts/tsetmc_fetch.py and stamped 23:00 UTC on the session date; not collected live unless TSETMC_FUNDS names it.'
WHERE code = 'IR_GOLD_FUND_KAHRABA';

-- The IME funds whose settled closes go into `prices`. The fetch reads this
-- table (GET /internal/bourse/funds/roster), so adding a fund is an INSERT
-- here and one in `instruments`.
CREATE TABLE commodity_funds (
    ins_code        TEXT PRIMARY KEY CHECK (ins_code ~ '^[0-9]{1,20}$'),
    instrument_code TEXT NOT NULL UNIQUE REFERENCES instruments(code) ON DELETE RESTRICT,
    symbol_fa       TEXT NOT NULL,                 -- fold_symbol(lVal18AFC)
    name_fa         TEXT NOT NULL DEFAULT '',      -- fold_name(lVal30)
    -- What TSETMC's own sub-sector says backs it (6822 gold, 6823 silver).
    underlying      TEXT NOT NULL CHECK (underlying IN ('gold','silver','saffron')),
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    -- Coverage of its tsetmc_cdn closes in `prices`, from the table.
    first_close     DATE,
    last_close      DATE,
    close_count     INT NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO commodity_funds (ins_code, instrument_code, symbol_fa, name_fa, underlying, notes) VALUES
  ('34144395039913458','IR_GOLD_FUND_AYAR','عیار','صندوق طلای عیار مفید','gold',
     'IRTKMOFD0001; 1,998 daily rows from 2018-06-02 on 2026-09-29, 48 of them zero-trade carries.'),
  ('46700660505281786','IR_GOLD_FUND_TALA','طلا','صندوق س. کالای پارسیان','gold',
     'IRTKLOTF0001; 2,237 daily rows from 2017-06-10 on 2026-09-29, 58 of them zero-trade carries. The oldest fund here, and the highest unit price (1,749,293 rials on 2026-09-28): a level, not a split: no reference restates beyond a rial, except across the 2023-03-27 session one of TSETMC''s two CDN copies omits.'),
  ('25559236668122210','IR_GOLD_FUND_KAHRABA','کهربا','صندوق س. کالای کهربا','gold',
     'IRTKROBA0001; 1,250 daily rows from 2021-07-06 on 2026-09-29, 34 of them zero-trade carries.'),
  ('18156575395080321','IR_SILVER_FUND_SILVER','سیلور','صندوق س.بازده نقره نوا','silver',
     'IRTKSILV0001; 183 daily rows from 2025-12-20 on 2026-09-29, 25 of them zero-trade carries (2026-01-21..02-01 and 2026-02-28..03-14).'),
  ('33761569293467411','IR_SILVER_FUND_SIMIN','سیمین','صندوق س.نقره سیمین هوبر','silver',
     'IRTKSIMI0001; 183 daily rows from 2025-12-20 on 2026-09-29, 25 of them zero-trade carries.')
ON CONFLICT (ins_code) DO NOTHING;

-- ------------------------------------------------------ 0029's index notes
-- TSETMC's two copies, in the index notes 0029 wrote.
--
-- Fetched five minutes apart on 2026-09-29, the CDN answered two copies of the
-- index histories. In one, the second market and the price index of 50
-- companies carry the decimal-shift stretches 0029's notes describe; in the
-- other they do not (10,060,226.2 where the first prints 1,006,020.0 on
-- 2026-08-16; 142 dates of the price-50 index a power of ten apart). Either
-- copy is stored corrected by scale_exp, and a later copy a power of ten away
-- is accepted as the same value, so the chart is the same whichever was
-- ingested first; the notes stop claiming the stretch is simply what TSETMC
-- stores. Only a note still reading as 0029 wrote it is changed.
UPDATE market_indices SET notes =
  'One of the two copies of this history TSETMC''s CDN serves stores it at one tenth of its value from 2026-08-16 onward (history 1,315,840 against a live 13,310,356 on 2026-09-29); the other does not. Whichever copy was ingested, the values are corrected by scale_exp, and the other copy is accepted as the same values a power of ten apart.'
WHERE ins_code = '71704845530629737'
  AND notes = 'Stored at one tenth of its value from 2026-08-16 onward (history 1,315,840 against a live 13,310,356 on 2026-09-29); corrected by scale_exp.';
UPDATE market_indices SET notes =
  'One of the two copies of this history TSETMC''s CDN serves carries 9 decimal-shift steps, most of them in January-February 2026 alternating between scales day to day; the other does not (142 dates a power of ten apart, measured 2026-09-29). Whichever copy was ingested, the values are corrected by scale_exp.'
WHERE ins_code = '69932667409721265'
  AND notes = 'Carries 9 decimal-shift steps, most of them in January-February 2026 alternating between scales day to day; corrected by scale_exp.';
