"""The Tehran market data layer: parsers, the power-of-ten correction, the ingest.

THE FIXTURES ARE TSETMC'S OWN RESPONSES
---------------------------------------
Captured 2026-09-29 around 11:09 Tehran time, with the session open:

=====================================  ======================================
tsetmc_index_second_market.json.gz     GetIndexB2History/71704845530629737,
                                       whole (sha256 of the decompressed
                                       bytes 7b9ae20e…c4cc79).  Stored /10
                                       since 2026-08-16.
tsetmc_index_financial.json.gz         GetIndexB2History/61247168213690670,
                                       whole (3f26cdb7…64e2).  Two one-day
                                       /10 excursions in May 2020.
tsetmc_index_furniture.json.gz         GetIndexB2History/29331053506731535,
                                       whole (30fea293…ff78).  Four zeros.
tsetmc_index_tedpix.json.gz            GetIndexB2History/32097828799138957,
                                       whole (c2e0a032…fe17).
tsetmc_index_live_{bourse,farabourse}  GetIndexB1LastAll/All/{1,2}, whole.
tsetmc_market_value_bourse.json.gz     GetMarketValueByFlow/1/9999, whole.
tsetmc_clienttype_foolad_trimmed.json  GetClientTypeHistory/46348559193224090,
                                       TRIMMED to the newest 120 sessions plus
                                       the three whose buy and sell totals
                                       disagree (2020-01-13, 2020-05-04,
                                       2020-05-13) — a parser test needs rows,
                                       not all 3,850 of them.
tsetmc_overview_{bourse,farabourse}    GetMarketOverview/{1,2}, whole.
tsetmc_sectors_summary.json            GetSectorsSummary, whole.
=====================================  ======================================

The index histories are whole, not trimmed, for the reason the فولاد bar
fixture is: the correction anchors on the scale MOST rows share, and a slice
changes that count.
"""
from __future__ import annotations

import copy
import math
import os
import re
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from app.bourse.ingest import (
    IndexContradiction,
    MarketIngestFailed,
    index_roster,
    ingest_client_flows,
    ingest_index_history,
    ingest_market_files,
    ingest_market_values,
    ingest_snapshot,
    restatement_is_systematic,
)
from app.bourse.parse import (
    MarketParseError,
    parse_client_types,
    parse_index_history,
    parse_index_live,
    parse_market_overview,
    parse_market_values,
    parse_sector_summary,
)
from app.bourse.scale import (
    CHECK_VERSION,
    STATUS_REFUSED,
    STATUS_VALIDATED,
    scale_exponents,
    verdict,
)
from app.db import (
    equity_client_flows,
    equity_instruments,
    market_index_checks,
    market_index_values,
    market_indices,
    market_shares,
    market_snapshots,
    market_values,
    sector_breadth_snapshots,
)

from .conftest import TEST_TOKEN, load_fixture_json, load_fixture_json_gz

SECOND_MARKET = "71704845530629737"
FINANCIAL = "61247168213690670"
FURNITURE = "29331053506731535"
TEDPIX = "32097828799138957"
FOOLAD = "46348559193224090"

MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "database", "migrations", "0029_tehran_market.up.sql",
)


def _live():
    out = {}
    out.update(parse_index_live(load_fixture_json("tsetmc_index_live_bourse.json")))
    out.update(parse_index_live(load_fixture_json("tsetmc_index_live_farabourse.json")))
    return out


def _register_index(engine, code, name="شاخص", market="bourse", kind="headline"):
    with engine.begin() as conn:
        conn.execute(
            market_indices.insert().values(
                ins_code=code, name_fa=name, name_en="", market=market, kind=kind
            )
        )


def _register_foolad(engine):
    """The roster row, and the universe row migration 0030 seeds from it: since
    0030 the money flow's key is market_shares, which carries the roster."""
    with engine.begin() as conn:
        conn.execute(
            equity_instruments.insert().values(
                ins_code=FOOLAD, symbol_fa="فولاد", name_fa="فولاد مبارکه اصفهان",
                market="bourse",
            )
        )
        conn.execute(
            market_shares.insert().values(
                ins_code=FOOLAD, symbol_fa="فولاد", name_fa="فولاد مبارکه اصفهان",
                market="bourse", board="main", company_code="IRO1FOLD", sector_code="27",
                listed=False,
            )
        )


