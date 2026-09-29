-- 0029: the Tehran market itself — its indices, its total value, and the
-- real/legal (حقیقی/حقوقی) money flow of the equity roster.
--
-- 0028 stored twenty companies. It could not answer "what did the market do?",
-- because the market is not twenty companies: it is the exchange's own
-- indices, the equal-weighted one beside the cap-weighted one, the sectors, and
-- the money moving between individuals and institutions. Every table here is
-- fed the same way 0028's are — fetched OFF the server by scripts/tsetmc_fetch.py
-- (cdn.tsetmc.com refuses TCP 443 from the production host), copied in over the
-- existing SSH access, and ingested inside the compose network. Nothing here
-- refreshes itself, and every read surface says how old it is.
--
-- WHAT TSETMC ACTUALLY SERVES, MEASURED BEFORE ANY OF THIS WAS DESIGNED
-- (all 71 index histories, 256,419 rows, downloaded 2026-09-29)
--
-- 1. THE CLOSE IS THE ONLY TRUSTWORTHY FIELD. GetIndexB2History carries a close,
--    a low and a high, and no open. The low/high are not a band: 409 of
--    TEDPIX's 4,294 rows have a close OUTSIDE [low, high], and one equal-weighted
--    row has a low of 989,911 under a close of 10,878. They are stored as served
--    — they are evidence — and nothing reads them. There is no CHECK tying them
--    to the close, because the data would violate it.
--
-- 2. SOME VALUES ARE OFF BY EXACTLY A FACTOR OF TEN. 26 steps across six
--    indices move by x10 or /10 in one session, which no index can do: TSE's
--    daily price limits hold TEDPIX's largest measured session to -5.51%. Some
--    revert the next day (the financial index: 1,579,880 -> 154,466 ->
--    1,531,330 on 2020-05-11..13). Some PERSIST: the second-market index has
--    been stored /10 since 2026-08-16 — the history's last value is 1,315,840
--    while TSETMC's own live figure the same morning was 13,310,356.
--
--    So the raw close is kept exactly as served and the correction is a
--    per-row power of ten, `scale_exp`, computed by the ingest and applied at
--    read time as `close * 10^scale_exp` — the same split 0028 makes between a
--    raw bar and its corporate-action factor. The reference scale is the one
--    MOST rows share (the second-market index: 4,263 rows at one scale, 31 at
--    the other), and it is checked against the exchange's live value on every
--    ingest: after correction, all 71 indices agree with TSETMC's live figure
--    to within that session's own move. That agreement is the verdict
--    `market_index_checks` records; an index whose corrected history disagrees
--    with the live value by more than a session could move is REFUSED, and a
--    refused index serves no corrected figure at all.
--
--    A genuine rebasing by the exchange would also appear as a x10 step. The
--    correction treats it identically, which is right for every RETURN (a
--    rebasing is a change of units, not of value) and means the displayed LEVEL
--    is on the majority scale. The live cross-check is what would show that
--    disagreement, rather than it being assumed away.
--
-- 3. A NON-POSITIVE VALUE IS A FAULT, NOT A LEVEL. Four thin sector indices
--    carry 11 zero rows between runs of 272.7 (furniture, 2011-2013) and the
--    like. They are dropped by the parser and counted in its report; the CHECK
--    below makes that the only possible outcome.
--
-- 4. A CLOSED MARKET IS A REPEATED VALUE, NOT A GAP. TEDPIX repeats
--    3,713,955.9 for 50 consecutive sessions from 2026-02-25 to 2026-05-18, and
--    2,984,605.4 for 8 from 2025-06-11. The rows are stored as served — the
--    index DID stand there — and the read side treats an exact repeat as "no
--    session", so a closure does not inject fifty 0% returns into a volatility
--    figure. This is the index-level counterpart of excluding halted bars.
--
-- 5. EVERY THURSDAY/FRIDAY ROW IS A CARRY. The seven weekend rows in TEDPIX
--    (2008, 2009, 2017, 2018) repeat their neighbour or differ by 0.1. Rule 4
--    already absorbs them; nothing here hard-codes the weekend, because Iran's
--    working week is itself a live policy question and a calendar rule would be
--    wrong the day it changes.
--
-- THE MONEY FLOW, CROSS-CHECKED AGAINST 0028
--
-- GetClientTypeHistory gives, per session, buy/sell counts, volumes and values
-- split into individuals (حقیقی, I) and institutions (حقوقی, N). For فولاد its
-- total traded value equals equity_bars.value EXACTLY on 3,835 of 3,848 shared
-- sessions, and buy total equals sell total on all but 3 — two independent
-- identities that the read side re-checks per session, excluding the ones that
-- fail from every aggregate rather than trusting a row because it arrived.
--
-- THE REGISTRY IS DATA, AGAIN
--
-- Like equity_instruments, the 71 indices are seeded here and an unknown
-- insCode is refused by the ingest rather than turned into a row. The names are
-- TSETMC's own labels with ARABIC KAF/YEH folded to their Persian forms (the
-- same fold 0028's symbols use); the English names are descriptive labels
-- written here, not a claim that the exchange publishes them. `weighting` and
-- `return_basis` are stated ONLY where the index's own name or TSE's published
-- methodology says so — شاخص کل is price-and-dividend, شاخص قیمت is price
-- only, هم وزن is equal-weighted — and are 'unstated' everywhere else rather
-- than guessed. A sector code is recorded only where TSETMC's own label carries
-- one ("27-فلزات اساسی"); the Farabourse sector indices carry none and get none.

