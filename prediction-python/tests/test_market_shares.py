"""The whole market, share by share (migration 0030): the universe mirror,
every share's money flow, the exchange's session rows, the commodity funds'
closes, and the fetch script's decisions.

THE FIXTURES ARE TSETMC'S OWN RESPONSES
---------------------------------------
Captured 2026-09-29 between 13:39 and 14:40 Tehran (the shares' session
closed; the funds' still open), with the project's User-Agent. Rows are copied
as served; only the selection is ours.

=============================================  ================================
tsetmc_market_watch_subset.json                GetMarketWatch (showTraded=false),
                                               25 of its 3,786 rows: four roster
                                               shares, three of their other
                                               boards (…0002, …0003), چکاپا
                                               (…0011), two Farabourse, two پایه,
                                               two IRO5 (one in sector 84), an
                                               untraded secondary board — and ten
                                               rows that are NOT shares: three
                                               funds (one of them سافرون), an
                                               ETF, two options, a bond, a right,
                                               a future, a housing facility.
tsetmc_static_data_industrial_groups.json.gz   GetStaticData: its 66
                                               IndustrialGroup rows and two
                                               PaperType rows, padding and all.
tsetmc_day_20260928_subset.json                GetInstrmentsHistoryInDay/20260928,
                                               30 of its 2,290 rows: the shares
                                               above that traded, plus bonds,
                                               funds and options. insCode is a
                                               bare JSON number; 27 of the 30
                                               exceed 2^53.
tsetmc_day_20250312_subset.json                GetInstrmentsHistoryInDay/20250312
                                               (فولاد's capital-increase
                                               ex-date): four roster shares and
                                               two bonds.
tsetmc_clienttype_farabourse_sdpt.json         GetClientTypeHistory/9917562130961534
                                               (درپاد), whole: 113 rows.
tsetmc_clienttype_board3_foolad3.json          GetClientTypeHistory/53809308236531169
                                               (فولاد3), whole: 31 rows from
                                               2026-08-08.
tsetmc_clienttype_newest_sessions.json         {insCode: GetClientTypeHistory
                                               trimmed to its newest five rows}
                                               for seven shares that traded on
                                               2026-09-28.
tsetmc_fund_silver_daily.json                  GetClosingPriceDailyList/
                                               18156575395080321/0 (سیلور), whole:
                                               183 rows, 25 zero-trade carries.
tsetmc_commodity_fund_identities.json          {insCode: GetInstrumentIdentity}
                                               for the five seeded funds and
                                               سافرون.
tsetmc_instrument_info_iro5.json               GetInstrumentInfo/19492025284339283
                                               (خگلپا, IRO5GLPA0001).
=============================================  ================================

The existing tsetmc_foolad_daily.json.gz (GetClosingPriceDailyList, whole,
2026-09-10) and tsetmc_clienttype_foolad_trimmed.json are reused.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.bourse.funds import (
    TEDPIX,
    FundContradiction,
    FundRestated,
    fund_roster,
    ingest_fund_closes,
    ingest_fund_files,
    reference_restatements,
)
from app.bourse.ingest import (
    FlowContradiction,
    MarketIngestFailed,
    ingest_client_flows,
    settled_cutoff,
)
from app.bourse.parse import (
    SHARE_MARKETS,
    MarketParseError,
    exact_code,
    parse_client_types,
    parse_day_file,
    parse_market_watch,
    parse_session_row,
    parse_static_sectors,
)
from app.bourse.shares import (
    DELIST_GUARD_MARKET_MIN,
    DELIST_GUARD_MIN,
    FLOW_FLOOR,
    SessionContradiction,
    WrongSessionFile,
    ingest_day_file,
    ingest_share_manifest,
    ingest_universe,
    load_manifest,
    refresh_session_coverage,
    share_state,
)
from app.db import (
    commodity_funds,
    data_providers,
    equity_client_flows,
    equity_instruments,
    instruments,
    market_index_values,
    market_indices,
    market_sectors,
    market_session_files,
    market_share_sessions,
    market_shares,
    prices,
    raw_observations,
)
from app.equities.adjust import fold_name, fold_symbol, parse_daily_list

from .conftest import TEST_TOKEN, load_fixture_json, load_fixture_json_gz

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATION_UP = os.path.join(REPO_ROOT, "database", "migrations", "0030_market_flows.up.sql")
MIGRATION_DOWN = os.path.join(REPO_ROOT, "database", "migrations", "0030_market_flows.down.sql")
FETCH_SCRIPT = os.path.join(REPO_ROOT, "scripts", "tsetmc_fetch.py")

FOOLAD = "46348559193224090"
FOOLAD3 = "53809308236531169"
BMLT = "778253364357513"
BMLT2 = "66264163366013063"
KAPA = "28957320033282870"
SDPT = "9917562130961534"          # درپاد, Farabourse
ARMP = "38738476064699383"         # آرمان, پایه
GLPA = "19492025284339283"         # خگلپا, IRO5
MOMS = "60369091709615715"         # مهرمام, IRO5, sector 84
SILVER = "18156575395080321"       # سیلور fund
SAFRON = "51200575796028449"       # سافرون

# 2026-09-29 13:57 Tehran, when the day-file and fund fixtures were captured.
CAPTURED = datetime(2026, 9, 29, 10, 27, tzinfo=timezone.utc)
MARKET_WATCH_AT = datetime(2026, 9, 29, 10, 20, tzinfo=timezone.utc)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _migration_rows(table):
    """The VALUES tuples of one INSERT INTO <table> in migration 0030."""
    sql = _read(MIGRATION_UP)
    body = re.search(
        rf"INSERT INTO {table}\b[^;]*?VALUES(.*?)ON CONFLICT", sql, flags=re.S
    ).group(1)
    return re.findall(r"^\s*\(((?:'(?:[^']|'')*',?\s*)+)\)", body, flags=re.M)


def _fields(tuple_text):
    return [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", tuple_text)]


def _seed_sectors(engine):
    """market_sectors exactly as 0030 seeds it."""
    rows = [_fields(t) for t in _migration_rows("market_sectors")]
    with engine.begin() as conn:
        conn.execute(
            market_sectors.insert(),
            [{"sector_code": c, "name_fa": fa, "name_en": en} for c, fa, en in rows],
        )
    return rows


def _seed_funds(engine):
    """The instruments and commodity_funds rows 0030 leaves in place."""
    registry = {
        "IR_GOLD_FUND_AYAR": "Ayar gold ETF",
        "IR_GOLD_FUND_TALA": "Tala gold ETF",
        "IR_GOLD_FUND_KAHRABA": "Kahraba gold ETF",
        "IR_SILVER_FUND_SILVER": "Nova silver fund (Silver)",
        "IR_SILVER_FUND_SIMIN": "Simin silver fund",
    }
    with engine.begin() as conn:
        conn.execute(
            instruments.insert(),
            [
                {"code": code, "kind": "market_price", "name_en": name, "domain": "fund",
                 "quote_currency": "IRT", "unit": "unit", "calendar_class": "tse_session",
                 "quality_tier": "official_mirror"}
                for code, name in registry.items()
            ],
        )
        conn.execute(
            commodity_funds.insert(),
            [
                {"ins_code": code, "instrument_code": inst, "symbol_fa": sym,
                 "name_fa": name, "underlying": under, "notes": notes}
                for code, inst, sym, name, under, notes in (
                    _fields(t) for t in _migration_rows("commodity_funds")
                )
            ],
        )


def _universe(engine, now=MARKET_WATCH_AT):
    _seed_sectors(engine)
    return ingest_universe(
        engine,
        load_fixture_json("tsetmc_market_watch_subset.json"),
        load_fixture_json_gz("tsetmc_static_data_industrial_groups.json.gz"),
        now=now,
    )


def _share(engine, code):
    with engine.connect() as conn:
        return conn.execute(
            select(market_shares).where(market_shares.c.ins_code == code)
        ).mappings().first()


def _count(engine, table, *where):
    with engine.connect() as conn:
        stmt = select(func.count()).select_from(table)
        for clause in where:
            stmt = stmt.where(clause)
        return conn.execute(stmt).scalar_one()


def _newest(code):
    return copy.deepcopy(load_fixture_json("tsetmc_clienttype_newest_sessions.json")[code])


def _load_fetch_script():
    spec = importlib.util.spec_from_file_location("tsetmc_fetch", FETCH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tsetmc_fetch"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fetch():
    if not os.path.isfile(FETCH_SCRIPT):
        pytest.skip("scripts/ is not mounted — run from a full checkout")
    return _load_fetch_script()


def _write(directory, name, payload):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _manifest(tmp_path, *, flows=(), days=(), watch=True, static=True):
    """A run directory laid out exactly as the fetch lays it out."""
    base = tmp_path / "run" / "shares"
    base.mkdir(parents=True, exist_ok=True)
    if watch:
        _write(base, "market-watch.json", load_fixture_json("tsetmc_market_watch_subset.json"))
    if static:
        _write(base, "static-data.json",
               load_fixture_json_gz("tsetmc_static_data_industrial_groups.json.gz"))
    for code, payload in flows:
        _write(base / "flows", f"flow-{code}.json", payload)
    for stamp, payload in days:
        _write(base / "days", f"day-{stamp}.json", payload)
    manifest = {
        "version": 1,
        "market_watch": "market-watch.json" if watch else None,
        "static_data": "static-data.json" if static else None,
        "flows": [f"flow-{code}.json" for code, _ in flows],
        "days": [f"day-{stamp}.json" for stamp, _ in days],
    }
    return str(_write(base, "manifest.json", manifest))


# --- the migration's seeds ------------------------------------------------------


def test_the_sector_names_are_tsetmcs_own_folded():
    """Every Persian name 0030 seeds is fold_name of GetStaticData's, code for
    code; 84, which no TSETMC list names, carries no name in either language."""
    static = load_fixture_json_gz("tsetmc_static_data_industrial_groups.json.gz")
    served = {
        f"{r['code']:02d}": fold_name(r["name"])
        for r in static["staticData"] if r["type"] == "IndustrialGroup"
    }
    seeded = {code: (fa, en) for code, fa, en in (_fields(t) for t in _migration_rows("market_sectors"))}
    assert len(served) == 66 and len(seeded) == 67
    assert set(seeded) == set(served) | {"84"}
    for code, name in served.items():
        assert seeded[code][0] == name, code
        assert seeded[code][1], f"sector {code} has no English name"
    assert seeded["84"] == ("", "")
    # Folded: no Arabic kaf or yeh survives into a seeded name.
    assert not any(ch in fa for fa, _ in seeded.values() for ch in "كي")


def test_the_funds_are_what_tsetmcs_own_classification_says():
    """Each seeded fund's underlying is TSETMC's sub-sector (6822 gold-based,
    6823 silver-based), its symbol and name are TSETMC's, folded — and سافرون,
    whose sub-sector is only 'agricultural', is deliberately not seeded."""
    identities = load_fixture_json("tsetmc_commodity_fund_identities.json")
    seeded = {t[0]: t for t in (_fields(r) for r in _migration_rows("commodity_funds"))}
    by_subsector = {6822: "gold", 6823: "silver"}
    assert set(seeded) == set(identities) - {SAFRON}
    for code, (_, instrument, symbol, name, underlying, _notes) in seeded.items():
        identity = identities[code]["instrumentIdentity"]
        assert underlying == by_subsector[identity["subSector"]["cSoSecVal"]], code
        assert symbol == fold_symbol(identity["lVal18AFC"])
        assert name == fold_name(identity["lVal30"])
        assert identity["cgrValCot"] == "QS"
        assert instrument.startswith(f"IR_{underlying.upper()}_FUND_")
    saffron = identities[SAFRON]["instrumentIdentity"]
    assert fold_symbol(saffron["lVal18AFC"]) == "سافرون"
    assert fold_name(saffron["subSector"]["lSoSecVal"]) == "صندوق کالایی کشاورزی"
    assert saffron["cgrValCot"] == "Q1"
    # Named in the header's reasoning, and in no statement the database runs.
    executable = "\n".join(
        line for line in _read(MIGRATION_UP).splitlines() if not line.lstrip().startswith("--"))
    assert SAFRON not in executable and "IR_SAFFRON" not in executable


def test_the_iro5_market_is_companies():
    """The universe rule admits IRO5 because TSETMC says it is the
    Farabourse's growth market for companies, not because of the prefix."""
    info = load_fixture_json("tsetmc_instrument_info_iro5.json")["instrumentInfo"]
    assert info["instrumentID"].startswith("IRO5")
    assert fold_name(info["flowTitle"]) == "بازار فرابورس"
    assert "نوآفرین" in fold_name(info["cgrValCotTitle"])
    assert info["zTitad"] > 1e9  # shares outstanding: a company
    assert SHARE_MARKETS["IRO5"] == "sme"


