"""The equity adjustment engine, its gate, and the ingest around them.

WHY THE FIXTURE IS THE WHOLE FILE
----------------------------------
``tests/fixtures/tsetmc_foolad_daily.json.gz`` is فولاد's complete
``GetClosingPriceDailyList`` response as TSETMC served it on 2026-09-10 — all
4,636 bars, byte-identical after decompression (sha256 of the decompressed
bytes: f08c6b5d013f443b054283a54ed4ab6bf49cf264359af2fbd983db331a513386).

The SCI workbook fixture was trimmed from 1.2 MB to 29 KB because a spreadsheet
parser only needs the rows it parses.  This one was NOT trimmed, and the
decision was deliberate.  Dropping bars changes the answer:

* the corporate-action detection compares each bar against the one before it,
  so removing a bar between an action and its predecessor destroys the action;
* removing bars manufactures calendar gaps, and the gate treats a gap as a
  reopening — so a trimmed fixture would make the gate vacuous, passing
  everything, and the gate is the single most important thing here to test;
* the measured session-return distribution (65,802 pairs, worst legitimate move
  13.6%) is what justifies the 25% bound, and a slice cannot demonstrate it.

Gzip gets the same bytes to 186 KB, which is the answer to "trim it if you can
without losing anything": the data is not compressible by deletion, only by
compression.

WHAT THE NUMBERS BELOW ARE
---------------------------
Every expectation in this file was measured against that exact payload before
the engine was written, and reproduced by the engine afterwards:

    raw close ratio       x1.5163   (1,900 -> 2,881 over nineteen years)
    adjusted close ratio  x907.86
    corporate actions     31
    2022-08-09            -42.34% raw  ->  +4.20% adjusted
    2008-10-26            -39.2% adjusted, and NOT an error

The last one is the reason this file exists rather than a smaller one.  A gate
that refuses any surviving -39% day would refuse فولاد — the very symbol the
engine is validated against — because that day is a genuine reopening auction
after a 43-calendar-day suspension, where the exchange lifts the price limit.
"""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app.db import (
    corporate_actions,
    equity_adjustments,
    equity_bars,
    equity_instruments,
)
from app.equities.adjust import (
    ADJUSTMENT_VERSION,
    KIND_CLOSE_RESTATED,
    KIND_REFERENCE_RESTATED,
    MAX_REOPENING_RETURN,
    MAX_SESSION_RETURN,
    SESSION_GAP_DAYS,
    STATUS_REFUSED,
    STATUS_VALIDATED,
    Bar,
    BarParseError,
    adjust,
    adjust_payload,
    detect_actions,
    first_traded_index,
    fold_name,
    fold_symbol,
    parse_daily_list,
    parse_deven,
    session_moves,
    validate,
)
from app.equities.ingest import (
    EquityIngestFailed,
    ingest_bar_files,
    ingest_payload,
    roster,
)

from .conftest import TEST_TOKEN, load_fixture_json, load_fixture_json_gz

FOOLAD = "46348559193224090"
FIXTURE = "tsetmc_foolad_daily.json.gz"

# فولاد's roster row, as migration 0028 seeds it. The tests build it from
# SQLAlchemy metadata rather than applying the SQL, the same way every other
# test in this suite works against the SQLite mirror of the schema.
FOOLAD_ROW = {
    "ins_code": FOOLAD,
    "symbol_fa": "فولاد",
    "name_fa": "فولاد مبارکه اصفهان",
    "market": "bourse",
    "board": "بازار اول (تابلوی اصلی) بورس",
    "sector_code": "27",
    "sector_fa": "فلزات اساسی",
    "isin": "IRO1FOLD0009",
}


@pytest.fixture(scope="module")
def payload():
    return load_fixture_json_gz(FIXTURE)


@pytest.fixture(scope="module")
def series(payload):
    return adjust_payload(payload, ins_code=FOOLAD)


@pytest.fixture()
def seeded(engine):
    """An engine whose roster carries فولاد, as 0028 seeds it."""
    with engine.begin() as conn:
        conn.execute(equity_instruments.insert().values(**FOOLAD_ROW))
    return engine