def _toy_index(code, closes, start=date(2024, 1, 6)):
    """A GetIndexB2History-shaped payload over consecutive calendar days."""
    rows = []
    for i, c in enumerate(closes):
        d = date.fromordinal(start.toordinal() + i)
        rows.append({
            "insCode": int(code), "dEven": int(d.strftime("%Y%m%d")),
            "xNivInuClMresIbs": c, "xNivInuPbMresIbs": c, "xNivInuPhMresIbs": c,
        })
    return {"indexB2": rows}


# --- the registry seed -------------------------------------------------------


def test_the_seed_is_exactly_tsetmcs_index_list():
    """Every index TSETMC listed on both flows is seeded, and nothing else.

    The registry is data, and a seed that silently omitted an index would make
    the fetch skip it forever with nothing saying so.
    """
    sql = open(MIGRATION, encoding="utf-8").read()
    seeded = set(re.findall(r"^\s*\('(\d+)','", sql, flags=re.M))
    listed = set(_live())
    assert len(seeded) == 71
    assert seeded == listed


def test_sector_codes_come_only_from_tsetmcs_own_labels():
    """A bourse sector index's code is the number in its own label; the
    Farabourse sector indices carry none, and are given none."""
    sql = open(MIGRATION, encoding="utf-8").read()
    rows = re.findall(
        r"^\s*\('(\d+)','([^']*)','[^']*','(\w+)','(\w+)','(\d*)'", sql, flags=re.M
    )
    assert len(rows) == 71
    for code, name, market, kind, sector in rows:
        digits = re.findall(r"\d{2}", name)
        if market == "bourse" and kind == "sector":
            assert sector and sector in digits, (code, name, sector)
        else:
            assert sector == "", (code, name, sector)


# --- parsing -----------------------------------------------------------------


def test_the_index_fixture_is_the_whole_response():
    history = parse_index_history(load_fixture_json_gz("tsetmc_index_second_market.json.gz"))
    assert history.ins_code == SECOND_MARKET
    assert len(history.values) == 4294
    assert history.values[0].trade_date == date(2008, 12, 4)
    assert history.values[-1].trade_date == date(2026, 9, 28)


def test_low_and_high_are_kept_but_are_not_a_band():
    """409 of TEDPIX's rows close outside the served [low, high]. The parser
    keeps them — they are what was served — and refuses nothing over it."""
    history = parse_index_history(load_fixture_json_gz("tsetmc_index_tedpix.json.gz"))
    outside = [
        v for v in history.values
        if v.low is not None and v.high is not None and not (v.low <= v.close <= v.high)
    ]
    assert len(history.values) == 4294
    assert len(outside) == 409


def test_a_nonpositive_close_is_dropped_and_counted():
    history = parse_index_history(load_fixture_json_gz("tsetmc_index_furniture.json.gz"))
    assert history.dropped_nonpositive == 4
    assert history.dropped_dates == [
        date(2011, 2, 6), date(2011, 7, 23), date(2012, 8, 25), date(2013, 8, 4),
    ]
    assert all(v.close > 0 for v in history.values)


def test_an_index_payload_for_another_code_is_refused():
    payload = load_fixture_json_gz("tsetmc_index_financial.json.gz")
    with pytest.raises(MarketParseError, match="asked for index"):
        parse_index_history(payload, ins_code=SECOND_MARKET)


def test_a_mixed_index_file_is_refused():
    payload = load_fixture_json_gz("tsetmc_index_financial.json.gz")
    payload["indexB2"][5]["insCode"] = int(SECOND_MARKET)
    with pytest.raises(MarketParseError, match="exactly one insCode"):
        parse_index_history(payload)


def test_a_duplicated_session_is_refused():
    payload = _toy_index("123", [100.0, 101.0, 102.0])
    payload["indexB2"].append(copy.deepcopy(payload["indexB2"][1]))
    with pytest.raises(MarketParseError, match="twice"):
        parse_index_history(payload)


def test_an_unrecognised_shape_is_refused():
    with pytest.raises(MarketParseError, match="indexB2"):
        parse_index_history({"closingPriceDaily": []})