def test_the_down_migration_undoes_every_table_and_restores_the_cascade():
    up, down = _read(MIGRATION_UP), _read(MIGRATION_DOWN)
    created = set(re.findall(r"^CREATE TABLE (\w+)", up, flags=re.M))
    dropped = set(re.findall(r"^DROP TABLE IF EXISTS (\w+)", down, flags=re.M))
    assert created == dropped == {
        "market_sectors", "market_shares", "market_share_sessions",
        "market_session_files", "commodity_funds",
    }
    assert "REFERENCES market_shares(ins_code) ON DELETE RESTRICT" in up
    assert "REFERENCES equity_instruments(ins_code) ON DELETE CASCADE" in down
    assert "DROP INDEX IF EXISTS idx_equity_client_flows_date" in down
    # Flow rows the old key cannot hold are removed BEFORE it is re-added.
    assert down.index("DELETE FROM equity_client_flows") < down.index(
        "REFERENCES equity_instruments(ins_code)")


# --- parsing ---------------------------------------------------------------------


def test_the_market_watch_yields_shares_by_insid():
    watch = parse_market_watch(load_fixture_json("tsetmc_market_watch_subset.json"))
    shares = {s.ins_code: s for s in watch.shares}
    assert watch.rows_total == 25 and len(shares) == 15
    assert {s.market for s in shares.values()} == {"bourse", "farabourse", "paye", "sme"}
    foolad = shares[FOOLAD]
    assert (foolad.symbol_fa, foolad.market, foolad.board, foolad.company_code) == (
        "فولاد", "bourse", "main", "IRO1FOLD")
    # insID, not the ISIN (IRO1FOLD0009 is فولاد's seeded ISIN).
    assert foolad.ins_id == "IRO1FOLD0001"
    assert shares[FOOLAD3].board == "secondary" and shares[FOOLAD3].company_code == "IRO1FOLD"
    assert shares[BMLT2].board == "block" and shares[BMLT2].symbol_fa == "وبملت2"
    assert shares[KAPA].board == "other"
    assert shares[ARMP].market == "paye" and shares[SDPT].market == "farabourse"
    assert shares[GLPA].market == "sme"
    # csv's trailing space is gone, and 84 is kept as served.
    assert foolad.sector_code == "27" and shares[MOMS].sector_code == "84"
    # Folded: فملي arrives with Arabic yeh.
    assert shares["35425587644337450"].symbol_fa == "فملی"
    # Funds, options, bonds, rights, futures: none of them.
    assert SILVER not in shares and SAFRON not in shares
    assert all(s.ins_id[:4] in SHARE_MARKETS for s in shares.values())


def test_the_classification_never_reads_yval_flow_or_cgrvalcot():
    """Two responses to one URL minutes apart differed in exactly these fields;
    present, absent or contradictory, the shares come out the same."""
    payload = load_fixture_json("tsetmc_market_watch_subset.json")
    baseline = parse_market_watch(payload).shares
    noisy = copy.deepcopy(payload)
    for row in noisy["marketwatch"]:
        row.update({"yVal": "300", "flow": 1, "cGrValCot": "N1"})
    assert parse_market_watch(noisy).shares == baseline


def test_a_market_watch_that_is_not_one_is_refused():
    payload = load_fixture_json("tsetmc_market_watch_subset.json")
    only_funds = {"marketwatch": [r for r in payload["marketwatch"]
                                  if r["insID"].startswith("IRT")]}
    with pytest.raises(MarketParseError, match="include no share"):
        parse_market_watch(only_funds)
    twice = copy.deepcopy(payload)
    twice["marketwatch"].append(copy.deepcopy(twice["marketwatch"][0]))
    with pytest.raises(MarketParseError, match="twice"):
        parse_market_watch(twice)
    nameless = copy.deepcopy(payload)
    nameless["marketwatch"][0]["lva"] = " "
    with pytest.raises(MarketParseError, match="no symbol"):
        parse_market_watch(nameless)


def test_static_data_sector_names_are_padded_codes_and_folded_names():
    names = parse_static_sectors(load_fixture_json_gz("tsetmc_static_data_industrial_groups.json.gz"))
    assert len(names) == 66 and "84" not in names
    assert names["01"] == "زراعت و خدمات وابسته"
    assert names["27"] == "فلزات اساسی"  # Arabic yeh folded
    assert all(re.fullmatch(r"[0-9]{2}", code) for code in names)


def test_big_insCodes_survive_exactly():
    """The day file's insCodes are bare numbers above 2^53. Parsed by
    Python's json they are exact ints; parsed through a double they are
    refused, never stored rounded."""
    text = _read(os.path.join(os.path.dirname(__file__), "fixtures",
                              "tsetmc_day_20260928_subset.json"))
    literal = re.findall(r'"insCode":(\d+)', text)
    assert sum(int(code) > 2 ** 53 for code in literal) == 27
    rows = parse_day_file(json.loads(text))
    assert sorted(rows) == sorted(literal)
    assert FOOLAD in rows  # 46348559193224090, which a double makes ...088
    assert int(float(int(FOOLAD))) != int(FOOLAD)
    with pytest.raises(MarketParseError, match="float"):
        parse_day_file(json.loads(text, parse_int=float))


def test_exact_code_accepts_digits_and_integers_only():
    assert exact_code(46348559193224090, "x") == FOOLAD
    assert exact_code(" 46348559193224090 ", "x") == FOOLAD
    for bad in (4.6348559193224090e16, True, None, "12a", "-5", "1" * 21):
        with pytest.raises(MarketParseError):
            exact_code(bad, "x")


def test_a_day_file_is_refused_when_it_repeats_an_instrument_or_is_empty():
    payload = load_fixture_json("tsetmc_day_20260928_subset.json")
    rows = payload["closingPriceDailyHistoryWithInstDetails"]
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(MarketParseError, match="twice"):
        parse_day_file(payload)
    with pytest.raises(MarketParseError, match="empty"):
        parse_day_file({"closingPriceDailyHistoryWithInstDetails": []})


def test_a_session_row_is_read_as_served():
    rows = parse_day_file(load_fixture_json("tsetmc_day_20260928_subset.json"))
    row = parse_session_row(rows[FOOLAD])
    assert (row["close"], row["last_trade"], row["price_yesterday"]) == (3420.0, 3420.0, 3330.0)
    assert row["value"] == 6268711012480.0  # = the flow history's buy total, exactly
    assert row["volume"] == 1834250671.0
    assert row["trades"] == 17793 and isinstance(row["trades"], int)
    raw = copy.deepcopy(rows[FOOLAD])
    raw["pDrCotVal"] = 0.0
    assert parse_session_row(raw)["last_trade"] is None
    raw["priceYesterday"] = 0.0
    with pytest.raises(MarketParseError, match="no reference price"):
        parse_session_row(raw)


def test_the_day_file_reference_price_is_tsetmcs_adjusted_one():
    """MEASURED: on فولاد's 2025-03-12 ex-date the day file's priceYesterday is
    3,982 — the corporate-action-adjusted reference GetClosingPriceDailyList
    serves for that session — and NOT the previous session's close of 5,530.
    So close / price_yesterday over a share's traded sessions is a return a
    capital increase does not break, which is what the read side chains."""
    day = parse_day_file(load_fixture_json("tsetmc_day_20250312_subset.json"))
    session = parse_session_row(day[FOOLAD])
    bars = {b.trade_date: b for b in parse_daily_list(
        load_fixture_json_gz("tsetmc_foolad_daily.json.gz"))}
    assert session["price_yesterday"] == 3982.0
    assert bars[date(2025, 3, 11)].final_close == 5530.0
    assert session["price_yesterday"] == bars[date(2025, 3, 12)].price_yesterday
    assert session["close"] == bars[date(2025, 3, 12)].final_close == 3922.0
    assert session["value"] == bars[date(2025, 3, 12)].value


# --- the universe ----------------------------------------------------------------


def test_the_universe_is_mirrored_with_every_board(engine):
    report = _universe(engine)
    assert report["shares"] == 15 and report["inserted"] == 15
    assert report["markets"] == {"bourse": 9, "farabourse": 2, "paye": 2, "sme": 2}
    assert report["boards"] == {"block": 1, "main": 10, "other": 1, "secondary": 3}
    assert report["unknown_sector"]["count"] == 0  # 84 is seeded, empty-named
    row = _share(engine, FOOLAD3)
    assert (row["ins_id"], row["symbol_fa"], row["board"], row["company_code"],
            row["sector_code"], row["listed"]) == (
        "IRO1FOLD0003", "فولاد3", "secondary", "IRO1FOLD", "27", True)
    assert _count(engine, market_shares) == 15


def test_a_second_market_watch_changes_nothing_but_last_seen(engine):
    _universe(engine)
    before = _share(engine, FOOLAD)
    later = MARKET_WATCH_AT + timedelta(days=7)
    again = ingest_universe(engine, load_fixture_json("tsetmc_market_watch_subset.json"), now=later)
    assert (again["inserted"], again["renamed"], again["resectored"], again["delisted"]) == (0, 0, 0, 0)
    after = _share(engine, FOOLAD)
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["updated_at"] == before["updated_at"]
    assert after["last_seen_at"] != before["last_seen_at"]


def test_renames_resectors_delistings_and_relistings(engine):
    _universe(engine)
    payload = load_fixture_json("tsetmc_market_watch_subset.json")
    changed = copy.deepcopy(payload)
    rows = {r["insCode"]: r for r in changed["marketwatch"]}
    rows[SDPT]["lva"] = "درپادجدید"
    rows[ARMP]["csv"] = "56 "
    changed["marketwatch"] = [r for r in changed["marketwatch"] if r["insCode"] != GLPA]
    report = ingest_universe(engine, changed, now=MARKET_WATCH_AT + timedelta(days=1))
    assert (report["renamed"], report["resectored"], report["delisted"]) == (1, 1, 1)
    assert report["renamed_examples"] == [{"ins_code": SDPT, "from": "درپاد", "to": "درپادجدید"}]
    assert _share(engine, ARMP)["sector_code"] == "56"
    gone = _share(engine, GLPA)
    assert gone is not None and gone["listed"] is False  # never deleted
    back = ingest_universe(engine, payload, now=MARKET_WATCH_AT + timedelta(days=2))
    assert back["relisted"] == 1 and _share(engine, GLPA)["listed"] is True


def test_a_truncated_market_watch_cannot_delist_the_market(engine):
    _seed_sectors(engine)
    with engine.begin() as conn:
        conn.execute(market_shares.insert(), [
            {"ins_code": str(10_000 + i), "symbol_fa": f"s{i}", "market": "bourse",
             "board": "main", "listed": True}
            for i in range(DELIST_GUARD_MIN * 2)
        ])
    with pytest.raises(MarketParseError, match="truncated or partial"):
        ingest_universe(engine, load_fixture_json("tsetmc_market_watch_subset.json"))
    assert _count(engine, market_shares, market_shares.c.listed.is_(True)) == DELIST_GUARD_MIN * 2
    assert _count(engine, market_shares) == DELIST_GUARD_MIN * 2