# --- the measured case -------------------------------------------------------


def test_fixture_is_the_whole_response(payload):
    """4,636 bars, one instrument, 2007-03-11 to 2026-09-09."""
    bars = parse_daily_list(payload, ins_code=FOOLAD)
    assert len(bars) == 4636
    assert bars[0].trade_date == date(2007, 3, 11)
    assert bars[-1].trade_date == date(2026, 9, 9)
    assert {bar.ins_code for bar in bars} == {FOOLAD}


def test_raw_ratio_is_the_nonsense_number(series):
    """x1.52 over nineteen years — the number that makes the adjustment necessary."""
    assert series.raw_ratio == pytest.approx(1.5163, abs=0.0005)
    assert series.traded_bars[0].final_close == 1900.0
    assert series.traded_bars[-1].final_close == 2881.0


def test_thirty_one_corporate_actions(series):
    assert len(series.actions) == 31
    # Oldest first, each with both numbers that imply its ratio.
    assert [a.effective_date for a in series.actions] == sorted(
        a.effective_date for a in series.actions
    )
    for action in series.actions:
        # فولاد carries only the reference_restated mechanism, which is exactly
        # why one validation case is not enough — see the شپنا tests below.
        assert action.kind == KIND_REFERENCE_RESTATED
        assert action.restated_close is None
        assert action.ratio == pytest.approx(
            action.price_yesterday / action.prev_close
        )
        assert action.prev_trade_date < action.effective_date


def test_adjusted_ratio_is_about_908(series):
    """x907.86, from chaining the 31 ratios back from the newest bar."""
    assert series.adjusted_ratio == pytest.approx(907.86, rel=1e-4)


def test_the_2022_artefact_moves_from_minus_42_to_plus_4(series):
    """The single case the brief names: a -42.3% day that never happened."""
    bars, adjusted = series.traded_bars, series.adjusted_closes
    index = next(i for i, b in enumerate(bars) if b.trade_date == date(2022, 8, 9))

    raw_return = bars[index].final_close / bars[index - 1].final_close - 1
    assert raw_return == pytest.approx(-0.4234, abs=0.0005)
    # The row itself says so: priceYesterday is 5,240, not the 9,470 close.
    assert bars[index].price_yesterday == 5240.0
    assert bars[index - 1].final_close == 9470.0

    adjusted_return = adjusted[index] / adjusted[index - 1] - 1
    assert adjusted_return == pytest.approx(0.0420, abs=0.0005)


def test_the_capital_increase_is_detected_across_halted_bars(series):
    """فولاد's 2022 action is restated in two steps, one on a bar with no trading.

    Dropping untraded bars before detection — the obvious simplification —
    loses the 11,170 -> 9,470 step and leaves a 15% error in the whole
    pre-2022 history.
    """
    by_date = {a.effective_date: a for a in series.actions}
    first_step = by_date[date(2022, 8, 6)]
    second_step = by_date[date(2022, 8, 9)]

    assert first_step.prev_close == 11170.0 and first_step.price_yesterday == 9470.0
    assert second_step.prev_close == 9470.0 and second_step.price_yesterday == 5240.0

    bars = {b.trade_date: b for b in series.bars}
    assert not bars[date(2022, 8, 6)].traded  # zero volume, and still an action


def test_newest_segment_is_left_unadjusted(series):
    """Back-adjustment means today's adjusted close IS today's real close."""
    last_action = max(a.effective_date for a in series.actions)
    for bar, adjusted in zip(series.traded_bars, series.adjusted_closes):
        if bar.trade_date >= last_action:
            assert adjusted == pytest.approx(bar.final_close)