def test_live_values_parse_for_both_flows():
    live = _live()
    assert len(live) == 71
    assert live[TEDPIX].value == pytest.approx(7591547.65)
    assert live[SECOND_MARKET].value == pytest.approx(13310356.3)
    # Folded to Persian orthography, like every other TSETMC label here.
    assert live[TEDPIX].name_fa == "شاخص کل"


def test_market_values_parse_ascending():
    parsed = parse_market_values(
        load_fixture_json_gz("tsetmc_market_value_bourse.json.gz"), "bourse"
    )
    assert len(parsed.values) == 1615
    assert parsed.values[0][0] == date(2019, 12, 24)
    assert parsed.values[-1][0] == date(2026, 9, 28)


def test_client_type_counts_arrive_as_floats_and_are_kept_whole():
    parsed = parse_client_types(load_fixture_json("tsetmc_clienttype_foolad_trimmed.json"))
    newest = parsed.rows[-1]
    assert parsed.ins_code == FOOLAD
    assert newest["trade_date"] == date(2026, 9, 28)
    assert newest["buy_i_count"] == 2549 and isinstance(newest["buy_i_count"], int)
    assert newest["buy_i_value"] == 5457307657210.0


def test_a_fractional_count_is_refused():
    payload = load_fixture_json("tsetmc_clienttype_foolad_trimmed.json")
    payload["clientType"][0]["buy_I_Count"] = 12.5
    with pytest.raises(MarketParseError, match="whole count"):
        parse_client_types(payload)


def test_a_negative_amount_is_refused():
    payload = load_fixture_json("tsetmc_clienttype_foolad_trimmed.json")
    payload["clientType"][0]["sell_N_Value"] = -1.0
    with pytest.raises(MarketParseError, match="negative"):
        parse_client_types(payload)


def test_the_overview_is_tehran_time_in_utc():
    row = parse_market_overview(load_fixture_json("tsetmc_overview_bourse.json"), "bourse")
    # 2026-09-29 11:09:03 Tehran is 07:39:03 UTC (UTC+03:30, no DST since 2022).
    assert row["activity_at"] == datetime(2026, 9, 29, 7, 39, 3, tzinfo=timezone.utc)
    assert row["index_value"] == pytest.approx(7591547.65)
    assert row["ew_index_value"] == pytest.approx(2029188.37)
    assert row["trade_value"] == pytest.approx(274275641654100.0)


def test_a_figure_the_farabourse_does_not_have_is_null_not_zero():
    row = parse_market_overview(
        load_fixture_json("tsetmc_overview_farabourse.json"), "farabourse"
    )
    assert row["ew_index_value"] is None
    assert row["ew_index_change"] is None
    assert row["index_value"] == pytest.approx(60730.04)


def test_the_sector_summary_keeps_tsetmcs_four_buckets():
    rows = parse_sector_summary(load_fixture_json("tsetmc_sectors_summary.json"))
    assert len(rows) == 51
    basic_metals = next(r for r in rows if r["sector_code"] == "27")
    assert (
        basic_metals["down_over_2"], basic_metals["down_under_2"],
        basic_metals["up_under_2"], basic_metals["up_over_2"],
    ) == (41, 31, 26, 80)
    # The trailing space in TSETMC's "27 " is gone; the label is folded.
    assert basic_metals["sector_fa"] == "فلزات اساسی"


# --- the power-of-ten correction ---------------------------------------------


def test_the_second_market_is_corrected_on_the_majority_scale():
    history = parse_index_history(load_fixture_json_gz("tsetmc_index_second_market.json.gz"))
    closes = [v.close for v in history.values]
    exps, breaks = scale_exponents(closes)
    assert exps.count(0) == 4263 and exps.count(1) == 31
    assert len(breaks) == 1
    assert history.values[breaks[0]].trade_date == date(2026, 8, 16)
    corrected_last = closes[-1] * 10 ** exps[-1]
    assert corrected_last == pytest.approx(13158400.0)
    # ...and the exchange agrees: its live figure the next morning was
    # 13,310,356.3, +1.15% on the session — which is a ratio of 0.9886, not 0.1.
    result = verdict(
        [v.trade_date for v in history.values], closes, exps, breaks, 0,
        _live()[SECOND_MARKET].value,
    )
    assert result.status == STATUS_VALIDATED
    assert result.live_ratio == pytest.approx(13158400.0 / 13310356.3)
    assert result.rows_rescaled == 31