def test_a_watch_missing_one_market_cannot_delist_that_market(engine):
    """A watch without the Farabourse passes the universe-wide test (354 of
    1,162 shares, 30%) and used to mark every Farabourse share unlisted."""
    _universe(engine)
    with engine.begin() as conn:
        conn.execute(market_shares.insert(), [
            {"ins_code": str(20_000 + i), "symbol_fa": f"f{i}", "market": "farabourse",
             "board": "main", "listed": True}
            for i in range(DELIST_GUARD_MARKET_MIN * 2)
        ])
    listed = _count(engine, market_shares, market_shares.c.listed.is_(True))
    with pytest.raises(MarketParseError, match="listed farabourse shares"):
        ingest_universe(engine, load_fixture_json("tsetmc_market_watch_subset.json"),
                        now=MARKET_WATCH_AT + timedelta(days=1))
    assert _count(engine, market_shares, market_shares.c.listed.is_(True)) == listed


def test_the_seeded_roster_row_becomes_listed_and_gains_its_insid(engine):
    """0030 seeds the roster listed=FALSE with no insID; the first market watch
    completes it without inserting a second row."""
    with engine.begin() as conn:
        conn.execute(market_shares.insert().values(
            ins_code=FOOLAD, symbol_fa="فولاد", name_fa="فولاد مبارکه اصفهان",
            market="bourse", board="main", company_code="IRO1FOLD", sector_code="27",
            listed=False))
    report = _universe(engine)
    assert report["inserted"] == 14 and report["relisted"] == 1
    row = _share(engine, FOOLAD)
    assert row["listed"] is True and row["ins_id"] == "IRO1FOLD0001"


def test_sector_names_are_refreshed_and_new_codes_added_without_english(engine):
    _seed_sectors(engine)
    with engine.begin() as conn:
        conn.execute(market_sectors.update().where(market_sectors.c.sector_code == "27")
                     .values(name_fa="نام قدیمی"))
        conn.execute(market_sectors.delete().where(market_sectors.c.sector_code == "93"))
    report = ingest_universe(
        engine, load_fixture_json("tsetmc_market_watch_subset.json"),
        load_fixture_json_gz("tsetmc_static_data_industrial_groups.json.gz"))
    assert report["sectors"]["renamed"] == ["27"] and report["sectors"]["added"] == ["93"]
    with engine.connect() as conn:
        rows = {r.sector_code: r for r in conn.execute(select(market_sectors))}
    assert rows["27"].name_fa == "فلزات اساسی"
    assert rows["93"].name_fa == "فعالیتهای فرهنگی و ورزشی" and rows["93"].name_en == ""
    assert rows["84"].name_fa == "" and rows["27"].name_en == "Basic metals"


def test_a_share_in_an_unknown_sector_is_counted(engine):
    payload = load_fixture_json("tsetmc_market_watch_subset.json")
    report = ingest_universe(engine, payload)  # no sectors seeded at all
    assert report["unknown_sector"]["count"] == 15
    assert report["unknown_sector"]["codes"]["27"] == 4


# --- money flow, for any share in the universe --------------------------------------


def test_a_non_roster_share_ingests_and_its_coverage_comes_from_the_table(engine):
    _universe(engine)
    report = ingest_client_flows(
        engine, load_fixture_json("tsetmc_clienttype_farabourse_sdpt.json"), now=CAPTURED)
    assert report["sessions_inserted"] == 113 and report["symbol"] == "درپاد"
    row = _share(engine, SDPT)
    stored = _count(engine, equity_client_flows, equity_client_flows.c.ins_code == SDPT)
    assert row["flow_count"] == stored == 113
    assert (row["flow_first_date"], row["flow_last_date"]) == (date(2026, 1, 4), date(2026, 9, 28))


def test_a_share_not_in_the_universe_is_refused_and_nothing_is_written(engine):
    with pytest.raises(MarketParseError, match="not in market_shares"):
        ingest_client_flows(engine, load_fixture_json("tsetmc_clienttype_farabourse_sdpt.json"))
    assert _count(engine, equity_client_flows) == 0


def test_a_file_named_for_one_share_carrying_another_is_refused(engine):
    _universe(engine)
    with pytest.raises(MarketParseError, match="asked for"):
        ingest_client_flows(
            engine, load_fixture_json("tsetmc_clienttype_farabourse_sdpt.json"), ins_code=FOOLAD3)


def test_an_overlapping_re_ingest_is_idempotent(engine):
    _universe(engine)
    full = load_fixture_json("tsetmc_clienttype_board3_foolad3.json")
    ingest_client_flows(engine, full, now=CAPTURED)
    trimmed = {"clientType": full["clientType"][:10]}  # the overlap a fetch ships
    again = ingest_client_flows(engine, trimmed, now=CAPTURED)
    assert again["sessions_inserted"] == 0 and again["sessions_existing"] == 10
    assert _share(engine, FOOLAD3)["flow_count"] == 31


def test_an_unsettled_flow_row_is_not_stored(engine):
    _universe(engine)
    mid_session = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)  # 10:30 Tehran
    report = ingest_client_flows(
        engine, load_fixture_json("tsetmc_clienttype_board3_foolad3.json"), now=mid_session)
    assert report["unsettled_skipped"] == 1 and report["last_date"] == "2026-09-27"


def test_a_restated_overlap_row_is_kept_and_does_not_stall_the_share(engine):
    """The fetch re-sends a share's ten newest stored rows every week. When
    TSETMC's copy of one of them changed, the share used to fail on it — every
    week, because the same ten rows go back each time — and nothing newer was
    ever stored. The stored row stays; the new rows are stored."""
    _universe(engine)
    full = load_fixture_json("tsetmc_clienttype_board3_foolad3.json")
    ingest_client_flows(engine, {"clientType": full["clientType"][1:]}, now=CAPTURED)
    weekly = copy.deepcopy({"clientType": full["clientType"][:11]})  # 1 new + 10 overlap
    weekly["clientType"][5]["buy_I_Value"] += 1_000_000
    report = ingest_client_flows(engine, weekly, now=CAPTURED)
    assert report["sessions_inserted"] == 1 and report["restated"] == 1
    [example] = report["restated_examples"]
    assert "buy_i_value" in example["fields"]
    assert _share(engine, FOOLAD3)["flow_count"] == 31
    with engine.connect() as conn:
        kept = conn.execute(select(equity_client_flows.c.buy_i_value).where(
            (equity_client_flows.c.ins_code == FOOLAD3)
            & (equity_client_flows.c.trade_date == date.fromisoformat(example["date"]))
        )).scalar_one()
    assert float(kept) == float(full["clientType"][5]["buy_I_Value"])  # not overwritten


def test_a_flow_payload_unlike_most_of_its_overlap_fails_that_share_only(engine, tmp_path):
    _universe(engine)
    sdpt = load_fixture_json("tsetmc_clienttype_farabourse_sdpt.json")
    ingest_client_flows(engine, sdpt, now=CAPTURED)
    other = {"clientType": copy.deepcopy(sdpt["clientType"][:10])}  # the overlap a fetch ships
    for row in other["clientType"]:
        row["buy_I_Value"] *= 10
    manifest = _manifest(tmp_path, flows=[
        (SDPT, other), (FOOLAD3, load_fixture_json("tsetmc_clienttype_board3_foolad3.json"))])
    report = ingest_share_manifest(engine, manifest, "flows", now=CAPTURED)
    assert set(report["items"]) == {FOOLAD3}
    [error] = report["errors"]
    assert error["ins_code"] == SDPT and error["error"] == FlowContradiction.__name__
    assert "not a restatement" in error["message"]
    assert _count(engine, equity_client_flows, equity_client_flows.c.ins_code == SDPT) == 113


# --- sessions --------------------------------------------------------------------------


def _flows_for_20260928(engine):
    for code, payload in load_fixture_json("tsetmc_clienttype_newest_sessions.json").items():
        ingest_client_flows(engine, payload, now=CAPTURED)


def test_a_day_file_stores_only_universe_shares_with_exact_codes(engine):
    _universe(engine)
    _flows_for_20260928(engine)
    report, inserted = ingest_day_file(
        engine, load_fixture_json("tsetmc_day_20260928_subset.json"), date(2026, 9, 28),
        now=CAPTURED)
    assert report["rows_total"] == 30
    # All fifteen shares of the market-watch subset traded that day.
    assert report["rows_shares"] == report["rows_inserted"] == len(inserted) == 15
    # Every one of the seven shares with stored flow agrees with it on value.
    assert report["date_check"] == {"against": "equity_client_flows", "compared": 7,
                                    "agreed": 7, "disagreed": 0, "status": "agreed"}
    with engine.connect() as conn:
        codes = set(conn.execute(select(market_share_sessions.c.ins_code)).scalars())
        record = conn.execute(select(market_session_files)).mappings().one()
    assert FOOLAD in codes and SILVER not in codes  # a fund is not a share
    assert codes <= {s.ins_code for s in parse_market_watch(
        load_fixture_json("tsetmc_market_watch_subset.json")).shares}
    assert (record["trade_date"], record["rows_total"], record["rows_shares"]) == (
        date(2026, 9, 28), 30, 15)


def test_a_day_file_re_ingest_is_idempotent(engine):
    _universe(engine)
    payload = load_fixture_json("tsetmc_day_20260928_subset.json")
    ingest_day_file(engine, payload, date(2026, 9, 28), now=CAPTURED)
    again, inserted = ingest_day_file(engine, payload, date(2026, 9, 28), now=CAPTURED)
    assert again["rows_inserted"] == 0 and again["rows_existing"] == 15 and not inserted
    assert again["file_record"] == "unchanged"
    assert again["date_check"]["status"] == "unchecked"  # no flows stored in this test


def test_a_day_file_named_for_another_session_is_refused(engine):
    """TSETMC's file carries no date. Stored under 2026-09-27, the 2026-09-28
    file agrees with that day's stored flows on one share of the six compared —
    پلوله, whose traded value happened to differ by 0.3% between the two days —
    where the genuine date agrees on every share compared (seven of seven)."""
    _universe(engine)
    _flows_for_20260928(engine)
    with pytest.raises(WrongSessionFile, match="only 1 of the 6 shares"):
        ingest_day_file(engine, load_fixture_json("tsetmc_day_20260928_subset.json"),
                        date(2026, 9, 27), now=CAPTURED)
    assert _count(engine, market_share_sessions) == 0
    assert _count(engine, market_session_files) == 0


def test_a_restated_day_row_is_kept_and_the_rest_of_the_day_is_stored(engine):
    """MEASURED 2026-09-29: the 2026-09-28 day file differed between two CDN
    copies by one trade on کرمان and وطوبی, and failed the whole day on the
    re-check. The stored rows stay; the shares not yet stored that day are
    written."""
    _universe(engine)
    good = load_fixture_json("tsetmc_day_20260928_subset.json")
    rows = good["closingPriceDailyHistoryWithInstDetails"]
    first = {"closingPriceDailyHistoryWithInstDetails": [
        r for r in rows if r["insCode"] != int(SDPT)]}
    ingest_day_file(engine, first, date(2026, 9, 28), now=CAPTURED)
    other = copy.deepcopy(good)
    for row in other["closingPriceDailyHistoryWithInstDetails"]:
        if row["insCode"] == int(FOOLAD):
            row["zTotTran"] += 1
    report, inserted = ingest_day_file(engine, other, date(2026, 9, 28), now=CAPTURED)
    assert inserted == {SDPT} and report["rows_restated"] == 1
    assert report["restated_examples"] == [{"ins_code": FOOLAD, "fields": ["trades"]}]
    with engine.connect() as conn:
        trades = conn.execute(select(market_share_sessions.c.trades).where(
            (market_share_sessions.c.ins_code == FOOLAD)
            & (market_share_sessions.c.trade_date == date(2026, 9, 28)))).scalar_one()
    assert trades == parse_session_row(parse_day_file(good)[FOOLAD])["trades"]