def test_cumulative_factor_equals_the_chaining_walk(series):
    """The stored factor is the same arithmetic as walking backwards, exactly.

    This is what lets 0028 keep 31 numbers instead of materialising an adjusted
    copy of 4,636 bars, so the equality is asserted rather than assumed.
    """
    actions = {a.effective_date: a for a in series.actions}
    factor = 1.0
    expected: list[float] = []
    for bar in reversed(series.traded_bars):
        expected.append(bar.final_close * factor)
        if bar.trade_date in actions:
            factor *= actions[bar.trade_date].ratio
    expected.reverse()
    assert series.adjusted_closes == pytest.approx(expected, rel=1e-12)


# --- the gate ----------------------------------------------------------------


def test_foolad_passes_the_gate(series):
    validation = series.validation
    assert validation.status == STATUS_VALIDATED
    assert validation.violations == ()
    assert validation.sessions_checked == 4095
    # The worst genuine session in nineteen years is +12.3%, against a 25% bound.
    assert abs(validation.worst_return) < MAX_SESSION_RETURN
    assert validation.worst_return == pytest.approx(0.1231, abs=0.0005)


def test_the_43_day_suspension_reopening_is_reported_not_refused(series):
    """2008-10-26: -39.2% after a 43-day halt, and TSETMC says no action occurred.

    A gate that judged consecutive ROWS instead of consecutive SESSIONS would
    refuse فولاد here — the symbol it exists to validate — for a move the
    exchange genuinely produced when it reopened the instrument without a price
    limit.
    """
    moves = session_moves(series.traded_bars, series.adjusted_closes)
    reopening = next(m for m in moves if m.trade_date == date(2008, 10, 26))

    assert reopening.gap_days == 43
    assert not reopening.is_session
    assert reopening.ret == pytest.approx(-0.3922, abs=0.0005)
    assert series.validation.reopenings == 1
    # And no corporate action was invented to explain it.
    assert date(2008, 10, 26) not in {a.effective_date for a in series.actions}


def test_the_gate_refuses_a_surviving_minus_40_day(payload):
    """A -42.3% day on an ORDINARY session must refuse the whole symbol.

    Built from فولاد's own numbers rather than invented ones: the 2022-08-09
    capital increase really did take the closing price from 9,470 to 5,460.
    Here that pair is placed on a normal session boundary — both bars given
    trading volume — and TSETMC's signal for it is erased. That is exactly the
    failure the gate exists for: a session the exchange says was ordinary,
    carrying a move its price limit cannot produce.
    """
    broken = copy.deepcopy(payload)
    for row in broken["closingPriceDaily"]:
        if row["dEven"] == 20220806:
            # Make the 9,470 bar an ordinary traded session rather than a halt.
            row["priceYesterday"] = 11170.0   # was 9,470: the first step, erased
            row["qTotTran5J"], row["zTotTran"] = 1_000_000.0, 500.0
            row["priceFirst"] = row["priceMin"] = row["priceMax"] = 9470.0
        if row["dEven"] == 20220809:
            row["priceYesterday"] = 9470.0    # was 5,240: the second step, erased

    series = adjust_payload(broken, ins_code=FOOLAD)

    assert series.validation.status == STATUS_REFUSED
    assert len(series.actions) == 29  # two fewer than the honest payload
    violation = next(
        m for m in series.validation.violations if m.trade_date == date(2022, 8, 9)
    )
    assert violation.is_session and violation.gap_days == 3
    assert violation.ret == pytest.approx(-0.4234, abs=0.0005)
    assert "corporate action was missed" in series.validation.refusal_reason
    assert "2022-08-09" in series.validation.refusal_reason