def test_one_day_excursions_are_corrected_and_revert():
    history = parse_index_history(load_fixture_json_gz("tsetmc_index_financial.json.gz"))
    exps, breaks = scale_exponents([v.close for v in history.values])
    rescaled = [v.trade_date for v, e in zip(history.values, exps) if e != 0]
    assert len(breaks) == 4
    assert rescaled == [date(2020, 5, 12), date(2020, 5, 18)]


def test_tedpix_needs_no_correction_and_matches_the_live_value():
    history = parse_index_history(load_fixture_json_gz("tsetmc_index_tedpix.json.gz"))
    closes = [v.close for v in history.values]
    exps, breaks = scale_exponents(closes)
    assert breaks == [] and set(exps) == {0}
    result = verdict(
        [v.trade_date for v in history.values], closes, exps, breaks, 0,
        _live()[TEDPIX].value,
    )
    assert result.status == STATUS_VALIDATED
    # Largest session after correction: 2015-03-17, -5.51%.
    assert result.largest_move_date == date(2015, 3, 17)
    assert result.largest_move == pytest.approx(-0.0551, abs=5e-4)


def test_a_history_a_decade_away_from_the_live_value_is_refused():
    """The anchor is the majority, and the majority is CHECKED, not trusted."""
    closes = [100.0, 101.0, 102.0]
    exps, breaks = scale_exponents(closes)
    result = verdict(
        [date(2024, 1, d) for d in (6, 7, 8)], closes, exps, breaks, 0, 1021.0
    )
    assert result.status == STATUS_REFUSED
    assert "live figure" in result.refusal_reason


def test_a_genuine_large_move_is_not_a_scale_change():
    """+244.7% is the largest post-correction session measured across all 71
    indices (medical instruments, 2016-03-16). It is x3.45 — far from the x7.94
    at which the band begins — and is left alone and reported."""
    exps, breaks = scale_exponents([4636.3, 15979.5, 15180.7])
    assert breaks == [] and exps == [0, 0, 0]


def test_the_majority_decides_which_end_is_rescaled():
    # One early row at a scale the other three do not share: the early row moves.
    exps, _ = scale_exponents([100.0, 10.0, 11.0, 12.0])
    assert exps == [-1, 0, 0, 0]
    # Three early rows against one late row: the late row moves.
    exps, _ = scale_exponents([10.0, 11.0, 12.0, 125.0])
    assert exps == [0, 0, 0, -1]


def test_a_correction_beyond_three_decades_is_refused():
    closes = [1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 100000.0, 100000.0,
              100000.0, 100000.0]
    exps, breaks = scale_exponents(closes)
    result = verdict([date(2024, 1, 1 + i) for i in range(len(closes))], closes, exps,
                     breaks, 0, None)
    assert max(abs(e) for e in exps) > 3
    assert result.status == STATUS_REFUSED
    assert "power of ten" in result.refusal_reason


def test_no_live_value_is_recorded_as_not_checked_rather_than_passed():
    closes = [100.0, 101.0]
    exps, breaks = scale_exponents(closes)
    result = verdict([date(2024, 1, 6), date(2024, 1, 7)], closes, exps, breaks, 0, None)
    assert result.status == STATUS_VALIDATED
    assert result.live_ratio is None and result.live_value is None


# --- the ingest --------------------------------------------------------------