CREATE TABLE market_indices (
    ins_code      TEXT PRIMARY KEY,           -- TSETMC's index insCode
    name_fa       TEXT NOT NULL,              -- lVal30, Arabic kaf/yeh folded
    name_en       TEXT NOT NULL DEFAULT '',   -- a descriptive label, see header
    market        TEXT NOT NULL CHECK (market IN ('bourse','farabourse')),
    kind          TEXT NOT NULL CHECK (kind IN ('headline','market','segment','sector')),
    sector_code   TEXT NOT NULL DEFAULT '',   -- joins equity_instruments.sector_code
    weighting     TEXT NOT NULL DEFAULT 'unstated'
                  CHECK (weighting IN ('cap','equal','free_float','unstated')),
    return_basis  TEXT NOT NULL DEFAULT 'unstated'
                  CHECK (return_basis IN ('total','price','unstated')),
    display_order INT  NOT NULL DEFAULT 1000,
    -- Coverage, maintained by the ingest from the TABLE, never from a payload.
    first_date    DATE,
    last_date     DATE,
    value_count   INT  NOT NULL DEFAULT 0,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_market_indices_sector ON market_indices (market, sector_code)
    WHERE sector_code <> '';

CREATE TABLE market_index_values (
    ins_code     TEXT NOT NULL REFERENCES market_indices(ins_code) ON DELETE CASCADE,
    trade_date   DATE NOT NULL,                -- dEven, a GREGORIAN integer date
    close        NUMERIC NOT NULL CHECK (close > 0),   -- xNivInuClMresIbs, AS SERVED
    low          NUMERIC,                      -- xNivInuPbMresIbs, as served; not a band
    high         NUMERIC,                      -- xNivInuPhMresIbs, as served; not a band
    -- The correction, not the value: the corrected close is close * 10^scale_exp.
    -- Recomputed over the whole series on every ingest, so it is the one column
    -- here an ingest may UPDATE — the close it multiplies never changes.
    scale_exp    SMALLINT NOT NULL DEFAULT 0 CHECK (scale_exp BETWEEN -3 AND 3),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_index_values_pkey PRIMARY KEY (ins_code, trade_date)
);

-- The verdict, per index per check version: 0028's equity_adjustments for an
-- index. A read serves a corrected figure only when this row says 'validated'.
CREATE TABLE market_index_checks (
    id                  BIGSERIAL PRIMARY KEY,
    ins_code            TEXT NOT NULL REFERENCES market_indices(ins_code) ON DELETE CASCADE,
    check_version       TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('validated','refused')),
    values_total        INT  NOT NULL DEFAULT 0,
    dropped_nonpositive INT  NOT NULL DEFAULT 0,
    scale_breaks        INT  NOT NULL DEFAULT 0,
    rows_rescaled       INT  NOT NULL DEFAULT 0,
    first_break         DATE,
    last_break          DATE,
    -- The largest single-session move AFTER correction, so a reader can see the
    -- worst thing the corrected series still contains rather than being told
    -- it is clean. Thin sector indices legitimately carry large ones.
    largest_move        NUMERIC,
    largest_move_date   DATE,
    -- The exchange's own live figure at fetch time, and corrected-last / live.
    live_value          NUMERIC,
    live_ratio          NUMERIC,
    live_checked_at     TIMESTAMPTZ,
    refusal_reason      TEXT NOT NULL DEFAULT '',
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_index_checks_unique UNIQUE (ins_code, check_version),
    CONSTRAINT market_index_checks_reason CHECK (
        status <> 'refused' OR length(refusal_reason) > 0)
);