def test_what_the_gate_cannot_catch_is_stated_not_hidden(payload):
    """A falsified reference on a HALT boundary is indistinguishable from a reopening.

    This is the documented limit of the method, asserted so it stays true and
    stays visible. Erase فولاد's 2022-08-09 signal WITHOUT moving the pair onto
    a session — which is how it really sits, since 2022-08-06 was a halt bar —
    and the resulting -42.3% is the same shape as the genuine -39.2% reopening
    on 2008-10-26 after a 43-day suspension. Nothing in this payload separates
    them, so the gate does not pretend to.

    The defence against this is the DETECTOR, not the gate: 30 of فولاد's 31
    actions fall on a reopening, and :func:`detect_actions` reads both of the
    ways TSETMC signals one.
    """
    hidden = copy.deepcopy(payload)
    for row in hidden["closingPriceDaily"]:
        if row["dEven"] == 20220809:
            row["priceYesterday"] = 9470.0

    series = adjust_payload(hidden, ins_code=FOOLAD)

    move = next(
        m
        for m in session_moves(series.traded_bars, series.adjusted_closes)
        if m.trade_date == date(2022, 8, 9)
    )
    assert move.ret == pytest.approx(-0.4234, abs=0.0005)
    assert not move.is_session          # the bar before it never traded
    assert series.validation.status == STATUS_VALIDATED
    # It is not silently accepted either: it is counted as a reopening, which
    # is the caveat that travels with the series to every reader.
    assert series.validation.reopenings == 2


def test_the_reopening_tier_refuses_a_broken_factor_chain():
    """The looser tier is a bound on the arithmetic, not on the market."""
    bars = _bars([100.0, 100.0, 1000.0], volumes=[500, 0, 500])
    series = adjust(bars)
    assert series.validation.status == STATUS_REFUSED
    assert "factor chain itself is wrong" in series.validation.refusal_reason
    assert series.validation.max_reopening_return == MAX_REOPENING_RETURN


def test_a_refusal_must_state_a_reason():
    """The verdict carries what an operator needs, or the schema rejects the row."""
    passing = validate(session_moves(*_toy_series([100, 101, 102])))
    assert passing.status == STATUS_VALIDATED and passing.refusal_reason == ""

    failing = validate(session_moves(*_toy_series([100, 50, 51])))
    assert failing.status == STATUS_REFUSED
    assert str(MAX_SESSION_RETURN * 100).startswith("25")
    assert failing.refusal_reason
    assert "Refusing the symbol" in failing.refusal_reason


def test_pre_listing_placeholder_bars_are_excluded_from_the_series():
    """نوری's defect, reproduced in miniature: par-value bars are prices of nothing."""
    closes = [1000.0, 1000.0, 1000.0, 31250.0, 32812.0]
    bars = _bars(closes, volumes=[0, 0, 0, 311419200, 434364])
    series = adjust(bars)

    assert series.pre_listing_bars == 3
    assert len(series.adjusted_closes) == 2
    assert series.traded_bars[0].final_close == 31250.0
    # Without the exclusion this is a +3,025% first day and the gate refuses it.
    assert series.validation.status == STATUS_VALIDATED
    assert series.validation.sessions_checked == 1


def test_an_instrument_that_never_traded_is_refused():
    with pytest.raises(BarParseError, match="never traded"):
        adjust(_bars([1000.0, 1000.0], volumes=[0, 0]))


# --- the second mechanism ----------------------------------------------------
#
# ``tests/fixtures/tsetmc_shepna_close_restated.json`` is a CONTIGUOUS 41-bar
# slice of شپنا's real response, 2014-02-16 to 2014-04-20.  Trimmed the way the
# SCI fixture was, and safely so: this tests the DETECTOR, and a contiguous
# slice leaves every bar-to-bar relationship inside it intact.  (The فولاد
# fixture could not be trimmed because it tests the GATE, whose meaning depends
# on the gaps between bars.)

SHEPNA_FIXTURE = "tsetmc_shepna_close_restated.json"


@pytest.fixture(scope="module")
def shepna():
    return load_fixture_json(SHEPNA_FIXTURE)