def test_a_day_file_unlike_most_of_the_stored_day_fails_that_day_only(engine, tmp_path):
    _universe(engine)
    good = load_fixture_json("tsetmc_day_20260928_subset.json")
    ingest_day_file(engine, good, date(2026, 9, 28), now=CAPTURED)
    other = copy.deepcopy(good)
    for row in other["closingPriceDailyHistoryWithInstDetails"]:
        row["pClosing"] += 10
    manifest = _manifest(tmp_path, days=[
        ("20250312", load_fixture_json("tsetmc_day_20250312_subset.json")),
        ("20260928", other)])
    report = ingest_share_manifest(engine, manifest, "sessions", now=CAPTURED)
    assert set(report["items"]) == {"2025-03-12"}
    [error] = report["errors"]
    assert error["trade_date"] == "2026-09-28"
    assert error["error"] == SessionContradiction.__name__
    assert "not a restatement" in error["message"]
    with engine.connect() as conn:
        close = conn.execute(select(market_share_sessions.c.close).where(
            (market_share_sessions.c.ins_code == FOOLAD)
            & (market_share_sessions.c.trade_date == date(2026, 9, 28)))).scalar_one()
    assert float(close) == float(parse_session_row(
        parse_day_file(good)[FOOLAD])["close"])  # not overwritten
    # Coverage from the table: two sessions for فولاد now.
    row = _share(engine, FOOLAD)
    assert (row["session_count"], row["session_last_date"]) == (2, date(2026, 9, 28))

def test_an_unsettled_session_is_refused(engine):
    _universe(engine)
    before_close = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)  # 11:30 Tehran
    assert settled_cutoff(before_close) == date(2026, 9, 28)
    with pytest.raises(MarketParseError, match="not a settled session"):
        ingest_day_file(engine, load_fixture_json("tsetmc_day_20260928_subset.json"),
                        date(2026, 9, 28), now=before_close)


def test_an_unstorable_row_is_counted_and_the_rest_of_the_day_stored(engine):
    _universe(engine)
    payload = load_fixture_json("tsetmc_day_20260928_subset.json")
    for row in payload["closingPriceDailyHistoryWithInstDetails"]:
        if row["insCode"] == int(SDPT):
            row["priceYesterday"] = 0.0
    report, _ = ingest_day_file(engine, payload, date(2026, 9, 28), now=CAPTURED)
    assert report["rows_invalid"] == 1 and report["rows_inserted"] == 14
    assert report["invalid_examples"][0]["ins_code"] == SDPT


def test_session_coverage_is_recomputed_from_the_table(engine):
    _universe(engine)
    ingest_day_file(engine, load_fixture_json("tsetmc_day_20250312_subset.json"),
                    date(2025, 3, 12), now=CAPTURED)
    _, inserted = ingest_day_file(engine, load_fixture_json("tsetmc_day_20260928_subset.json"),
                                  date(2026, 9, 28), now=CAPTURED)
    assert refresh_session_coverage(engine, inserted | {FOOLAD}, now=CAPTURED) == 15
    row = _share(engine, FOOLAD)
    assert (row["session_count"], row["session_last_date"]) == (2, date(2026, 9, 28))
    assert _share(engine, SDPT)["session_count"] == 1


def test_a_day_ingested_before_its_shares_joined_the_universe_is_asked_for_again(engine):
    """A day file stores only the shares market_shares carries when it is
    ingested, and a recorded day is never fetched again. Ingested while the
    universe was short — here only the roster's فولاد, as after a first run
    whose market-watch ingest failed — the other shares' session rows for that
    day would be missing for good. The state names such a day until a
    re-ingest has seen it against the universe as it now stands."""
    _seed_sectors(engine)
    with engine.begin() as conn:
        conn.execute(market_shares.insert().values(
            ins_code=FOOLAD, symbol_fa="فولاد", market="bourse", board="main",
            company_code="IRO1FOLD", sector_code="27", listed=False,
            first_seen_at=CAPTURED - timedelta(days=1)))
    day = load_fixture_json("tsetmc_day_20260928_subset.json")
    first, _ = ingest_day_file(engine, day, date(2026, 9, 28), now=CAPTURED)
    assert first["rows_inserted"] == 1
    assert share_state(engine)["session_dates_incomplete"] == []
    later = CAPTURED + timedelta(hours=1)
    ingest_universe(engine, load_fixture_json("tsetmc_market_watch_subset.json"), now=later)
    _flows_for_20260928(engine)
    state = share_state(engine)
    assert state["session_dates"] == ["2026-09-28"]
    assert state["session_dates_incomplete"] == ["2026-09-28"]
    again, inserted = ingest_day_file(engine, day, date(2026, 9, 28),
                                      now=later + timedelta(hours=1))
    assert again["rows_inserted"] == len(inserted) == 14
    assert share_state(engine)["session_dates_incomplete"] == []


def test_a_recheck_that_finds_nothing_new_is_not_asked_for_on_every_run(engine):
    """A share that joined the universe after a day was ingested, with a traded
    flow row that day, which TSETMC's day file does not carry: one re-ingest
    checks the day against the grown universe, and the state stops naming it
    rather than asking for the same file on every run."""
    _universe(engine)
    day = load_fixture_json("tsetmc_day_20260928_subset.json")
    ingest_day_file(engine, day, date(2026, 9, 28), now=CAPTURED)
    later = CAPTURED + timedelta(hours=1)
    ghost = "12345678"
    with engine.begin() as conn:
        conn.execute(market_shares.insert().values(
            ins_code=ghost, symbol_fa="نبود", market="bourse", board="main",
            first_seen_at=later))
    flows = _newest(FOOLAD)
    for row in flows["clientType"]:
        row["insCode"] = ghost
    ingest_client_flows(engine, flows, now=CAPTURED)
    assert share_state(engine)["session_dates_incomplete"] == ["2026-09-28"]
    again, inserted = ingest_day_file(engine, day, date(2026, 9, 28),
                                      now=later + timedelta(hours=1))
    assert again["rows_inserted"] == 0 and not inserted
    assert again["file_record"] == "rechecked"
    assert share_state(engine)["session_dates_incomplete"] == []
    # And with nothing new since, a re-ingest is what it always was.
    third, _ = ingest_day_file(engine, day, date(2026, 9, 28), now=later + timedelta(hours=2))
    assert third["file_record"] == "unchanged"


# --- the manifest -------------------------------------------------------------------------


@pytest.mark.parametrize("entry, key", [
    ("../flow-1.json", "flows"),
    ("/etc/flow-1.json", "flows"),
    ("flow-1.json/../../x", "flows"),
    ("flow-12a.json", "flows"),
    ("flow-1.json.gz", "flows"),
    ("day-2026928.json", "days"),
    ("day-20261399.json", "days"),
    ("../day-20260928.json", "days"),
])
def test_a_manifest_name_that_could_leave_its_directory_is_refused(tmp_path, entry, key):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, key: [entry]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(str(path))


def test_the_manifest_refuses_wrong_fixed_names_versions_and_duplicates(tmp_path):
    path = tmp_path / "manifest.json"
    for body in (
        {"version": 1, "market_watch": "../market-watch.json"},
        {"version": 1, "static_data": "sectors.json"},
        {"version": 2},
        {"version": 1, "flows": ["flow-1.json", "flow-1.json"]},
        {"version": 1, "flows": "flow-1.json"},
    ):
        path.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(ValueError):
            load_manifest(str(path))
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be named"):
        load_manifest(str(other))