def test_ingest_stores_the_raw_close_and_the_exponent_beside_it(engine):
    _register_index(engine, SECOND_MARKET, market="bourse", kind="market")
    report = ingest_index_history(
        engine, load_fixture_json_gz("tsetmc_index_second_market.json.gz"), _live()
    )
    assert report["status"] == STATUS_VALIDATED
    assert report["values_inserted"] == 4294
    assert report["scale_breaks"] == 1 and report["rows_rescaled"] == 31
    with engine.connect() as conn:
        row = conn.execute(
            select(market_index_values.c.close, market_index_values.c.scale_exp).where(
                (market_index_values.c.ins_code == SECOND_MARKET)
                & (market_index_values.c.trade_date == date(2026, 9, 28))
            )
        ).one()
        check = conn.execute(select(market_index_checks)).mappings().one()
        registry = conn.execute(select(market_indices)).mappings().one()
    # RAW: exactly what TSETMC served, never close * 10.
    assert float(row.close) == 1315840.0 and row.scale_exp == 1
    assert check["status"] == STATUS_VALIDATED
    assert check["check_version"] == CHECK_VERSION
    assert check["first_break"] == date(2026, 8, 16)
    assert check["live_checked_at"] is not None
    assert registry["value_count"] == 4294
    assert registry["last_date"] == date(2026, 9, 28)


def test_re_ingest_is_idempotent(engine):
    _register_index(engine, FINANCIAL)
    payload = load_fixture_json_gz("tsetmc_index_financial.json.gz")
    ingest_index_history(engine, payload, _live())
    again = ingest_index_history(engine, payload, _live())
    assert again["values_inserted"] == 0
    assert again["values_existing"] == len(payload["indexB2"])
    assert again["rescaled_existing_rows"] == 0
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(market_index_checks)).scalar() == 1


def test_a_power_of_ten_restatement_is_accepted_and_changes_nothing(engine):
    """TSETMC repairing its own decimal shift restates a value by exactly x10.
    The stored raw value is kept; the corrected value is the same either way."""
    _register_index(engine, SECOND_MARKET, kind="market")
    payload = load_fixture_json_gz("tsetmc_index_second_market.json.gz")
    ingest_index_history(engine, payload, _live())
    repaired = copy.deepcopy(payload)
    newest = next(r for r in repaired["indexB2"] if r["dEven"] == 20260928)
    newest["xNivInuClMresIbs"] = 13158400.0
    report = ingest_index_history(engine, repaired, _live())
    assert report["restated_by_power_of_ten"] == 1
    with engine.connect() as conn:
        row = conn.execute(
            select(market_index_values.c.close, market_index_values.c.scale_exp).where(
                market_index_values.c.trade_date == date(2026, 9, 28)
            )
        ).one()
    assert float(row.close) * 10 ** row.scale_exp == pytest.approx(13158400.0)


def test_the_two_print_formats_of_one_index_value_are_one_value(engine):
    """MEASURED 2026-09-29: TSETMC's CDN answered two copies five minutes
    apart, one printing an index to six significant digits and one to one
    decimal (3,654,230.0 against 3,654,237.6; 148.3 against 148.4). Compared
    exactly, 64 of 71 indices failed on the second run."""
    _register_index(engine, FINANCIAL)
    payload = load_fixture_json_gz("tsetmc_index_financial.json.gz")
    ingest_index_history(engine, payload, _live())
    other = copy.deepcopy(payload)
    for row in other["indexB2"][:200]:
        value = row["xNivInuClMresIbs"]
        digits = 6 - len(str(int(value)))  # six significant digits, truncated
        row["xNivInuClMresIbs"] = math.floor(value * 10 ** digits) / 10 ** digits
    report = ingest_index_history(engine, other, _live())
    assert report["restated"] == 0 and report["values_inserted"] == 0


def test_a_restated_index_value_is_kept_and_named(engine):
    """Beyond the print formats the two copies disagreed on three indices, one
    date each (-0.72%, -0.51%, -0.12%). The stored value stays; the new
    sessions are still ingested; the report names the date."""
    _register_index(engine, FINANCIAL)
    payload = load_fixture_json_gz("tsetmc_index_financial.json.gz")
    newest = max(r["dEven"] for r in payload["indexB2"])
    older = copy.deepcopy(payload)
    older["indexB2"] = [r for r in older["indexB2"] if r["dEven"] != newest]
    ingest_index_history(engine, older, _live())
    changed = copy.deepcopy(payload)
    target = changed["indexB2"][100]
    stored = target["xNivInuClMresIbs"]
    target["xNivInuClMresIbs"] = stored * 0.9928
    report = ingest_index_history(engine, changed, _live())
    assert report["values_inserted"] == 1
    assert report["restated"] == 1
    [example] = report["restated_examples"]
    assert example["stored"] == pytest.approx(stored) and example["served"] == target["xNivInuClMresIbs"]
    with engine.connect() as conn:
        kept = conn.execute(
            select(market_index_values.c.close).where(
                market_index_values.c.trade_date == date.fromisoformat(example["date"])
            )
        ).scalar_one()
    assert float(kept) == pytest.approx(stored)  # never overwritten