-- Total market capitalisation per market per session (GetMarketValueByFlow),
-- in RIALS as TSETMC serves it. Not a return series: it moves with listings,
-- delistings and share issues as well as prices, and the read side says so.
CREATE TABLE market_values (
    market       TEXT NOT NULL CHECK (market IN ('bourse','farabourse')),
    trade_date   DATE NOT NULL,
    market_cap   NUMERIC NOT NULL CHECK (market_cap > 0),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_values_pkey PRIMARY KEY (market, trade_date)
);

-- حقیقی/حقوقی per instrument per session, exactly as served. Values in RIALS.
-- I = individuals (حقیقی), N = institutions (حقوقی).
CREATE TABLE equity_client_flows (
    ins_code     TEXT NOT NULL REFERENCES equity_instruments(ins_code) ON DELETE CASCADE,
    trade_date   DATE NOT NULL,
    buy_i_count  BIGINT  NOT NULL CHECK (buy_i_count  >= 0),
    buy_n_count  BIGINT  NOT NULL CHECK (buy_n_count  >= 0),
    sell_i_count BIGINT  NOT NULL CHECK (sell_i_count >= 0),
    sell_n_count BIGINT  NOT NULL CHECK (sell_n_count >= 0),
    buy_i_volume  NUMERIC NOT NULL CHECK (buy_i_volume  >= 0),
    buy_n_volume  NUMERIC NOT NULL CHECK (buy_n_volume  >= 0),
    sell_i_volume NUMERIC NOT NULL CHECK (sell_i_volume >= 0),
    sell_n_volume NUMERIC NOT NULL CHECK (sell_n_volume >= 0),
    buy_i_value   NUMERIC NOT NULL CHECK (buy_i_value   >= 0),
    buy_n_value   NUMERIC NOT NULL CHECK (buy_n_value   >= 0),
    sell_i_value  NUMERIC NOT NULL CHECK (sell_i_value  >= 0),
    sell_n_value  NUMERIC NOT NULL CHECK (sell_n_value  >= 0),
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT equity_client_flows_pkey PRIMARY KEY (ins_code, trade_date)
);

-- The exchange's market overview at the moment a refresh ran. A SNAPSHOT, not
-- a history: TSETMC publishes today's trade value and count only as a live
-- figure, and this deployment sees it only when someone runs the fetch. The
-- read side labels it with activity_at and never presents it as current.
CREATE TABLE market_snapshots (
    id                 BIGSERIAL PRIMARY KEY,
    market             TEXT NOT NULL CHECK (market IN ('bourse','farabourse')),
    -- marketActivityDEven + HEven, Tehran local time, stored in UTC.
    activity_at        TIMESTAMPTZ NOT NULL,
    index_value        NUMERIC,
    index_change       NUMERIC,
    -- NULL on the Farabourse, where TSETMC sends 0.0 for a figure it does not have.
    ew_index_value     NUMERIC,
    ew_index_change    NUMERIC,
    trade_count        BIGINT,
    trade_volume       NUMERIC,
    trade_value        NUMERIC,                -- rials, every instrument on the market
    market_value       NUMERIC,                -- rials
    market_state       TEXT NOT NULL DEFAULT '',
    market_state_title TEXT NOT NULL DEFAULT '',
    collected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_snapshots_unique UNIQUE (market, activity_at)
);