def test_an_action_applied_to_a_halted_bars_close_is_detected(shepna):
    """شپنا 2014-03-16: 27,031 -> 11,251 on a bar where nothing traded.

    TSETMC signalled this capital increase by restating the CLOSING PRICE in
    place rather than by restating the next bar's priceYesterday, so a detector
    that only compares across bars sees an ordinary series with a -58.4% hole
    in it. Thirty-two actions across the roster work this way, and in وبملت
    they account for a factor of 5.5 in the whole history.
    """
    series = adjust_payload(shepna)

    action = next(
        a for a in series.actions if a.effective_date == date(2014, 3, 16)
    )
    assert action.kind == KIND_CLOSE_RESTATED
    assert action.price_yesterday == 27031.0
    assert action.restated_close == 11251.0
    assert action.ratio == pytest.approx(11251.0 / 27031.0)
    # The audit pair for this kind is (price_yesterday, restated_close), so the
    # OTHER quotient is exactly 1 — nothing happened between the bars.
    assert action.price_yesterday == action.prev_close

    bars = {b.trade_date: b for b in series.bars}
    assert not bars[date(2014, 3, 16)].traded


def test_the_close_restatement_removes_a_58_percent_artefact(shepna):
    """Raw, the slice falls 56%. Adjusted, it rises 5.7% — and passes the gate."""
    series = adjust_payload(shepna)

    assert series.raw_ratio == pytest.approx(0.4399, abs=0.0005)
    assert series.adjusted_ratio == pytest.approx(1.0570, abs=0.0005)
    assert series.validation.status == STATUS_VALIDATED

    moves = session_moves(series.traded_bars, series.adjusted_closes)
    restated = next(m for m in moves if m.trade_date == date(2014, 3, 16))
    # Nothing traded, so the adjusted return across that bar is exactly zero.
    assert restated.ret == pytest.approx(0.0, abs=1e-12)


def test_the_close_restatement_survives_a_round_trip_through_the_store(engine, shepna):
    """The kind and its evidence reach the database, not just the report."""
    with engine.begin() as conn:
        conn.execute(
            equity_instruments.insert().values(
                ins_code="7745894403636165",
                symbol_fa="شپنا",
                name_fa="پالایش نفت اصفهان",
                market="bourse",
            )
        )
    report = ingest_payload(engine, shepna, ins_code="7745894403636165")
    assert report["actions_close_restated"] == 1
    assert report["actions_reference_restated"] == 0

    with engine.connect() as conn:
        row = conn.execute(select(corporate_actions)).mappings().one()
    assert row["kind"] == KIND_CLOSE_RESTATED
    assert float(row["restated_close"]) == 11251.0
    assert float(row["ratio"]) == pytest.approx(11251.0 / 27031.0)


# --- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (20260909, date(2026, 9, 9)),
        (20070311, date(2007, 3, 11)),
        ("20220809", date(2022, 8, 9)),
    ],
)
def test_deven_is_gregorian(value, expected):
    """dEven is Gregorian, alone among TSETMC's dates.

    Read as Jalali, 20260909 would be 2647-11-30 and every date filter in the
    read API would quietly return nothing.
    """
    assert parse_deven(value) == expected


@pytest.mark.parametrize("value", [0, 1405_06_18, 99999999, "", None, 20260931])
def test_deven_refuses_what_is_not_a_gregorian_date(value):
    with pytest.raises(BarParseError):
        parse_deven(value)


def test_payload_refuses_a_mismatched_ins_code(payload):
    with pytest.raises(BarParseError, match="offered as"):
        parse_daily_list(payload, ins_code="65883838195688438")


def test_payload_refuses_duplicate_sessions(payload):
    doubled = copy.deepcopy(payload)
    doubled["closingPriceDaily"].append(doubled["closingPriceDaily"][0])
    with pytest.raises(BarParseError, match="two bars for"):
        parse_daily_list(doubled)


def test_payload_refuses_a_mixed_instrument_file(payload):
    mixed = copy.deepcopy(payload)
    mixed["closingPriceDaily"][5]["insCode"] = "65883838195688438"
    with pytest.raises(BarParseError, match="mixes 2 instruments"):
        parse_daily_list(mixed)


def test_payload_refuses_a_shape_it_does_not_recognise():
    with pytest.raises(BarParseError, match="closingPriceDaily"):
        parse_daily_list({"instrumentInfo": {}})
    with pytest.raises(BarParseError, match="empty"):
        parse_daily_list({"closingPriceDaily": []})