def test_an_index_payload_unlike_most_of_the_store_fails_the_index(engine):
    _register_index(engine, FINANCIAL)
    payload = load_fixture_json_gz("tsetmc_index_financial.json.gz")
    ingest_index_history(engine, payload, _live())
    changed = copy.deepcopy(payload)
    for row in changed["indexB2"]:
        row["xNivInuClMresIbs"] *= 1.5
    with pytest.raises(IndexContradiction, match="not a restatement of this series"):
        ingest_index_history(engine, changed, _live())


def test_restatement_is_systematic_only_past_half_of_enough_rows():
    assert not restatement_is_systematic(1, 10)
    assert not restatement_is_systematic(5, 10)
    assert restatement_is_systematic(6, 10)
    # Too few rows compared to call anything systematic: a one-row overlap
    # that disagrees is a restatement, not a reason to stall a share for good.
    assert not restatement_is_systematic(3, 3)
    assert restatement_is_systematic(3, 4)


def test_a_power_of_ten_restatement_in_the_other_print_format_is_accepted(engine):
    """The copy without the decimal-shift stretch prints 10,060,226.2 where the
    stretched one printed 1,006,020.0: a power of ten to within the print."""
    _register_index(engine, SECOND_MARKET, kind="market")
    payload = load_fixture_json_gz("tsetmc_index_second_market.json.gz")
    ingest_index_history(engine, payload, _live())
    repaired = copy.deepcopy(payload)
    newest = next(r for r in repaired["indexB2"] if r["dEven"] == 20260928)
    newest["xNivInuClMresIbs"] = 13158426.2
    report = ingest_index_history(engine, repaired, _live())
    assert report["restated_by_power_of_ten"] == 1 and report["restated"] == 0


def test_an_index_outside_the_registry_is_refused(engine):
    with pytest.raises(MarketParseError, match="not in market_indices"):
        ingest_index_history(
            engine, load_fixture_json_gz("tsetmc_index_financial.json.gz"), _live()
        )


def test_a_shifting_majority_rewrites_exponents_never_closes(engine):
    _register_index(engine, "123")
    ingest_index_history(engine, _toy_index("123", [100.0, 101.0, 102.0]), {})
    later = _toy_index("123", [100.0, 101.0, 102.0, 10.3, 10.4, 10.5, 10.6, 10.7])
    report = ingest_index_history(engine, later, {})
    assert report["rescaled_existing_rows"] == 3
    with engine.connect() as conn:
        rows = conn.execute(
            select(market_index_values.c.close, market_index_values.c.scale_exp)
            .order_by(market_index_values.c.trade_date)
        ).all()
    assert [float(r.close) for r in rows[:3]] == [100.0, 101.0, 102.0]
    assert [r.scale_exp for r in rows] == [-1, -1, -1, 0, 0, 0, 0, 0]


def test_market_values_ingest_and_are_idempotent(engine):
    payload = load_fixture_json_gz("tsetmc_market_value_bourse.json.gz")
    first = ingest_market_values(engine, payload, "bourse")
    again = ingest_market_values(engine, payload, "bourse")
    assert first["values_inserted"] == 1615
    assert again["values_inserted"] == 0


def test_a_tenfold_market_value_step_refuses_the_series(engine):
    payload = load_fixture_json_gz("tsetmc_market_value_bourse.json.gz")
    payload["marketValue"][3]["marketCap"] *= 10
    with pytest.raises(MarketParseError, match="tenfold"):
        ingest_market_values(engine, payload, "bourse")
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(market_values)).scalar() == 0