-- "Sectors at a glance" (GetSectorsSummary): per sector, how many instruments
-- were down more than 2%, down less than 2%, up less than 2% and up more than
-- 2% — TSETMC's own four buckets and its own wording; it reports no
-- "unchanged" bucket, so none is invented here. The payload carries no time of
-- its own, so it is stamped with the bourse overview fetched in the same run.
CREATE TABLE sector_breadth_snapshots (
    activity_at   TIMESTAMPTZ NOT NULL,
    sector_code   TEXT NOT NULL,
    sector_fa     TEXT NOT NULL,
    down_over_2   INT NOT NULL CHECK (down_over_2  >= 0),
    down_under_2  INT NOT NULL CHECK (down_under_2 >= 0),
    up_under_2    INT NOT NULL CHECK (up_under_2   >= 0),
    up_over_2     INT NOT NULL CHECK (up_over_2    >= 0),
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sector_breadth_snapshots_pkey PRIMARY KEY (activity_at, sector_code)
);

INSERT INTO market_indices
  (ins_code, name_fa, name_en, market, kind, sector_code, weighting, return_basis, display_order)
VALUES
  ('32097828799138957','شاخص کل','TEDPIX, all-share (price and dividends)','bourse','headline','','cap','total',10),
  ('67130298613737946','شاخص کل (هم وزن)','Equal-weighted all-share','bourse','headline','','equal','total',20),
  ('5798407779416661','شاخص قیمت(وزنی-ارزشی)','TEPIX, all-share price index','bourse','headline','','cap','price',30),
  ('8384385859414435','شاخص قیمت (هم وزن)','Equal-weighted price index','bourse','headline','','equal','price',40),
  ('49579049405614711','شاخص آزاد شناور','Free-float index','bourse','segment','','free_float','unstated',50),
  ('62752761908615603','شاخص بازار اول','First market (bourse)','bourse','market','','unstated','unstated',60),
  ('71704845530629737','شاخص بازار دوم','Second market (bourse)','bourse','market','','unstated','unstated',70),
  ('43754960038275285','شاخص صنعت','Industry index','bourse','segment','','unstated','unstated',80),
  ('61247168213690670','65-مالی','Financial index','bourse','segment','','unstated','unstated',90),
  ('10523825119011581','شاخص 30 شرکت بزرگ','30 largest companies','bourse','segment','','unstated','unstated',100),
  ('46342955726788357','شاخص50شرکت فعالتر','50 most active companies','bourse','segment','','unstated','unstated',110),
  ('69932667409721265','شاخص قیمت 50 شرکت','Price index, 50 companies','bourse','segment','','unstated','price',120),
  ('43685683301327984','شاخص کل فرابورس','IFX, Farabourse all-share','farabourse','headline','','cap','total',200),
  ('3454456635278563','شاخص کل هم وزن فرابورس','IFX equal-weighted','farabourse','headline','','equal','total',210),
  ('16385144332417446','شاخص قیمت فرابورس','IFX price index','farabourse','headline','','cap','price',220),
  ('24779787662870476','شاخص قیمت هم وزن فرابورس','IFX equal-weighted price index','farabourse','headline','','equal','price',230),
  ('68337207779130851','بازار اول فرابورس','Farabourse first market','farabourse','market','','unstated','unstated',240),
  ('35898516735559137','بازار دوم فرابورس','Farabourse second market','farabourse','market','','unstated','unstated',250),
  ('34408080767216529','01-زراعت','Agriculture','bourse','sector','01','unstated','unstated',1010),
  ('19219679288446732','10-ذغال سنگ','Coal mining','bourse','sector','10','unstated','unstated',1100),
  ('65675836323214668','استخراج نفت جزکشف11','Oil and gas extraction','bourse','sector','11','unstated','unstated',1110),
  ('13235969998952202','13-کانه فلزی','Metal ore mining','bourse','sector','13','unstated','unstated',1130),
  ('62691002126902464','14-سایر معادن','Other mining','bourse','sector','14','unstated','unstated',1140),
  ('59288237226302898','17-منسوجات','Textiles','bourse','sector','17','unstated','unstated',1170),
  ('69306841376553334','19-محصولات چرمی','Leather products','bourse','sector','19','unstated','unstated',1190),
  ('58440550086834602','20-محصولات چوبی','Wood products','bourse','sector','20','unstated','unstated',1200),
  ('30106839080444358','21-محصولات کاغذ','Paper products','bourse','sector','21','unstated','unstated',1210),
  ('25766336681098389','22-انتشار و چاپ','Publishing and printing','bourse','sector','22','unstated','unstated',1220),
  ('12331083953323969','23-فراورده نفتی','Refined petroleum products','bourse','sector','23','unstated','unstated',1230),
  ('36469751685735891','25-لاستیک','Rubber and plastics','bourse','sector','25','unstated','unstated',1250),
  ('47946228513774508','26محصولات کامپیوتری الکترونیکی','Computer and electronic products','bourse','sector','26','unstated','unstated',1260),
  ('32453344048876642','27-فلزات اساسی','Basic metals','bourse','sector','27','unstated','unstated',1270),
  ('1123534346391630','28-محصولات فلزی','Fabricated metal products','bourse','sector','28','unstated','unstated',1280),
  ('11451389074113298','29-ماشین آلات','Machinery and equipment','bourse','sector','29','unstated','unstated',1290),
  ('33878047680249697','31-دستگاههای برقی','Electrical machinery','bourse','sector','31','unstated','unstated',1310),
  ('24733701189547084','32-وسایل ارتباطی','Communication equipment','bourse','sector','32','unstated','unstated',1320),
  ('61848754958448778','33-ابزار پزشکی','Medical instruments','bourse','sector','33','unstated','unstated',1330),
  ('20213770409093165','34-خودرو','Automobiles and parts','bourse','sector','34','unstated','unstated',1340),
  ('58231368623465359','35-حمل و نقل','Transport equipment','bourse','sector','35','unstated','unstated',1350),
  ('29331053506731535','36-مبلمان','Furniture','bourse','sector','36','unstated','unstated',1360),
  ('21948907150049163','38-قند و شکر','Sugar','bourse','sector','38','unstated','unstated',1380),
  ('40355846462826897','39-چند رشته ای ص','Diversified industrials','bourse','sector','39','unstated','unstated',1390),
  ('54843635503648458','40-تامین آب، برق، گاز','Utilities','bourse','sector','40','unstated','unstated',1400),
  ('15508900928481581','42-غذایی بجز قند','Food and beverages (excluding sugar)','bourse','sector','42','unstated','unstated',1420),
  ('3615666621538524','43-مواد دارویی','Pharmaceuticals','bourse','sector','43','unstated','unstated',1430),
  ('33626672012415176','44-شیمیایی','Chemicals','bourse','sector','44','unstated','unstated',1440),
  ('41934470778361119','45-پیمانکاری','Industrial contracting','bourse','sector','45','unstated','unstated',1450),
  ('65986638607018835','47خرده فروشی به جز وسایل نقلیه','Retail','bourse','sector','47','unstated','unstated',1470),
  ('57616105980228781','49-کاشی و سرامیک','Tiles and ceramics','bourse','sector','49','unstated','unstated',1490),
  ('70077233737515808','53-سیمان','Cement','bourse','sector','53','unstated','unstated',1530),
  ('14651627750314021','54-کانی غیرفلزی','Other non-metallic minerals','bourse','sector','54','unstated','unstated',1540),
  ('64514606457616199','55-هتل و رستوران','Hotels and restaurants','bourse','sector','55','unstated','unstated',1550),
  ('34295935482222451','56-سرمایه گذاریها','Investment companies','bourse','sector','56','unstated','unstated',1560),
  ('72002976013856737','57-بانکها','Banks and credit institutions','bourse','sector','57','unstated','unstated',1570),
  ('25163959460949732','58-سایرمالی','Other financial intermediation','bourse','sector','58','unstated','unstated',1580),
  ('24187097921483699','60-حمل و نقل','Transport and storage','bourse','sector','60','unstated','unstated',1600),
  ('41867092385281437','64-رادیویی','Telecommunications','bourse','sector','64','unstated','unstated',1640),
  ('59105676994811497','بیمه و بازنشسته66','Insurance and pension funds','bourse','sector','66','unstated','unstated',1660),
  ('61985386521682984','67-اداره بازارهای مالی','Auxiliary financial activities','bourse','sector','67','unstated','unstated',1670),
  ('4654922806626448','70-انبوه سازی','Real estate and construction','bourse','sector','70','unstated','unstated',1700),
  ('8900726085939949','72-رایانه','Computers and related activities','bourse','sector','72','unstated','unstated',1720),
  ('18780171241610744','73-اطلاعات و ارتباطات','Information and communication','bourse','sector','73','unstated','unstated',1730),
  ('47233872677452574','74-فنی مهندسی','Technical and engineering services','bourse','sector','74','unstated','unstated',1740),
  ('61953785757685638','شاخص ارکان و نهادهای مالی','Financial institutions (Farabourse)','farabourse','sector','','unstated','unstated',2010),
  ('7694150664765911','شاخص چوب','Wood products (Farabourse)','farabourse','sector','','unstated','unstated',2020),
  ('17294951120469927','شاخص دستگاه های برقی','Electrical machinery (Farabourse)','farabourse','sector','','unstated','unstated',2030),
  ('11599352365626742','شاخص فنی مهندسی','Technical and engineering (Farabourse)','farabourse','sector','','unstated','unstated',2040),
  ('29471826302648725','شاخص لاستیک','Rubber and plastics (Farabourse)','farabourse','sector','','unstated','unstated',2050),
  ('29763883614455544','شاخص ماشین آلات فرابورس','Machinery (Farabourse)','farabourse','sector','','unstated','unstated',2060),
  ('37438986132327420','شاخص محصولات فلزی','Fabricated metal products (Farabourse)','farabourse','sector','','unstated','unstated',2070),
  ('54273165806481141','کاشی و سرامیک','Tiles and ceramics (Farabourse)','farabourse','sector','','unstated','unstated',2080)
ON CONFLICT (ins_code) DO NOTHING;

UPDATE market_indices SET notes =
  'The headline index. Stood still at 3,713,955.9 for 50 consecutive sessions from 2026-02-25 to 2026-05-18 while the market was closed, and at 2,984,605.4 for 8 from 2025-06-11; the rows are stored as served and read as no-session days.'
WHERE ins_code = '32097828799138957';
UPDATE market_indices SET notes =
  'Starts 2014-03-19 at its 10,000 base, five years after TEDPIX; any comparison between the two is bounded by that date.'
WHERE ins_code = '67130298613737946';
UPDATE market_indices SET notes =
  'Stored at one tenth of its value from 2026-08-16 onward (history 1,315,840 against a live 13,310,356 on 2026-09-29); corrected by scale_exp.'
WHERE ins_code = '71704845530629737';
UPDATE market_indices SET notes =
  'Carries 9 decimal-shift steps, most of them in January-February 2026 alternating between scales day to day; corrected by scale_exp.'
WHERE ins_code = '69932667409721265';
UPDATE market_indices SET notes =
  'Its first 38 rows (2009-06-29 to 2009-08-23) sit at one tenth of the scale of the other 4,120; corrected by scale_exp.'
WHERE ins_code = '49579049405614711';