def test_first_bar_may_carry_a_zero_reference_price(payload):
    """priceYesterday = 0 on bar one is 'there is no yesterday', not an action."""
    bars = parse_daily_list(payload)
    trimmed = _bars([1500.0, 1545.0], volumes=[2230300000, 64168880])
    trimmed = (
        Bar(**{**trimmed[0].__dict__, "price_yesterday": 0.0}),
        trimmed[1],
    )
    assert detect_actions(trimmed) == []
    assert bars[0].price_yesterday >= 0


def test_a_zero_reference_price_mid_series_is_refused():
    """It would zero the whole chain before it, so it raises instead."""
    bars = _bars([100.0, 100.0, 100.0])
    bars = (bars[0], Bar(**{**bars[1].__dict__, "price_yesterday": 0.0}), bars[2])
    with pytest.raises(BarParseError, match="non-positive reference price"):
        detect_actions(bars)


def test_folds_differ_for_a_key_and_a_label():
    """A symbol drops ZWNJ; a name keeps it, because there it separates words."""
    assert fold_symbol("فملي") == "فملی"          # Arabic YEH -> Persian YEH
    assert fold_symbol("كچاد") == "کچاد"          # Arabic KAF -> Persian KEHEH
    assert fold_symbol("خ‌گستر") == "خگستر"  # ZWNJ removed from a key
    assert fold_name("معدني‌وصنعتي‌چادرملو") == "معدنی‌وصنعتی‌چادرملو"


def test_first_traded_index_skips_only_the_leading_run():
    bars = _bars([1.0, 1.0, 2.0, 2.0, 3.0], volumes=[0, 0, 10, 0, 10])
    assert first_traded_index(bars) == 2


# --- the ingest --------------------------------------------------------------


def test_ingest_stores_raw_bars_and_actions(seeded, payload):
    report = ingest_payload(seeded, payload, ins_code=FOOLAD)

    assert report["status"] == STATUS_VALIDATED
    assert report["bars_inserted"] == 4636
    assert report["actions_inserted"] == 31
    assert report["adjustment_version"] == ADJUSTMENT_VERSION
    assert report["symbol"] == "فولاد"

    with seeded.connect() as conn:
        assert conn.execute(select(func.count()).select_from(equity_bars)).scalar() == 4636
        verdict = conn.execute(select(equity_adjustments)).mappings().one()
        assert verdict["status"] == STATUS_VALIDATED
        assert verdict["actions_applied"] == 31
        assert verdict["reopenings"] == 1
        assert float(verdict["max_session_return"]) == MAX_SESSION_RETURN
        assert float(verdict["max_reopening_return"]) == MAX_REOPENING_RETURN
        assert verdict["session_gap_days"] == SESSION_GAP_DAYS
        assert verdict["refusal_reason"] == ""
        coverage = conn.execute(select(equity_instruments)).mappings().one()
        assert coverage["bar_count"] == 4636
        assert coverage["first_bar"] == date(2007, 3, 11)
        assert coverage["last_bar"] == date(2026, 9, 9)


def test_equity_bars_never_receives_an_adjusted_value(seeded, payload):
    """The invariant the whole schema rests on, asserted against every row.

    Every stored close must be the number TSETMC served — never that number
    times a corporate-action factor. The adjusted series is x908 the raw one,
    so a single adjusted row would be visible immediately; this checks all
    4,636 rather than trusting that.
    """
    ingest_payload(seeded, payload, ins_code=FOOLAD)
    served = {
        bar.trade_date: bar for bar in parse_daily_list(payload, ins_code=FOOLAD)
    }

    with seeded.connect() as conn:
        rows = conn.execute(select(equity_bars)).mappings().all()
    assert len(rows) == len(served)
    for row in rows:
        bar = served[row["trade_date"]]
        assert float(row["final_close"]) == bar.final_close
        assert float(row["close"]) == bar.close
        assert float(row["open"]) == bar.open
        assert float(row["high"]) == bar.high
        assert float(row["low"]) == bar.low
        assert float(row["price_yesterday"]) == bar.price_yesterday
        assert int(row["volume"]) == bar.volume
        assert int(row["trade_count"]) == bar.trade_count

    # And the oldest bar in particular: adjusted it is ~1.7 million, raw 1,900.
    oldest = min(rows, key=lambda r: r["trade_date"])
    assert float(oldest["final_close"]) == 1900.0