def test_a_restated_market_value_is_stored_and_counted(engine):
    """TSETMC restates this series: two fetches an hour apart on 2026-09-29
    disagreed on 18 bourse sessions, the largest by 0.18% (2022-04-30). The
    latest statement is kept and every restatement is counted."""
    payload = load_fixture_json_gz("tsetmc_market_value_bourse.json.gz")
    ingest_market_values(engine, payload, "bourse")
    changed = copy.deepcopy(payload)
    changed["marketValue"][0]["marketCap"] *= 1.0018
    changed["marketValue"][1]["marketCap"] *= 1 + 3.8e-8  # recomputation noise
    report = ingest_market_values(engine, changed, "bourse")
    assert report["values_revised"] == 1
    assert report["largest_revision"]["pct"] == pytest.approx(0.18, abs=1e-4)
    with engine.connect() as conn:
        stored = conn.execute(
            select(market_values.c.market_cap).where(
                market_values.c.trade_date == date(2026, 9, 28)
            )
        ).scalar_one()
    assert float(stored) == pytest.approx(changed["marketValue"][0]["marketCap"])


def test_client_flows_ingest_for_a_roster_symbol(engine):
    _register_foolad(engine)
    payload = load_fixture_json("tsetmc_clienttype_foolad_trimmed.json")
    first = ingest_client_flows(engine, payload)
    again = ingest_client_flows(engine, payload)
    assert first["symbol"] == "فولاد"
    assert first["sessions_inserted"] == 125
    assert again["sessions_inserted"] == 0
    with engine.connect() as conn:
        row = conn.execute(
            select(equity_client_flows).where(
                equity_client_flows.c.trade_date == date(2020, 1, 13)
            )
        ).mappings().one()
    # Stored as served, including the session whose totals disagree: the
    # identity is checked where it is READ, not by throwing the row away.
    assert row["buy_i_value"] + row["buy_n_value"] != row["sell_i_value"] + row["sell_n_value"]


def test_client_flows_for_a_share_outside_the_universe_are_refused(engine):
    """The key moved from the roster to market_shares in 0030; a share neither
    carries is still refused, by an explicit lookup (SQLite enforces no FK)."""
    with pytest.raises(MarketParseError, match="not in market_shares"):
        ingest_client_flows(engine, load_fixture_json("tsetmc_clienttype_foolad_trimmed.json"))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(equity_client_flows)).scalar() == 0


def test_the_snapshot_and_the_sector_breadth_share_one_time(engine):
    report = ingest_snapshot(
        engine,
        {
            "bourse": load_fixture_json("tsetmc_overview_bourse.json"),
            "farabourse": load_fixture_json("tsetmc_overview_farabourse.json"),
        },
        load_fixture_json("tsetmc_sectors_summary.json"),
    )
    assert report["bourse"]["stored"] == "inserted"
    assert report["sectors"] == {"rows": 51, "inserted": 51}
    again = ingest_snapshot(
        engine, {"bourse": load_fixture_json("tsetmc_overview_bourse.json")},
        load_fixture_json("tsetmc_sectors_summary.json"),
    )
    assert again["bourse"]["stored"] == "existing"
    assert again["sectors"]["inserted"] == 0
    with engine.connect() as conn:
        stamps = conn.execute(
            select(sector_breadth_snapshots.c.activity_at).distinct()
        ).scalars().all()
        snaps = conn.execute(select(func.count()).select_from(market_snapshots)).scalar()
    assert snaps == 2
    assert len(stamps) == 1


def test_a_sector_summary_without_a_bourse_overview_has_no_time(engine):
    with pytest.raises(MarketParseError, match="no time"):
        ingest_snapshot(engine, {}, load_fixture_json("tsetmc_sectors_summary.json"))


def _write(tmp_path, name, payload):
    import json

    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_one_bad_item_does_not_cost_the_others(engine, tmp_path):
    _register_index(engine, FINANCIAL)
    good = _write(tmp_path, "fin.json", load_fixture_json_gz("tsetmc_index_financial.json.gz"))
    unknown = _write(tmp_path, "sm.json", load_fixture_json_gz("tsetmc_index_second_market.json.gz"))
    truncated = tmp_path / "trunc.json"
    truncated.write_text("{", encoding="utf-8")
    report = ingest_market_files(
        engine,
        {
            "index_live": [_write(tmp_path, "b1.json", load_fixture_json("tsetmc_index_live_bourse.json"))],
            "index_histories": [good, unknown, str(truncated)],
            "market_values": {"bourse": _write(
                tmp_path, "mv.json", load_fixture_json_gz("tsetmc_market_value_bourse.json.gz"))},
        },
    )
    assert set(report["indices"]) == {FINANCIAL}
    assert report["failed"] == 2
    assert report["succeeded"] == 2
    assert {e["kind"] for e in report["errors"]} == {"index_history"}