def test_a_symlink_out_of_the_run_directory_is_refused(tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    run = tmp_path / "run"
    (run / "flows").mkdir(parents=True)
    (run / "flows" / "flow-1.json").symlink_to(outside / "anything.json")
    (run / "manifest.json").write_text(
        json.dumps({"version": 1, "flows": ["flow-1.json"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the manifest directory"):
        load_manifest(str(run / "manifest.json"))


def test_chunked_calls_add_up_to_one_full_run(engine, tmp_path):
    flows = list(load_fixture_json("tsetmc_clienttype_newest_sessions.json").items())
    manifest = _manifest(tmp_path, flows=flows)
    assert ingest_share_manifest(engine, manifest, "universe", now=CAPTURED)["universe"]["inserted"] == 15
    seen, offset, calls = {}, 0, 0
    while offset is not None:
        report = ingest_share_manifest(engine, manifest, "flows", offset=offset, limit=3,
                                       now=CAPTURED)
        seen.update(report["items"])
        offset, calls = report["next_offset"], calls + 1
    assert calls == 3 and len(seen) == 7
    assert _count(engine, equity_client_flows) == 35
    with pytest.raises(ValueError, match="past the end"):
        ingest_share_manifest(engine, manifest, "flows", offset=7, now=CAPTURED)


def test_a_broken_sector_names_file_does_not_cost_the_universe(engine, tmp_path):
    manifest = _manifest(tmp_path)
    broken = {"staticData": [{"type": "IndustrialGroup", "code": "27", "name": "x" * 20}] * 5}
    _write(tmp_path / "run" / "shares", "static-data.json", broken)
    report = ingest_share_manifest(engine, manifest, "universe", now=CAPTURED)
    assert report["universe"]["inserted"] == 15 and report["universe"]["sectors"] is None
    [error] = report["errors"]
    assert error["kind"] == "static_data" and "0..99" in error["message"]


def test_a_call_in_which_everything_failed_raises(engine, tmp_path):
    manifest = _manifest(tmp_path, flows=[(SDPT, load_fixture_json(
        "tsetmc_clienttype_farabourse_sdpt.json"))])
    with pytest.raises(MarketIngestFailed):  # no universe ingested: not in market_shares
        ingest_share_manifest(engine, manifest, "flows", now=CAPTURED)
    with pytest.raises(ValueError, match="part must be"):
        ingest_share_manifest(engine, manifest, "bars")
    with pytest.raises(ValueError, match="limit"):
        ingest_share_manifest(engine, manifest, "flows", limit=0)


def test_the_share_state_is_what_the_fetch_needs(engine):
    _universe(engine)
    with engine.begin() as conn:
        conn.execute(equity_instruments.insert().values(
            ins_code=FOOLAD, symbol_fa="فولاد", name_fa="فولاد مبارکه اصفهان", market="bourse"))
    ingest_client_flows(engine, _newest(FOOLAD), now=CAPTURED)
    ingest_day_file(engine, load_fixture_json("tsetmc_day_20260928_subset.json"),
                    date(2026, 9, 28), now=CAPTURED)
    state = share_state(engine)
    assert state["floor"] == FLOW_FLOOR.isoformat() == "2025-03-21"
    assert state["session_dates"] == ["2026-09-28"]
    items = {i["ins_code"]: i for i in state["items"]}
    assert items[FOOLAD]["roster"] is True and items[SDPT]["roster"] is False
    assert (items[FOOLAD]["flow_count"], items[FOOLAD]["flow_last_date"]) == (5, "2026-09-28")
    assert items[SDPT]["flow_count"] == 0 and items[SDPT]["flow_last_date"] is None


# --- the commodity funds ------------------------------------------------------------------


def test_fund_closes_are_toman_settled_traded_and_stamped_after_the_close(engine):
    _seed_funds(engine)
    report = ingest_fund_closes(engine, load_fixture_json("tsetmc_fund_silver_daily.json"),
                                now=CAPTURED)
    assert report["instrument_code"] == "IR_SILVER_FUND_SILVER"
    assert (report["sessions_total"], report["closes_inserted"],
            report["carry_forward_skipped"], report["unsettled_skipped"]) == (183, 158, 25, 0)
    assert report["last_close"] == 1357.8  # 13,578 rials
    with engine.connect() as conn:
        newest = conn.execute(select(prices).order_by(prices.c.observed_at.desc())).mappings().first()
        raw = conn.execute(select(raw_observations).where(
            raw_observations.c.dedupe_key
            == "tsetmc_cdn|IR_SILVER_FUND_SILVER|close|2026-09-28")).mappings().one()
        fund = conn.execute(select(commodity_funds).where(
            commodity_funds.c.ins_code == SILVER)).mappings().one()
    assert (newest["symbol"], newest["value"], newest["currency"], newest["unit"],
            newest["source"]) == ("IR_SILVER_FUND_SILVER", 1357.8, "IRT", "unit", "tsetmc_cdn")
    stamp = newest["observed_at"].replace(tzinfo=newest["observed_at"].tzinfo or timezone.utc)
    assert stamp == datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)
    assert (raw["raw_value"], raw["currency"], raw["unit"]) == (13578.0, "IRR", "IRR/unit")
    # Listed 2025-12-20 at 10,000 rials; the first two rows are untraded
    # carries, so the first stored close is its first traded session.
    assert (fund["first_close"], fund["last_close"], fund["close_count"]) == (
        date(2025, 12, 22), date(2026, 9, 28), 158)
    # The zero-trade carries of the 2026 closures are not prices of anything.
    with engine.connect() as conn:
        days = {r.observed_at.date() for r in conn.execute(select(prices.c.observed_at))}
    assert date(2026, 1, 25) not in days


def test_a_close_whose_stamp_is_in_the_future_is_not_stored(engine):
    """Funds trade until 18:00 Tehran; at 21:30 Tehran on 2026-09-28 that
    session's 23:00 UTC stamp has not arrived, so its close waits."""
    _seed_funds(engine)
    evening = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
    report = ingest_fund_closes(engine, load_fixture_json("tsetmc_fund_silver_daily.json"),
                                now=evening)
    assert report["unsettled_skipped"] == 1 and report["last_date"] == "2026-09-27"


def test_a_fund_re_ingest_is_idempotent_and_a_restated_close_is_kept(engine):
    """TSETMC's two copies differ on Tala's 2021-12-15 close by ten rials and
    on عیار's by one. One rial is the exchange's rounding; ten is a
    restatement, kept as stored and named, and it no longer fails the fund."""
    _seed_funds(engine)
    payload = load_fixture_json("tsetmc_fund_silver_daily.json")
    ingest_fund_closes(engine, payload, now=CAPTURED)
    again = ingest_fund_closes(engine, payload, now=CAPTURED)
    assert again["closes_inserted"] == 0 and again["closes_existing"] == 158
    assert again["restated"] == 0
    rounded = copy.deepcopy(payload)
    max(rounded["closingPriceDaily"], key=lambda r: r["dEven"])["pClosing"] += 1
    assert ingest_fund_closes(engine, rounded, now=CAPTURED)["restated"] == 0
    changed = copy.deepcopy(payload)
    newest = max(changed["closingPriceDaily"], key=lambda r: r["dEven"])
    newest["pClosing"] += 10
    report = ingest_fund_closes(engine, changed, now=CAPTURED + timedelta(days=1))
    assert report["restated"] == 1
    assert report["restated_examples"] == [
        {"date": "2026-09-28", "stored": 1357.8, "served": 1358.8}]
    assert _count(engine, prices) == 158
    with engine.connect() as conn:
        newest_value = conn.execute(
            select(prices.c.value).order_by(prices.c.observed_at.desc())).scalars().first()
    assert float(newest_value) == 1357.8  # never overwritten


def test_a_fund_list_unlike_most_of_the_store_fails_the_fund(engine):
    _seed_funds(engine)
    payload = load_fixture_json("tsetmc_fund_silver_daily.json")
    ingest_fund_closes(engine, payload, now=CAPTURED)
    tenfold = copy.deepcopy(payload)
    for row in tenfold["closingPriceDaily"]:
        row["pClosing"] *= 10
        row["priceYesterday"] *= 10
    with pytest.raises(FundContradiction, match="not a restatement"):
        ingest_fund_closes(engine, tenfold, now=CAPTURED)
    assert _count(engine, prices) == 158


def _tedpix_sessions(engine, payload):
    """The exchange calendar the fund check reads: TEDPIX on every day the
    fixture's list carries."""
    days = sorted({date(r["dEven"] // 10000, r["dEven"] // 100 % 100, r["dEven"] % 100)
                   for r in payload["closingPriceDaily"]})
    with engine.begin() as conn:
        conn.execute(market_index_values.insert(), [
            {"ins_code": TEDPIX, "trade_date": d, "close": 1.0, "scale_exp": 0,
             "collected_at": CAPTURED} for d in days])


def _without_a_moving_session(payload):
    """The copy of a daily list TSETMC's CDN served without one traded session
    whose close moved: the next row's reference is that missing close."""
    rows = sorted(copy.deepcopy(payload["closingPriceDaily"]), key=lambda r: r["dEven"])
    i = next(i for i in range(1, len(rows) - 1)
             if rows[i]["zTotTran"] > 0 and rows[i + 1]["zTotTran"] > 0
             and rows[i]["pClosing"] != rows[i - 1]["pClosing"])
    gone = rows.pop(i)
    return {"closingPriceDaily": rows}, gone


def test_a_copy_missing_a_session_is_a_gap_not_a_restated_reference(engine):
    """MEASURED 2026-09-29: one CDN copy omits 2023-03-27, so AYAR's 2023-03-28
    reference (77,041, the missing session's close) did not chain to
    2023-03-26's close (75,820) and all three gold funds were refused as
    restated. A pair that skips a session TEDPIX records is not judged."""
    _seed_funds(engine)
    payload = load_fixture_json("tsetmc_fund_silver_daily.json")
    short, gone = _without_a_moving_session(payload)
    _tedpix_sessions(engine, payload)
    report = ingest_fund_closes(engine, short, now=CAPTURED)
    gone_day = date(gone["dEven"] // 10000, gone["dEven"] // 100 % 100, gone["dEven"] % 100)
    assert report["served_gaps"] == 1
    assert report["served_gap_examples"] == [gone_day.isoformat()]
    assert report["closes_inserted"] == 157
    # The complete copy later stores the session in the middle of the history.
    full = ingest_fund_closes(engine, payload, now=CAPTURED)
    assert full["closes_inserted"] == 1 and full["served_gaps"] == 0
    # A weekly run served the short copy again writes nothing, so it names no
    # gap: the old ones are not reported every week.
    again = ingest_fund_closes(engine, short, now=CAPTURED)
    assert again["closes_inserted"] == 0 and again["served_gaps"] == 0


def test_without_a_stored_calendar_a_missing_session_still_refuses(engine):
    """No TEDPIX stored (the index ingest failed): nothing says the pair
    skipped a session, and the strict rule stands."""
    _seed_funds(engine)
    short, _ = _without_a_moving_session(load_fixture_json("tsetmc_fund_silver_daily.json"))
    with pytest.raises(FundRestated, match="restated the reference"):
        ingest_fund_closes(engine, short, now=CAPTURED)


def test_restatements_are_judged_only_where_a_run_writes(engine):
    """A reference restated between two STORED closes is history already
    written; re-judging the whole list every run is what refused a fund for
    good once. A restated reference on a session this run writes still
    refuses it."""
    _seed_funds(engine)
    payload = load_fixture_json("tsetmc_fund_silver_daily.json")
    evening = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)  # 2026-09-28 unsettled
    ingest_fund_closes(engine, payload, now=evening)
    old_split = copy.deepcopy(payload)
    rows = sorted(old_split["closingPriceDaily"], key=lambda r: r["dEven"])
    rows[100]["priceYesterday"] = rows[99]["pClosing"] / 2
    assert ingest_fund_closes(engine, old_split, now=evening)["closes_inserted"] == 0
    new_split = copy.deepcopy(payload)
    rows = sorted(new_split["closingPriceDaily"], key=lambda r: r["dEven"])
    rows[-1]["priceYesterday"] = rows[-2]["pClosing"] / 2
    with pytest.raises(FundRestated, match="this run would write"):
        ingest_fund_closes(engine, new_split, now=CAPTURED)
    assert _count(engine, prices) == 157


def test_fund_closes_stop_where_a_live_source_begins(engine):
    """AYAR and TALA have BrsApi's intraday mirror from 2026-07-21. A settled
    close written into those days is a second, different-source row per day —
    pClosing differs from the last trade by a median 0.32% on عیار — so, as in
    tgju_backfill, only sessions before the first other-source day are
    written."""
    _seed_funds(engine)
    live = datetime(2026, 9, 20, 9, 5, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(prices.insert().values(
            symbol="IR_SILVER_FUND_SILVER", value=1300.0, currency="IRT", unit="unit",
            source="tse_funds", observed_at=live, collected_at=live, quality="ok"))
    report = ingest_fund_closes(engine, load_fixture_json("tsetmc_fund_silver_daily.json"),
                                now=CAPTURED)
    assert report["live_era_from"] == live.isoformat()
    assert report["live_era_skipped"] > 0
    assert report["last_date"] < "2026-09-20"
    with engine.connect() as conn:
        days = [r.observed_at.date() for r in conn.execute(
            select(prices.c.observed_at).where(prices.c.source == "tsetmc_cdn"))]
    assert max(days) < date(2026, 9, 20)


def test_a_restated_reference_refuses_the_fund(engine):
    _seed_funds(engine)
    payload = load_fixture_json("tsetmc_fund_silver_daily.json")
    split = copy.deepcopy(payload)
    rows = sorted(split["closingPriceDaily"], key=lambda r: r["dEven"])
    rows[100]["priceYesterday"] = rows[99]["pClosing"] / 2
    with pytest.raises(FundRestated, match="restated the reference"):
        ingest_fund_closes(engine, split, now=CAPTURED)
    assert _count(engine, prices) == 0


def test_a_one_rial_reference_difference_is_rounding():
    """MEASURED: across the five funds' 5,846 session pairs the reference and
    the previous close differ on two, by exactly one rial."""
    bars = parse_daily_list(load_fixture_json("tsetmc_fund_silver_daily.json"))
    assert reference_restatements(bars) == []
    rows = sorted(copy.deepcopy(load_fixture_json("tsetmc_fund_silver_daily.json"))
                  ["closingPriceDaily"], key=lambda r: r["dEven"])
    assert rows[-1]["zTotTran"] > 0 and rows[-2]["zTotTran"] > 0  # two traded sessions
    rows[-1]["priceYesterday"] = rows[-2]["pClosing"] + 1
    assert reference_restatements(parse_daily_list({"closingPriceDaily": rows})) == []
    rows[-1]["priceYesterday"] = rows[-2]["pClosing"] + 40
    assert reference_restatements(parse_daily_list({"closingPriceDaily": rows})) == [
        (date(2026, 9, 28), rows[-2]["pClosing"], rows[-2]["pClosing"] + 40)]


def test_an_unregistered_or_disabled_fund_is_refused(engine):
    with pytest.raises(MarketParseError, match="not in commodity_funds"):
        ingest_fund_closes(engine, load_fixture_json("tsetmc_fund_silver_daily.json"))
    _seed_funds(engine)
    with engine.begin() as conn:
        conn.execute(commodity_funds.update().values(enabled=False))
    with pytest.raises(MarketParseError, match="disabled"):
        ingest_fund_closes(engine, load_fixture_json("tsetmc_fund_silver_daily.json"))
    assert fund_roster(engine) == [] and len(fund_roster(engine, include_disabled=True)) == 5


def test_fund_files_are_isolated(engine, tmp_path):
    _seed_funds(engine)
    good = _write(tmp_path, "fund-1.json", load_fixture_json("tsetmc_fund_silver_daily.json"))
    bad = tmp_path / "fund-2.json"
    bad.write_text("{", encoding="utf-8")
    report = ingest_fund_files(engine, [str(good), str(bad)], now=CAPTURED)
    assert report["succeeded"] == 1 and report["failed"] == 1 and SILVER in report["funds"]
    with pytest.raises(MarketIngestFailed):
        ingest_fund_files(engine, [str(bad)])
    with pytest.raises(ValueError, match="named no fund"):
        ingest_fund_files(engine, [])


# --- the endpoints ---------------------------------------------------------------------------


HEADERS = {"X-Internal-Token": TEST_TOKEN}


def test_the_share_endpoints(client, engine, tmp_path):
    _seed_sectors(engine)
    manifest = _manifest(tmp_path, flows=[(SDPT, load_fixture_json(
        "tsetmc_clienttype_farabourse_sdpt.json"))])
    resp = client.post("/internal/bourse/shares/ingest",
                       json={"manifest": manifest, "part": "universe"}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["universe"]["inserted"] == 15
    resp = client.post("/internal/bourse/shares/ingest",
                       json={"manifest": manifest, "part": "flows"}, headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["next_offset"] is None
    state = client.get("/internal/bourse/shares/state", headers=HEADERS).json()
    assert state["count"] == 15 and state["floor"] == "2025-03-21"


def test_the_share_endpoint_answers_400_for_the_call_and_names_every_failed_item(
    client, engine, tmp_path
):
    """A chunk in which every item failed used to answer 502, and busybox wget
    — the fetch's only way in — throws the body of a non-2xx answer away, so
    the run knew THAT the chunk failed and never WHICH items or why. It now
    answers 200 with the whole report, and the provider's health row still
    records the failure."""
    with engine.begin() as conn:
        conn.execute(data_providers.insert().values(
            code="tsetmc_cdn", name="TSETMC CDN", category="iran_equity"))
    manifest = _manifest(tmp_path, flows=[(SDPT, load_fixture_json(
        "tsetmc_clienttype_farabourse_sdpt.json"))])
    bad = client.post("/internal/bourse/shares/ingest",
                      json={"manifest": manifest, "part": "everything"}, headers=HEADERS)
    assert bad.status_code == 400
    missing = client.post("/internal/bourse/shares/ingest",
                          json={"manifest": str(tmp_path / "manifest.json"), "part": "flows"},
                          headers=HEADERS)
    assert missing.status_code == 400
    failed = client.post("/internal/bourse/shares/ingest",
                         json={"manifest": manifest, "part": "flows"}, headers=HEADERS)
    assert failed.status_code == 200
    body = failed.json()
    assert body["all_failed"] is True and body["succeeded"] == 0 and body["failed"] == 1
    assert body["errors"][0]["ins_code"] == SDPT
    assert "not in market_shares" in body["errors"][0]["message"]
    with engine.connect() as conn:
        health = conn.execute(select(data_providers.c.consecutive_failures, data_providers.c.last_error)
                              .where(data_providers.c.code == "tsetmc_cdn")).one()
    assert health.consecutive_failures >= 1 and "failed" in health.last_error


def test_the_fund_endpoints(client, engine, tmp_path):
    _seed_funds(engine)
    roster = client.get("/internal/bourse/funds/roster", headers=HEADERS).json()
    assert roster["count"] == 5
    assert {i["underlying"] for i in roster["items"]} == {"gold", "silver"}
    path = _write(tmp_path, "fund-1.json", load_fixture_json("tsetmc_fund_silver_daily.json"))
    ok = client.post("/internal/bourse/funds/ingest", json={"paths": [str(path)]},
                     headers=HEADERS)
    assert ok.status_code == 200, ok.text
    assert ok.json()["funds"][SILVER]["closes_inserted"] > 150
    assert client.post("/internal/bourse/funds/ingest", json={"paths": []},
                       headers=HEADERS).status_code == 400
    bad = tmp_path / "fund-2.json"
    bad.write_text("{", encoding="utf-8")
    failed = client.post("/internal/bourse/funds/ingest", json={"paths": [str(bad)]},
                         headers=HEADERS)
    assert failed.status_code == 200 and failed.json()["all_failed"] is True
    assert failed.json()["errors"][0]["path"] == str(bad)


# --- the fetch script's decisions ---------------------------------------------------------------


def _rows(dates, code="1"):
    return [{"recDate": int(d.strftime("%Y%m%d")), "insCode": code} for d in dates]


def _days(start, n):
    return [start + timedelta(days=i) for i in range(n)]


def test_trim_a_new_share_outside_the_roster_starts_at_the_floor(fetch):
    rows = _rows(_days(date(2025, 3, 15), 10))
    kept = fetch.trim_client_types(rows, last_stored=None, first_stored=None, overlap=10,
                                   floor=date(2025, 3, 21))
    assert [r["recDate"] for r in kept] == [20250324, 20250323, 20250322, 20250321]


def test_trim_a_new_roster_share_keeps_its_whole_history(fetch):
    rows = _rows(_days(date(2008, 1, 1), 5))
    kept = fetch.trim_client_types(rows, last_stored=None, first_stored=None, overlap=10,
                                   floor=None)
    assert len(kept) == 5 and kept[0]["recDate"] == 20080105  # newest first, as served


def test_trim_ships_what_is_new_plus_the_overlap(fetch):
    """The real فولاد3 history: 31 sessions. With everything up to 2026-09-21
    stored, what ships is the five newer sessions and three overlap rows."""
    rows = load_fixture_json("tsetmc_clienttype_board3_foolad3.json")["clientType"]
    original = copy.deepcopy(rows)
    kept = fetch.trim_client_types(rows, last_stored=date(2026, 9, 21),
                                   first_stored=date(2026, 8, 8), overlap=3,
                                   floor=date(2025, 3, 21))
    assert [r["recDate"] for r in kept] == [
        20260928, 20260927, 20260926, 20260923, 20260922,   # new
        20260921, 20260920, 20260919]                        # overlap
    assert rows == original  # pure


def test_trim_returns_none_when_nothing_is_new(fetch):
    rows = load_fixture_json("tsetmc_clienttype_board3_foolad3.json")["clientType"]
    assert fetch.trim_client_types(rows, last_stored=date(2026, 9, 28),
                                   first_stored=date(2026, 8, 8), overlap=10,
                                   floor=date(2025, 3, 21)) is None


def test_trim_backfills_a_share_that_joined_the_roster(fetch):
    """Floored while outside the roster, then enrolled: the older history the
    roster keeps is shipped, with nothing the server already has beyond the
    overlap."""
    rows = _rows(_days(date(2025, 3, 1), 30))
    kept = fetch.trim_client_types(rows, last_stored=date(2025, 3, 30),
                                   first_stored=date(2025, 3, 21), overlap=0, floor=None)
    assert [r["recDate"] for r in kept][-1] == 20250301
    assert {r["recDate"] for r in kept} == {int(d.strftime("%Y%m%d"))
                                            for d in _days(date(2025, 3, 1), 20)}


def test_trim_fills_a_non_roster_share_back_to_the_floor(fetch):
    """A run with --flows-since later than the server's floor stores a share
    from that later date only. The next run at the server's floor ships the
    sessions in between — and nothing before the floor — rather than leaving
    that stretch missing for every share outside the roster for good."""
    rows = _rows(_days(date(2025, 3, 1), 60))  # 2025-03-01 .. 2025-04-29
    kept = fetch.trim_client_types(rows, last_stored=date(2025, 4, 29),
                                   first_stored=date(2025, 4, 10), overlap=0,
                                   floor=date(2025, 3, 21))
    assert {r["recDate"] for r in kept} == {int(d.strftime("%Y%m%d"))
                                            for d in _days(date(2025, 3, 21), 20)}
    # Stored from the floor already: nothing older is owed, so nothing ships.
    assert fetch.trim_client_types(rows, last_stored=date(2025, 4, 29),
                                   first_stored=date(2025, 3, 21), overlap=0,
                                   floor=date(2025, 3, 21)) is None


def test_a_history_that_repeats_a_session_is_refused_not_collapsed(fetch):
    """The ingest refuses a history with two rows for one session. Trimming
    keys rows by date, so a duplicate that reached it would be collapsed to
    one row silently and the ingest would never see it; the fetch refuses it
    first, for that share only."""
    doubled = copy.deepcopy(load_fixture_json("tsetmc_clienttype_board3_foolad3.json"))
    twin = copy.deepcopy(doubled["clientType"][3])
    twin["buy_I_Value"] += 1_000
    doubled["clientType"].insert(4, twin)
    with pytest.raises(MarketParseError):
        parse_client_types(doubled)
    with pytest.raises(fetch.FetchError, match="twice"):
        fetch.check_client_types(doubled, FOOLAD3)


def test_a_market_watch_listing_a_share_twice_is_refused_as_the_ingest_refuses_it(fetch):
    """The ingest refuses such a watch. Shipped anyway, the share's flow file
    would be named twice in the manifest, which the service refuses — every
    chunk of the flows part, i.e. the whole market's money flow for the run."""
    payload = load_fixture_json("tsetmc_market_watch_subset.json")
    twice = copy.deepcopy(payload)
    twice["marketwatch"].append(
        copy.deepcopy(next(r for r in payload["marketwatch"] if r["insCode"] == SDPT)))
    with pytest.raises(MarketParseError, match="twice"):
        parse_market_watch(twice)
    with pytest.raises(fetch.FetchError, match="twice"):
        fetch.share_codes(twice)


def test_an_empty_history_for_a_share_the_server_holds_is_a_failure(fetch, tmp_path,
                                                                    monkeypatch, capsys):
    """An empty list is the honest answer for a listing that never traded, and
    a bad response for a share with 3,850 stored sessions: the first is a
    notice, the second a failure that makes the run exit 1."""
    monkeypatch.setattr(fetch, "_curl", lambda url, timeout=120: b'{"clientType": []}')
    for name in ("flows", "evidence"):
        (tmp_path / name).mkdir()
    failures = fetch.Failures()
    held = {FOOLAD: {"roster": True, "flow_count": 3850, "flow_first_date": "2008-11-26",
                     "flow_last_date": "2026-09-28"}}
    written, dates, counts = fetch.download_share_flows(
        [(FOOLAD, "IRO1FOLD0001", "فولاد"), (MOMS, "IRO5MOMS0001", "مهرمام")], held,
        date(2025, 3, 21), tmp_path / "flows", tmp_path / "evidence", failures)
    assert written == [] and dates == {} and counts["empty"] == 1
    assert [(kind, key.split()[0]) for kind, key, _ in failures.items] == [("flows", FOOLAD)]
    out = capsys.readouterr().out
    assert f"NOTICE {MOMS}" in out and f"NOTICE {FOOLAD}" not in out


def test_an_empty_daily_list_for_a_fund_with_stored_closes_is_a_failure(fetch, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(fetch, "_curl",
                        lambda url, timeout=120: b'{"closingPriceDaily": []}')
    failures = fetch.Failures()
    written = fetch.download_funds(
        [{"ins_code": SILVER, "symbol_fa": "سیلور", "close_count": 158},
         {"ins_code": "33761569293467411", "symbol_fa": "سیمین", "close_count": 0}],
        tmp_path, failures)
    assert written == []
    assert [(kind, key.split()[0]) for kind, key, _ in failures.items] == [("fund", SILVER)]


def test_the_session_calendar_is_read_off_the_histories(fetch):
    busy = {str(i): [date(2026, 9, 27), date(2026, 9, 28)] for i in range(250)}
    busy.update({str(1000 + i): [date(2026, 9, 25)] for i in range(10)})  # a Friday blip
    busy["x"] = [date(2025, 1, 5)]
    sessions = fetch.market_sessions(busy, floor=date(2025, 3, 21), before=date(2026, 9, 28))
    assert sessions == [date(2026, 9, 27)]  # the 28th is not settled yet


def test_the_day_files_to_fetch_are_the_missing_ones_plus_two(fetch):
    stored = [date(2026, 9, d) for d in (20, 21, 22, 23)]
    sessions = stored + [date(2026, 9, 26), date(2026, 9, 27)]
    assert fetch.day_files_to_fetch(sessions, stored) == [
        date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 26), date(2026, 9, 27)]
    assert fetch.day_files_to_fetch([], []) == []
    # A stored day the server says was ingested before some of its shares
    # were in the universe is fetched again.
    assert fetch.day_files_to_fetch(stored, stored, recheck=[date(2026, 9, 20)]) == [
        date(2026, 9, 20), date(2026, 9, 22), date(2026, 9, 23)]


def test_the_script_settles_sessions_exactly_as_the_ingest_does(fetch):
    for instant in (
        datetime(2026, 9, 29, 8, 9, tzinfo=timezone.utc),
        datetime(2026, 9, 29, 11, 29, tzinfo=timezone.utc),
        datetime(2026, 9, 29, 11, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc),
    ):
        assert fetch.settled_cutoff(instant) == settled_cutoff(instant), instant


def test_the_scripts_share_rule_is_the_ingests(fetch):
    payload = load_fixture_json("tsetmc_market_watch_subset.json")
    assert set(fetch.SHARE_PREFIXES) == set(SHARE_MARKETS)
    assert {c for c, _, _ in fetch.share_codes(payload)} == {
        s.ins_code for s in parse_market_watch(payload).shares}


def test_client_type_shape_checks_replace_the_size_floor(fetch):
    board3 = load_fixture_json("tsetmc_clienttype_board3_foolad3.json")
    assert len(fetch.check_client_types(board3, FOOLAD3)) == 31  # 9.6 KB, once refused
    assert fetch.check_client_types({"clientType": []}, FOOLAD3) == []
    with pytest.raises(fetch.FetchError, match="another's code"):
        fetch.check_client_types(board3, FOOLAD)
    broken = copy.deepcopy(board3)
    broken["clientType"][0]["recDate"] = 20261399
    with pytest.raises(fetch.FetchError):
        fetch.check_client_types(broken, FOOLAD3)


def test_a_failed_download_is_an_exception_a_loop_can_catch(fetch, monkeypatch):
    """SystemExit is a BaseException: `except Exception` around one of 1,160
    downloads would not catch it and the first 502 would end the run."""
    class Result:
        returncode, stdout, stderr = 56, b"", b"curl: (56) Recv failure"

    monkeypatch.setattr(fetch.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setattr(fetch.PACER, "interval", 0.0)
    caught = []
    for code in ("1", "2"):
        try:
            fetch.fetch_json(fetch.CLIENT_TYPE_PATH.format(ins_code=code), "clientType")
        except Exception as exc:  # noqa: BLE001 - exactly what the loop does
            caught.append(exc)
    assert len(caught) == 2 and all(isinstance(e, fetch.FetchError) for e in caught)
    with pytest.raises(SystemExit):  # the roster downloads still stop the run
        fetch.download("1", fetch.Path("."))


def test_the_scripts_manifest_is_the_one_the_service_reads(fetch, tmp_path):
    manifest = fetch.build_manifest(["flow-9.json", "flow-10.json"], ["day-20260928.json"],
                                    floor=date(2025, 3, 21), has_watch=True, has_static=False)
    path = _write(tmp_path, "manifest.json", manifest)
    loaded = load_manifest(str(path))
    assert [code for code, _ in loaded.flows] == ["10", "9"]
    assert loaded.days[0][0] == date(2026, 9, 28)
    assert loaded.market_watch.endswith("market-watch.json") and loaded.static_data is None


def test_the_script_deletes_only_what_it_generated(fetch, tmp_path):
    for name in ("flow-1.json", "flow-x.json", "notes.txt", "manifest.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    fetch._clear_generated(tmp_path, fetch.FLOW_NAME_RE, fetch.SHARE_FIXED_NAMES)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["flow-x.json", "notes.txt"]


def test_the_request_rate_cannot_exceed_three_a_second(fetch):
    with pytest.raises(Exception):
        fetch._positive_delay("0.2")
    assert fetch._positive_delay("0.34") == pytest.approx(0.34)
    assert fetch.DEFAULT_DELAY >= fetch.MIN_REQUEST_INTERVAL


def test_the_default_spacing_is_half_a_second(fetch):
    """About two requests a second: the spacing every other TSETMC and TGJU
    request in this repo keeps. The floor stays for an operator who asks."""
    assert fetch.DEFAULT_DELAY == 0.5
    assert fetch.Pacer().interval == 0.5


def test_every_ingest_error_reaches_the_runs_summary(fetch, capsys):
    """The bars and market ingests used to print their per-item errors inline
    only: run B's 4 bar and 64 index failures were not in its FAILED list."""
    failures, restated, contradictions = fetch.Failures(), fetch.Restated(), []
    fetch._print_bars_report({"symbols": {}, "errors": [
        {"path": "/tmp/tsetmc-x/1.json", "error": "BarParseError", "message": "no"}]},
        failures, restated)
    fetch._print_market_report({"indices": {}, "errors": [
        {"kind": "index_history", "path": "/tmp/i-1.json", "error": "MarketParseError",
         "message": "bad"},
        {"kind": "index_history", "path": "/tmp/i-2.json", "error": "IndexContradiction",
         "message": "unlike"}]}, failures, restated, contradictions)
    fetch._print_universe_report({"universe": {}, "errors": [
        {"kind": "static_data", "name": "static-data.json", "error": "MarketParseError",
         "message": "no IndustrialGroup rows"}]}, failures)
    assert [(k, key) for k, key, _ in failures.items] == [
        ("ingest bars", "/tmp/tsetmc-x/1.json"), ("ingest index_history", "/tmp/i-1.json"),
        ("ingest static_data", "static-data.json")]
    assert [key for _, key, _ in contradictions] == ["/tmp/i-2.json"]


def test_a_chunk_in_which_every_item_failed_names_every_item(fetch):
    """The service answers 200 with the whole report when every item of a chunk
    failed; each item is a failure of the run, with its reason."""
    failures, restated, contradictions = fetch.Failures(), fetch.Restated(), []

    def post(url, body):
        return {"items": {}, "succeeded": 0, "failed": 2, "all_failed": True,
                "next_offset": None, "errors": [
                    {"kind": "sessions", "name": "day-20260927.json", "trade_date": "2026-09-27",
                     "error": "WrongSessionFile", "message": "some other session"},
                    {"kind": "sessions", "name": "day-20260928.json", "trade_date": "2026-09-28",
                     "error": "SessionContradiction", "message": "unlike"}]}

    fetch._ingest_chunks(post, "sessions", 2, 20, "m", failures, contradictions, restated)
    assert [(k, key) for k, key, _ in failures.items] == [("ingest sessions", "2026-09-27")]
    assert [key for _, key, _ in contradictions] == ["2026-09-28"]


def test_only_a_run_copy_is_ever_removed_from_the_container(fetch, monkeypatch):
    commands = []
    monkeypatch.setattr(fetch, "_ssh", lambda host, command, capture=False: commands.append(command))
    fetch._remove_container_copy("h", "/tmp/tsetmc-20260929T122625Z")
    assert "rm -rf -- /tmp/tsetmc-20260929T122625Z" in commands[0]
    for bad in ("/tmp", "/tmp/tsetmc-x", "/tmp/tsetmc-20260929T122625Z/..", "/"):
        with pytest.raises(fetch.RemoteError):
            fetch._remove_container_copy("h", bad)
    assert len(commands) == 1


def test_the_pacer_spaces_request_starts(fetch, monkeypatch):
    clock = {"now": 100.0}
    slept = []
    monkeypatch.setattr(fetch.time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock["now"] += seconds

    monkeypatch.setattr(fetch.time, "sleep", sleep)
    pacer = fetch.Pacer(0.35)
    pacer.wait()              # the first request never waits
    clock["now"] += 0.1       # a fast response
    pacer.wait()
    clock["now"] += 0.5       # a slow one already used the gap
    pacer.wait()
    assert slept == [0.25] and pacer.requests == 3


# --- the fetch, end to end against the service -------------------------------------------------
#
# TSETMC is replaced by the fixtures, ssh/scp by a local directory, and the
# internal calls go to the real FastAPI app over the TestClient — so what the
# script writes is what the service reads, file names and manifest included.


class _FakeServer:
    def __init__(self, fetch, client, tmp_path):
        self.fetch, self.client = fetch, client
        self.remote = tmp_path / "server"
        self.remote.mkdir()
        self.run_dir = None
        self.posts = []
        self.commands = []

    def internal_get(self, host, url, what):
        resp = self.client.get(url.split(":8500", 1)[1], headers=HEADERS)
        assert resp.status_code == 200, (what, resp.text)
        return resp.json()

    def subprocess_run(self, argv, **kwargs):
        import shutil

        self.commands.append(list(argv))
        if argv[0] == "scp":
            *sources, target = argv[4:]
            self.run_dir = self.remote / target.rsplit("/", 2)[-2]
            self.run_dir.mkdir(exist_ok=True)
            for source in sources:
                if os.path.isdir(source):
                    shutil.copytree(source, self.run_dir / os.path.basename(source),
                                    dirs_exist_ok=True)
                else:
                    shutil.copy(source, self.run_dir)

        class Done:
            returncode, stdout, stderr = 0, b"", b""

        return Done()

    def post_ingest(self, host, url, body, remote_dir, container_dir, copy_first):
        self.posts.append((url.rsplit("/internal/", 1)[1], body))
        local = json.loads(json.dumps(body).replace(container_dir, str(self.run_dir)))
        resp = self.client.post(url.split(":8500", 1)[1], json=local, headers=HEADERS)
        if resp.status_code != 200:
            raise self.fetch.RemoteError(f"HTTP {resp.status_code}")
        return resp.json()


def _tsetmc(fetch, broken=()):
    """Serve the fixtures by URL, as cdn.tsetmc.com would."""
    newest = load_fixture_json("tsetmc_clienttype_newest_sessions.json")
    by_path = {
        fetch.MARKET_WATCH_PATH: load_fixture_json("tsetmc_market_watch_subset.json"),
        fetch.STATIC_DATA_PATH: load_fixture_json_gz("tsetmc_static_data_industrial_groups.json.gz"),
        fetch.DAY_FILE_PATH.format(yyyymmdd="20260928"): None,  # served as raw bytes
        fetch.BARS_PATH.format(ins_code=SILVER): load_fixture_json("tsetmc_fund_silver_daily.json"),
        "/api/Index/GetIndexB1LastAll/All/1": load_fixture_json("tsetmc_index_live_bourse.json"),
        "/api/Index/GetIndexB1LastAll/All/2": load_fixture_json("tsetmc_index_live_farabourse.json"),
        "/api/MarketData/GetMarketValueByFlow/1/9999":
            load_fixture_json_gz("tsetmc_market_value_bourse.json.gz"),
        "/api/MarketData/GetMarketValueByFlow/2/9999":
            load_fixture_json_gz("tsetmc_market_value_bourse.json.gz"),
        "/api/MarketData/GetMarketOverview/1": load_fixture_json("tsetmc_overview_bourse.json"),
        "/api/MarketData/GetMarketOverview/2": load_fixture_json("tsetmc_overview_farabourse.json"),
        "/api/MarketData/GetSectorsSummary": load_fixture_json("tsetmc_sectors_summary.json"),
        fetch.INDEX_HISTORY_PATH.format(ins_code="32097828799138957"):
            load_fixture_json_gz("tsetmc_index_tedpix.json.gz"),
    }
    day_bytes = _read(os.path.join(os.path.dirname(__file__), "fixtures",
                                   "tsetmc_day_20260928_subset.json")).encode("utf-8")

    def curl(url, timeout=120):
        path = url[len(fetch.BASE):]
        match = re.fullmatch(r"/api/ClientType/GetClientTypeHistory/(\d+)", path)
        if match:
            code = match.group(1)
            if code in broken:
                raise fetch.FetchError(f"fetch failed for {url}\ncurl exit 56: Recv failure")
            return json.dumps(newest.get(code, {"clientType": []})).encode("utf-8")
        if path == fetch.DAY_FILE_PATH.format(yyyymmdd="20260928"):
            return day_bytes
        if path not in by_path:
            raise fetch.FetchError(f"fetch failed for {url}\ncurl exit 22: 404")
        return json.dumps(by_path[path], ensure_ascii=False).encode("utf-8")

    return curl


def _deployment(engine):
    """The rows a deployed 0030 carries that these tests need."""
    _seed_sectors(engine)
    _seed_funds(engine)
    with engine.begin() as conn:
        conn.execute(equity_instruments.insert().values(
            ins_code=FOOLAD, symbol_fa="فولاد", name_fa="فولاد مبارکه اصفهان", market="bourse",
            isin="IRO1FOLD0009"))
        conn.execute(market_shares.insert().values(
            ins_code=FOOLAD, symbol_fa="فولاد", name_fa="فولاد مبارکه اصفهان", market="bourse",
            board="main", company_code="IRO1FOLD", sector_code="27", listed=False))
        conn.execute(market_indices.insert().values(
            ins_code="32097828799138957", name_fa="شاخص کل", market="bourse", kind="headline"))
        # Only سیلور's daily list is a fixture; the other funds are switched
        # off rather than left to fail their downloads.
        conn.execute(commodity_funds.update().where(commodity_funds.c.ins_code != SILVER)
                     .values(enabled=False))


def _args(tmp_path, **overrides):
    import argparse

    values = dict(dry_run=False, host="test", out=str(tmp_path / "out"), ins_code=[],
                  delay=0.34, flows_since=date(2026, 9, 28), no_shares=False,
                  bars_only=False, market_only=True)
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture()
def wired(fetch, client, engine, tmp_path, monkeypatch):
    server = _FakeServer(fetch, client, tmp_path)
    monkeypatch.setattr(fetch, "_internal_get", server.internal_get)
    monkeypatch.setattr(fetch.subprocess, "run", server.subprocess_run)
    monkeypatch.setattr(fetch, "_post_ingest", server.post_ingest)
    monkeypatch.setattr(fetch, "MIN_MARKET_WATCH_SHARES", 10)
    monkeypatch.setattr(fetch, "SESSION_MIN_SHARES", 5)
    monkeypatch.setattr(fetch, "FLOW_CHUNK", 3)
    monkeypatch.setattr(fetch, "SESSION_CHUNK", 1)
    _deployment(engine)
    return server


def test_a_full_run_ships_what_the_service_ingests(fetch, wired, engine, tmp_path,
                                                   monkeypatch, capsys):
    """The default run, one share's download failing: every other share, the
    day file and the fund still land, the failure is named, and the exit is 1."""
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch, broken={ARMP}))
    exit_code = fetch.run(_args(tmp_path))
    out = capsys.readouterr().out
    assert exit_code == 1
    assert f"FAILED flows {ARMP}" in out and "curl exit 56" in out
    assert "TSETMC serves no money-flow rows" in out  # the shares with no history here
    # The universe, then flows in chunks of three, then one session per call.
    parts = [b.get("part") for url, b in wired.posts if url == "bourse/shares/ingest"]
    assert parts == ["universe", "flows", "flows", "sessions"]
    assert _count(engine, market_shares) == 15 and _share(engine, FOOLAD)["listed"] is True
    flows = {code: _share(engine, code)["flow_count"] for code in load_fixture_json(
        "tsetmc_clienttype_newest_sessions.json")}
    # The roster keeps its whole (here: five-row) history; every other new
    # share is floored at --flows-since; the broken one has nothing.
    assert flows[FOOLAD] == 5 and flows[ARMP] == 0
    assert {c: n for c, n in flows.items() if c not in (FOOLAD, ARMP)} == dict.fromkeys(
        [c for c in flows if c not in (FOOLAD, ARMP)], 1)
    assert _count(engine, market_share_sessions) == 15
    assert _count(engine, prices, prices.c.symbol == "IR_SILVER_FUND_SILVER") == 158
    # Evidence kept locally: the untrimmed histories, compressed.
    evidence = tmp_path / "out" / "evidence"
    assert (evidence / f"clienttype-{FOOLAD}.json.gz").is_file()
    assert "timings" in out


def test_a_run_removes_its_container_copy_and_keeps_the_archive(fetch, wired, tmp_path,
                                                                monkeypatch, capsys):
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch))
    assert fetch.run(_args(tmp_path)) == 0
    removals = [c for c in wired.commands if c[0] == "ssh" and "rm -rf" in c[-1]]
    assert len(removals) == 1 and "/tmp/tsetmc-" in removals[0][-1]
    assert "backups/tsetmc" not in removals[0][-1]
    assert wired.run_dir is not None and any(wired.run_dir.iterdir())  # the archive
    assert "cleanup    : removed /tmp/tsetmc-" in capsys.readouterr().out


def test_a_restated_row_is_listed_and_does_not_fail_the_run(fetch, wired, engine, tmp_path,
                                                            monkeypatch, capsys):
    """The second copy of 2026-09-28 differs by one trade on one share: the
    stored row is kept, the day is not failed, and the run exits 0 with the
    restatement listed."""
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch))
    assert fetch.run(_args(tmp_path)) == 0
    capsys.readouterr()
    served = _tsetmc(fetch)
    other = copy.deepcopy(load_fixture_json("tsetmc_day_20260928_subset.json"))
    for row in other["closingPriceDailyHistoryWithInstDetails"]:
        if row["insCode"] == int(FOOLAD):
            row["zTotTran"] += 1

    def second_copy(url, timeout=120):
        if url.endswith(fetch.DAY_FILE_PATH.format(yyyymmdd="20260928")):
            return json.dumps(other, ensure_ascii=False).encode("utf-8")
        return served(url, timeout)

    monkeypatch.setattr(fetch, "_curl", second_copy)
    assert fetch.run(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "RESTATED (1)" in out and f"[sessions] 2026-09-28: 1 stored row(s) kept" in out
    assert "FAILED" not in out


def test_a_second_run_ships_only_what_is_new(fetch, wired, engine, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch))
    assert fetch.run(_args(tmp_path)) == 0
    wired.posts.clear()
    assert fetch.run(_args(tmp_path)) == 0
    flow_files = sorted(os.listdir(tmp_path / "out" / "shares" / "flows"))
    # Nothing is newer than what is stored, so no flow file ships at all, and
    # the only day file is the overlap re-fetch of the newest stored session.
    assert flow_files == []
    assert sorted(os.listdir(tmp_path / "out" / "shares" / "days")) == ["day-20260928.json"]
    parts = [b.get("part") for url, b in wired.posts if url == "bourse/shares/ingest"]
    assert parts == ["universe", "sessions"]
    assert _count(engine, market_share_sessions) == 15


def test_a_dry_run_says_what_it_would_fetch_and_ships_nothing(fetch, wired, engine, tmp_path,
                                                              monkeypatch, capsys):
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch))
    assert fetch.run(_args(tmp_path, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "would fetch: 15 money-flow histories (1 roster" in out
    assert "dry run:" in out and wired.posts == [] and wired.run_dir is None
    assert _count(engine, market_shares) == 1  # the deployment's seed, untouched


def test_no_shares_is_the_0029_roster_path(fetch, wired, engine, tmp_path, monkeypatch):
    """--no-shares: no universe, no day files; the roster's history goes to
    /internal/bourse/ingest as before, now against market_shares."""
    curl = _tsetmc(fetch)
    foolad = load_fixture_json("tsetmc_clienttype_foolad_trimmed.json")

    def roster_aware(url, timeout=120):
        if url.endswith(f"/GetClientTypeHistory/{FOOLAD}"):
            return json.dumps(foolad).encode("utf-8")
        return curl(url, timeout)

    monkeypatch.setattr(fetch, "_curl", roster_aware)
    monkeypatch.setattr(fetch, "fetch_roster", lambda host: [
        {"ins_code": FOOLAD, "symbol_fa": "فولاد", "bar_count": 0, "last_bar": None}])
    assert fetch.run(_args(tmp_path, no_shares=True)) == 0
    urls = [url for url, _ in wired.posts]
    assert "bourse/shares/ingest" not in urls and "bourse/ingest" in urls
    assert _share(engine, FOOLAD)["flow_count"] == 125


def test_a_failed_market_ingest_does_not_cost_the_shares_or_the_funds(fetch, wired, engine,
                                                                      tmp_path, monkeypatch,
                                                                      capsys):
    """/internal/bourse/ingest answering 502 — every index, value and snapshot
    failed — is one failed item of the run, not its end: the universe, every
    share's flows, the day file and the funds are still ingested, the failure
    is named, and the exit is 1."""
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch))
    through = fetch._post_ingest

    def market_down(host, url, body, remote_dir, container_dir, copy_first):
        if url == fetch.MARKET_INGEST_URL:
            raise fetch.RemoteError("ssh command failed on test (exit 1): wget: server "
                                    "returned error: HTTP/1.1 502 Bad Gateway")
        return through(host, url, body, remote_dir, container_dir, copy_first)

    monkeypatch.setattr(fetch, "_post_ingest", market_down)
    assert fetch.run(_args(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "[ingest market]" in out and "502 Bad Gateway" in out
    assert _count(engine, market_shares) == 15
    assert _count(engine, market_share_sessions) == 15
    assert _count(engine, prices, prices.c.symbol == "IR_SILVER_FUND_SILVER") == 158


def test_a_day_the_server_calls_incomplete_is_fetched_again(fetch, wired, engine, tmp_path,
                                                            monkeypatch):
    """Without the overlap re-fetch, a stored day is fetched again only when
    the server's state names it: here a share first seen after the day was
    ingested, with a traded flow row that day and no session row."""
    monkeypatch.setattr(fetch, "_curl", _tsetmc(fetch))
    assert fetch.run(_args(tmp_path)) == 0
    monkeypatch.setattr(fetch, "DAY_OVERLAP", 0)
    wired.posts.clear()
    assert fetch.run(_args(tmp_path)) == 0
    assert os.listdir(tmp_path / "out" / "shares" / "days") == []
    # The run ingests at the wall clock, so the story is told relative to it.
    ingested = datetime.now(timezone.utc) - timedelta(hours=2)
    with engine.begin() as conn:
        conn.execute(market_session_files.update().values(ingested_at=ingested))
        conn.execute(market_shares.insert().values(
            ins_code="12345678", symbol_fa="نبود", market="bourse", board="main",
            first_seen_at=ingested + timedelta(hours=1)))
    flows = _newest(FOOLAD)
    for row in flows["clientType"]:
        row["insCode"] = "12345678"
    ingest_client_flows(engine, flows, now=CAPTURED)
    assert fetch.run(_args(tmp_path)) == 0
    assert os.listdir(tmp_path / "out" / "shares" / "days") == ["day-20260928.json"]
    assert share_state(engine)["session_dates_incomplete"] == []