def test_re_ingest_is_idempotent(seeded, payload):
    first = ingest_payload(seeded, payload, ins_code=FOOLAD)
    second = ingest_payload(seeded, payload, ins_code=FOOLAD)

    assert first["bars_inserted"] == 4636 and second["bars_inserted"] == 0
    assert first["actions_inserted"] == 31 and second["actions_inserted"] == 0
    assert second["bars_existing"] == 4636
    assert second["verdict"] == "updated"

    with seeded.connect() as conn:
        assert conn.execute(select(func.count()).select_from(equity_bars)).scalar() == 4636
        assert conn.execute(
            select(func.count()).select_from(corporate_actions)
        ).scalar() == 31
        assert conn.execute(
            select(func.count()).select_from(equity_adjustments)
        ).scalar() == 1


def test_a_payload_that_contradicts_a_stored_bar_is_refused(seeded, payload):
    """TSETMC does not revise a settled session, so a disagreement is not a revision."""
    ingest_payload(seeded, payload, ins_code=FOOLAD)

    tampered = copy.deepcopy(payload)
    for row in tampered["closingPriceDaily"]:
        if row["dEven"] == 20260909:
            row["pClosing"] = 9999.0

    with pytest.raises(BarParseError, match="contradicts"):
        ingest_payload(seeded, tampered, ins_code=FOOLAD)

    with seeded.connect() as conn:
        newest = conn.execute(
            select(equity_bars.c.final_close)
            .where(equity_bars.c.trade_date == date(2026, 9, 9))
        ).scalar()
    assert float(newest) == 2881.0  # untouched


def test_an_ins_code_outside_the_roster_is_refused(engine, payload):
    """Never invents a registry row from a file that happened to arrive."""
    with pytest.raises(BarParseError, match="not in equity_instruments"):
        ingest_payload(engine, payload, ins_code=FOOLAD)


def test_a_refused_symbol_still_stores_its_evidence(seeded, payload):
    """A refusal withholds the CONCLUSION, not the data behind it."""
    broken = copy.deepcopy(payload)
    for row in broken["closingPriceDaily"]:
        if row["dEven"] == 20220806:
            row["priceYesterday"] = 11170.0
            row["qTotTran5J"], row["zTotTran"] = 1_000_000.0, 500.0
            row["priceFirst"] = row["priceMin"] = row["priceMax"] = 9470.0
        if row["dEven"] == 20220809:
            row["priceYesterday"] = 9470.0

    report = ingest_payload(seeded, broken, ins_code=FOOLAD)

    assert report["status"] == STATUS_REFUSED
    assert report["bars_inserted"] == 4636
    assert report["actions_inserted"] == 29
    with seeded.connect() as conn:
        verdict = conn.execute(select(equity_adjustments)).mappings().one()
    assert verdict["status"] == STATUS_REFUSED
    assert verdict["refusal_reason"]
    assert "2022-08-09" in verdict["refusal_reason"]


def test_one_symbol_failing_does_not_abort_the_others(seeded, payload, tmp_path):
    """Per-symbol isolation: a truncated transfer costs one symbol, not the run."""
    good = tmp_path / f"{FOOLAD}.json"
    good.write_text(json.dumps(payload), encoding="utf-8")
    truncated = tmp_path / "65883838195688438.json"
    truncated.write_text('{"closingPriceDaily": [', encoding="utf-8")
    absent = tmp_path / "2400322364771558.json"

    report = ingest_bar_files(seeded, [str(truncated), str(good), str(absent)])

    assert report["validated"] == 1
    assert report["failed"] == 2
    assert "فولاد" in report["symbols"]
    assert {e["error"] for e in report["errors"]} == {
        "BarParseError", "FileNotFoundError"
    }
    with seeded.connect() as conn:
        assert conn.execute(select(func.count()).select_from(equity_bars)).scalar() == 4636