def test_a_run_in_which_everything_failed_raises(engine, tmp_path):
    unknown = _write(tmp_path, "sm.json", load_fixture_json_gz("tsetmc_index_second_market.json.gz"))
    with pytest.raises(MarketIngestFailed):
        ingest_market_files(engine, {"index_histories": [unknown]})


def test_an_empty_request_is_refused(engine):
    with pytest.raises(ValueError, match="named no payloads"):
        ingest_market_files(engine, {})


def test_the_index_roster_hides_disabled_rows(engine):
    _register_index(engine, FINANCIAL)
    _register_index(engine, SECOND_MARKET, kind="market")
    with engine.begin() as conn:
        conn.execute(
            market_indices.update()
            .where(market_indices.c.ins_code == SECOND_MARKET)
            .values(enabled=False)
        )
    assert [r["ins_code"] for r in index_roster(engine)] == [FINANCIAL]
    assert len(index_roster(engine, include_disabled=True)) == 2


# --- the endpoints -----------------------------------------------------------


def test_the_roster_endpoint(client, engine):
    _register_index(engine, TEDPIX)
    resp = client.get(
        "/internal/bourse/indices/roster", headers={"X-Internal-Token": TEST_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_the_ingest_endpoint_reports_per_item(client, engine, tmp_path):
    _register_index(engine, FINANCIAL)
    path = _write(tmp_path, "fin.json", load_fixture_json_gz("tsetmc_index_financial.json.gz"))
    resp = client.post(
        "/internal/bourse/ingest",
        json={"index_histories": [path]},
        headers={"X-Internal-Token": TEST_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["indices"][FINANCIAL]["scale_breaks"] == 4


def test_the_ingest_endpoint_answers_502_when_everything_failed(client, tmp_path):
    path = _write(tmp_path, "fin.json", load_fixture_json_gz("tsetmc_index_financial.json.gz"))
    resp = client.post(
        "/internal/bourse/ingest",
        json={"index_histories": [path]},
        headers={"X-Internal-Token": TEST_TOKEN},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_failed"


def test_the_ingest_endpoint_refuses_an_empty_body(client):
    resp = client.post(
        "/internal/bourse/ingest", json={}, headers={"X-Internal-Token": TEST_TOKEN}
    )
    assert resp.status_code == 400


# --- only settled sessions are stored ------------------------------------------


def test_the_settled_cutoff_is_the_tehran_date_until_mid_afternoon():
    from app.bourse.ingest import settled_cutoff

    # 11:39 Tehran, session open: today is not settled.
    assert settled_cutoff(datetime(2026, 9, 29, 8, 9, tzinfo=timezone.utc)) == date(2026, 9, 29)
    # 16:00 Tehran, long after the 12:30 close: today is.
    assert settled_cutoff(datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc)) == date(2026, 9, 30)
    # 02:00 Tehran is still the Tehran date, not the UTC one.
    assert settled_cutoff(datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc)) == date(2026, 9, 29)


def test_a_row_dated_today_mid_session_is_not_stored(engine):
    """Four dormant sector indices carried a row dated 2026-09-29 while that
    session was still open; the furniture fixture is one of them."""
    _register_index(engine, FURNITURE, kind="sector")
    payload = load_fixture_json_gz("tsetmc_index_furniture.json.gz")
    mid_session = datetime(2026, 9, 29, 8, 20, tzinfo=timezone.utc)
    report = ingest_index_history(engine, payload, {}, now=mid_session)
    assert report["unsettled_skipped"] == 1
    assert report["last_date"] == "2026-09-28"
    after_close = datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)
    again = ingest_index_history(engine, payload, {}, now=after_close)
    assert again["unsettled_skipped"] == 0 and again["values_inserted"] == 1
    assert again["last_date"] == "2026-09-29"