def test_a_pass_in_which_everything_failed_raises(seeded, tmp_path):
    """The endpoint turns this into a 502: a run that ingested nothing is not a success."""
    bad = tmp_path / "bad.json"
    bad.write_text("x" * 2000, encoding="utf-8")
    with pytest.raises(EquityIngestFailed) as excinfo:
        ingest_bar_files(seeded, [str(bad)])
    assert excinfo.value.report["failed"] == 1
    assert excinfo.value.report["validated"] == 0


def test_roster_hides_disabled_rows_unless_asked(seeded):
    with seeded.begin() as conn:
        conn.execute(
            equity_instruments.insert().values(
                ins_code="18027801615184692",
                symbol_fa="کچاد",
                name_fa="معدنی‌وصنعتی‌چادرملو",
                market="bourse",
                enabled=False,
                notes="seeded disabled; see migration 0028",
            )
        )
    assert [r["symbol_fa"] for r in roster(seeded)] == ["فولاد"]
    assert len(roster(seeded, include_disabled=True)) == 2


# --- the endpoints -----------------------------------------------------------


def test_ingest_endpoint_reports_per_symbol(client, engine, payload, tmp_path):
    with engine.begin() as conn:
        conn.execute(equity_instruments.insert().values(**FOOLAD_ROW))
    path = tmp_path / f"{FOOLAD}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    response = client.post(
        "/internal/equities/bars",
        json={"paths": [str(path)]},
        headers={"X-Internal-Token": TEST_TOKEN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["validated"] == 1
    assert body["adjustment_version"] == ADJUSTMENT_VERSION
    assert body["symbols"]["فولاد"]["adjusted_ratio"] == pytest.approx(907.86, rel=1e-4)


def test_ingest_endpoint_answers_502_when_every_payload_failed(client, tmp_path):
    path = tmp_path / "junk.json"
    path.write_text("x" * 2000, encoding="utf-8")
    response = client.post(
        "/internal/equities/bars",
        json={"paths": [str(path)]},
        headers={"X-Internal-Token": TEST_TOKEN},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_failed"


def test_ingest_endpoint_refuses_an_empty_body(client):
    response = client.post(
        "/internal/equities/bars",
        json={"paths": []},
        headers={"X-Internal-Token": TEST_TOKEN},
    )
    assert response.status_code == 400


def test_roster_endpoint(client, engine):
    with engine.begin() as conn:
        conn.execute(equity_instruments.insert().values(**FOOLAD_ROW))
    response = client.get(
        "/internal/equities/roster", headers={"X-Internal-Token": TEST_TOKEN}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["ins_code"] == FOOLAD


# --- helpers -----------------------------------------------------------------


def _bars(closes, volumes=None, start=date(2020, 1, 1)):
    """A minimal ascending bar series: one bar per calendar day.

    Used only for the cases that need a SHAPE rather than real data — a
    pre-listing run, a zero reference price. Everything about the arithmetic is
    tested against the فولاد fixture, because a hand-written series can only
    encode what the author already believed.
    """
    from datetime import timedelta

    volumes = volumes if volumes is not None else [1000] * len(closes)
    out = []
    previous = 0.0
    for index, (close, volume) in enumerate(zip(closes, volumes)):
        out.append(
            Bar(
                ins_code=FOOLAD,
                trade_date=start + timedelta(days=index),
                open=close if volume else 0.0,
                high=close if volume else 0.0,
                low=close if volume else 0.0,
                close=close,
                final_close=close,
                price_yesterday=previous,
                volume=volume,
                trade_count=1 if volume else 0,
                value=close * volume,
            )
        )
        previous = close
    return tuple(out)


def _toy_series(closes):
    bars = _bars(closes)
    return bars, [bar.final_close for bar in bars]
