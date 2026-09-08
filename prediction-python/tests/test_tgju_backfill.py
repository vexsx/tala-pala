"""Deep-history TGJU gap-fill (app/jobs/tgju_backfill.py).

Network is mocked with respx against a recorded ``summary-table-data``
fixture (tests/fixtures/tgju_history_sekee.json); nothing here touches the
internet.  The endpoint tests stub the provider method instead of using
respx, following tests/test_economic_job.py — respx and the FastAPI
TestClient both want to own httpx's transport.

The fixture is a real-shaped sekee payload spanning the deep era (2010, 2013)
and the days around a live cutoff, and it deliberately contains one row whose
close is ``-``: the parser drops that row, so the parsed count is legitimately
below ``recordsTotal`` and the truncation check must not mistake it for a
truncated download.  Its multi-year holes are also what exercises the
"unverifiable gap" half of the coherence check.

Tests that need a DENSE series — the ones about a scale break hiding inside
the span — build one with :func:`_dense_payload` instead, because a break
between two adjacent daily bars is exactly what the recorded fixture, with
three years between bars, cannot express.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import delete, func, insert, select

from app.db import app_settings, instruments, prices, raw_observations, utcnow
from app.jobs.tgju_backfill import (
    BACKFILL_SLUGS,
    JOIN_MAX_TOLERANCE_PCT,
    MAX_HISTORY_ROWS,
    MIN_CONVENTION_FACTOR,
    SCALE_ERROR_MIN_JUMP_PCT,
    SOURCE,
    SPAN_GUARANTEE_DAYS,
    SPLICE_NOTE_MARKER,
    SPLICE_SETTINGS_KEY,
    backfill_symbol,
    close_stamp,
    join_threshold_pct,
    run_tgju_backfill,
    span_tolerance_ratio,
)
from app.providers import tgju
from app.seed import seed_history

from .conftest import TEST_TOKEN, load_fixture_json

AUTH = {"X-Internal-Token": TEST_TOKEN}

HISTORY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}"

SYMBOL = "IR_COIN_EMAMI"
# The first live observation in most tests: 2026-04-09 06:15 UTC.  The fixture
# carries bars for 04-08, 04-09 and 04-10, so the cutoff sits inside the
# fixture's range and the boundary is actually exercised.
LIVE_AT = datetime(2026, 4, 9, 6, 15, tzinfo=timezone.utc)
LIVE_VALUE = 191_000_000.0  # IRT, ~0.2% above the 2026-04-08 fixture close

# Normalized (toman) closes of the three fixture bars that precede the cutoff.
EXPECTED_TOMAN = {
    date(2010, 4, 4): 258_000.0,
    date(2013, 7, 22): 985_000.0,
    date(2026, 4, 8): 190_600_000.0,
}


def _seed_live(engine, at=LIVE_AT, value=LIVE_VALUE, symbol=SYMBOL, unit="coin",
               source="tgju"):
    """One live price row — the era the backfill must never write into."""
    with engine.begin() as conn:
        conn.execute(
            insert(prices).values(
                symbol=symbol, value=value, currency="IRT", unit=unit,
                source=source, observed_at=at, collected_at=at, quality="ok",
            )
        )


def _mock_history(slug="sekee", payload=None, fixture="tgju_history_sekee.json"):
    return respx.get(HISTORY_URL.format(slug=slug)).mock(
        return_value=httpx.Response(
            200, json=payload if payload is not None else load_fixture_json(fixture)
        )
    )


def _stored(engine, symbol=SYMBOL, source=SOURCE):
    with engine.connect() as conn:
        rows = conn.execute(
            select(prices)
            .where(prices.c.symbol == symbol, prices.c.source == source)
            .order_by(prices.c.observed_at)
        ).mappings().all()
    return [dict(r) for r in rows]


def _count(engine, table, symbol=SYMBOL) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(table).where(table.c.symbol == symbol)
            ).scalar_one()
        )


# --- parsing against the recorded fixture -----------------------------------


def test_history_page_parses_fixture_and_carries_the_counts():
    payload = load_fixture_json("tgju_history_sekee.json")
    page = tgju.history_page(payload, "sekee")

    assert [d.isoformat() for d, _ in page.rows] == [
        "2010-04-04", "2013-07-22", "2026-04-08", "2026-04-09", "2026-04-10",
    ]
    assert page.rows[-1][1] == pytest.approx(1_915_000_000.0)  # raw rial close
    # The '-' row is dropped by the parser but WAS carried by the payload:
    # returned_rows counts what arrived, so a junk row never reads as a
    # truncated download.
    assert page.returned_rows == 6
    assert len(page.rows) == 5
    assert page.records_total == 6


@respx.mock
def test_fetch_asks_for_far_more_rows_than_the_longest_series(engine, settings):
    """The 1200 default would silently truncate every one of these series."""
    route = _mock_history()
    _seed_live(engine)
    backfill_symbol(engine, settings, SYMBOL)

    requested = int(route.calls.last.request.url.params["length"])
    assert requested == MAX_HISTORY_ROWS
    assert requested > 4284  # sekee's real row count on 2026-09-09


# --- gap-fill boundary ------------------------------------------------------


@respx.mock
def test_writes_nothing_at_or_after_the_live_cutoff(engine, settings):
    _mock_history()
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    assert report["inserted"] == 3
    assert report["skipped_at_or_after_cutoff"] == 2  # the 04-09 and 04-10 bars
    stored = _stored(engine)
    assert [r["observed_at"].date().isoformat() for r in stored] == [
        "2010-04-04", "2013-07-22", "2026-04-08",
    ]
    # Nothing at or after the live cutoff, and no backfilled row shares a UTC
    # day with a live one (the second-same-day-observation hazard).
    live_day = LIVE_AT.date()
    assert all(r["observed_at"].date() < live_day for r in stored)


@respx.mock
def test_boundary_day_is_excluded_even_for_a_late_first_live_observation(
    engine, settings
):
    """A 23:30 first live tick must not let a 23:00 close into its own day."""
    _mock_history()
    _seed_live(engine, at=datetime(2026, 4, 9, 23, 30, tzinfo=timezone.utc))

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    days = [r["observed_at"].date() for r in _stored(engine)]
    assert date(2026, 4, 9) not in days  # strictly before the cutoff DAY
    assert days[-1] == date(2026, 4, 8)


# --- units ------------------------------------------------------------------


@respx.mock
def test_rial_values_are_normalized_to_toman(engine, settings):
    _mock_history()
    _seed_live(engine)

    backfill_symbol(engine, settings, SYMBOL)

    stored = {r["observed_at"].date(): float(r["value"]) for r in _stored(engine)}
    assert stored == pytest.approx(EXPECTED_TOMAN)
    assert all(r["currency"] == "IRT" and r["unit"] == "coin" for r in _stored(engine))
    # Exactly the provider's own factor, not a locally re-derived one.
    assert stored[date(2026, 4, 8)] == pytest.approx(
        tgju.normalize_history_value("sekee", 1_906_000_000.0)
    )


@respx.mock
def test_raw_observations_keep_the_rial_value_and_unit(engine, settings):
    """The audit trail migration 0022 needed: raw beside normalized."""
    _mock_history()
    _seed_live(engine)

    backfill_symbol(engine, settings, SYMBOL)

    with engine.connect() as conn:
        raw = conn.execute(
            select(raw_observations)
            .where(raw_observations.c.symbol == SYMBOL)
            .order_by(raw_observations.c.observed_at)
        ).mappings().all()
    assert len(raw) == 3
    last = raw[-1]
    assert float(last["raw_value"]) == pytest.approx(1_906_000_000.0)  # RIALS
    assert last["unit"] == "IRR/coin"
    assert last["currency"] == "IRR"
    assert last["provider_code"] == "tgju"
    assert last["raw_payload"]["normalization"] == "rial_to_toman"
    assert last["raw_payload"]["normalized_value"] == pytest.approx(190_600_000.0)
    # One raw row per price row, on the same instants.
    assert [r["observed_at"] for r in raw] == [
        r["observed_at"] for r in _stored(engine)
    ]


# --- timestamp convention ---------------------------------------------------


def test_close_stamp_is_23_utc_on_the_bars_own_date():
    stamp = close_stamp(date(2013, 7, 22))
    assert stamp == datetime(2013, 7, 22, 23, 0, tzinfo=timezone.utc)
    # 02:30 Tehran the NEXT morning: after the bazaar close, so availability
    # is never overstated...
    tehran = stamp.astimezone(timezone(timedelta(hours=3, minutes=30)))  # Tehran
    assert (tehran.hour, tehran.minute) == (2, 30)
    assert tehran.date() == date(2013, 7, 23)
    # ...and still inside the bar's own UTC day, so daily_close (which floors
    # to the UTC day) keeps the bar on its correct date.
    assert stamp.date() == date(2013, 7, 22)
    # Explicitly NOT seed_history.py's 12:00 UTC = 15:30 Tehran, mid-session.
    assert stamp.hour != 12


@respx.mock
def test_stored_rows_use_the_23_utc_stamp(engine, settings):
    _mock_history()
    _seed_live(engine)

    backfill_symbol(engine, settings, SYMBOL)

    for row in _stored(engine):
        observed = row["observed_at"].replace(tzinfo=timezone.utc)
        assert (observed.hour, observed.minute) == (23, 0)
        assert observed == close_stamp(observed.date())


# --- the join sanity check --------------------------------------------------


@respx.mock
def test_join_check_refuses_a_scale_mismatched_series(engine, settings):
    """A live series quoted in rials next to a toman backfill: write nothing."""
    _mock_history()
    _seed_live(engine, value=1_910_000_000.0)  # 10x the backfilled level

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    join = report["join"]
    assert join["ok"] is False
    assert join["last_backfill_value"] == pytest.approx(190_600_000.0)
    assert join["first_live_value"] == pytest.approx(1_910_000_000.0)
    assert join["jump_pct"] == pytest.approx(902.0, abs=1.0)
    assert join["gap_days"] == 1
    assert join["threshold_pct"] == pytest.approx(20.0)
    assert "join sanity check failed" in report["reason"]
    # Nothing written for the symbol — not the deep rows, not the raw rows.
    assert _stored(engine) == []
    assert _count(engine, raw_observations) == 0
    assert _count(engine, prices) == 1  # only the seeded live row


@respx.mock
def test_join_check_passes_on_a_continuous_series(engine, settings):
    _mock_history()
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["join"]["ok"] is True
    assert report["join"]["jump_pct"] < report["join"]["threshold_pct"]
    assert report["status"] == "ok"


def test_join_threshold_scales_with_the_gap_but_never_hides_a_scale_error():
    assert join_threshold_pct(1) == pytest.approx(20.0)
    assert join_threshold_pct(3) == pytest.approx(30.0)
    assert join_threshold_pct(7) == pytest.approx(JOIN_MAX_TOLERANCE_PCT)
    # However wide the gap, the tolerance stays clear of the smallest jump a
    # rial/toman (or any order-of-magnitude) error can produce.
    assert join_threshold_pct(10_000) < SCALE_ERROR_MIN_JUMP_PCT


# --- truncation -------------------------------------------------------------


@respx.mock
def test_truncated_history_is_refused_not_stored(engine, settings):
    """recordsTotal 3458 with 3 rows: a fragment, refused loudly."""
    _mock_history(slug="geram18", payload=load_fixture_json("tgju_history_geram18.json"))
    _seed_live(engine, symbol="IR_GOLD_18K", value=18_295_400.0, unit="gram")

    report = backfill_symbol(engine, settings, "IR_GOLD_18K")

    assert report["status"] == "refused"
    assert "truncated" in report["reason"]
    assert report["records_total"] == 3458
    assert _stored(engine, symbol="IR_GOLD_18K") == []


@respx.mock
def test_payload_filling_the_cap_without_a_total_is_refused(engine, settings):
    """No recordsTotal and a full page: completeness cannot be shown."""
    row = ["1", "1", "1", "1,906,000,000", "-", "-", "2026/04/08", "1405/01/19"]
    _mock_history(payload={"data": [row] * MAX_HISTORY_ROWS})
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert "may be truncated" in report["reason"]
    assert _stored(engine) == []


# --- idempotency and dry run ------------------------------------------------


@respx.mock
def test_rerunning_inserts_nothing(engine, settings):
    _mock_history()
    _seed_live(engine)

    first = backfill_symbol(engine, settings, SYMBOL)
    before = _stored(engine)
    second = backfill_symbol(engine, settings, SYMBOL)

    assert first["inserted"] == 3
    assert second["inserted"] == 0
    assert second["status"] == "up_to_date"
    assert _stored(engine) == before
    assert _count(engine, raw_observations) == 3


@respx.mock
def test_dry_run_reports_without_writing(engine, settings):
    _mock_history()
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL, dry_run=True)

    assert report["status"] == "dry_run"
    assert report["would_insert"] == 3
    assert report["inserted"] == 0
    assert report["first"] == "2010-04-04" and report["last"] == "2026-04-08"
    assert report["join"]["ok"] is True  # every check still ran
    assert _stored(engine) == []
    assert _count(engine, raw_observations) == 0
    assert _count(engine, prices) == 1  # the seeded live row, untouched


# --- symbol selection -------------------------------------------------------


@respx.mock
def test_default_symbols_are_the_three_iranian_ones_and_need_a_live_anchor(
    engine, settings
):
    """No live row means no anchor for the join check, so nothing is written.

    Under ``respx.mock`` with no routes registered, any HTTP call would fail
    as unmocked — so a clean "refused" here also proves the anchor is checked
    BEFORE the network is touched.
    """
    result = run_tgju_backfill(engine, settings)

    assert [r["symbol"] for r in result["symbols"]] == list(BACKFILL_SLUGS)
    assert {r["status"] for r in result["symbols"]} == {"refused"}
    assert all("no live history" in r["reason"] for r in result["symbols"])
    assert result["total_inserted"] == 0


def test_xauusd_is_refused_with_the_futures_versus_spot_reason(engine, settings):
    with pytest.raises(ValueError) as exc:
        run_tgju_backfill(engine, settings, symbols=["XAUUSD"])
    message = str(exc.value)
    assert "FUTURES" in message and "spot" in message
    assert "GC=F" in message


def test_unknown_symbol_is_refused_rather_than_silently_dropped(engine, settings):
    with pytest.raises(ValueError) as exc:
        run_tgju_backfill(engine, settings, symbols=["IR_GOLD_18K", "NOT_A_SYMBOL"])
    assert "NOT_A_SYMBOL" in str(exc.value)


# --- endpoint ---------------------------------------------------------------


def _stub_history(monkeypatch, fixture="tgju_history_sekee.json"):
    payload = load_fixture_json(fixture)

    def fake(self, slug, max_rows=None):
        return tgju.history_page(payload, slug)

    monkeypatch.setattr(tgju.TGJUProvider, "fetch_history_page", fake)


def test_endpoint_backfills_and_is_idempotent(client, engine, monkeypatch):
    _stub_history(monkeypatch)
    _seed_live(engine)

    first = client.post(
        "/internal/backfill/tgju-history",
        json={"symbols": [SYMBOL]}, headers=AUTH,
    )
    assert first.status_code == 200
    body = first.json()
    assert body["total_inserted"] == 3
    assert body["symbols"][0]["status"] == "ok"
    assert body["refused"] == []

    again = client.post(
        "/internal/backfill/tgju-history",
        json={"symbols": [SYMBOL]}, headers=AUTH,
    )
    assert again.json()["total_inserted"] == 0
    assert len(_stored(engine)) == 3


def test_endpoint_dry_run_writes_nothing(client, engine, monkeypatch):
    _stub_history(monkeypatch)
    _seed_live(engine)

    resp = client.post(
        "/internal/backfill/tgju-history",
        json={"symbols": [SYMBOL], "dry_run": True}, headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["total_would_insert"] == 3
    assert body["total_inserted"] == 0
    assert _stored(engine) == []


def test_endpoint_refuses_xauusd_with_400(client):
    resp = client.post(
        "/internal/backfill/tgju-history",
        json={"symbols": ["XAUUSD"]}, headers=AUTH,
    )
    assert resp.status_code == 400
    assert "FUTURES" in resp.json()["error"]["message"]


def test_endpoint_requires_the_internal_token(client):
    assert client.post("/internal/backfill/tgju-history", json={}).status_code == 401


# --- helpers for the synthetic-payload tests --------------------------------


def _row(day: date, close: float, gregorian: str = None) -> list:
    """One summary-table-data row: [o, l, h, close, chg, chg%, greg, jalali]."""
    return [
        "1", "1", "1", f"{close:,.0f}", "-", "-",
        gregorian if gregorian is not None else day.strftime("%Y/%m/%d"),
        "1405/01/01",
    ]


def _payload(rows: list) -> dict:
    """A complete (non-truncated) payload, newest first as TGJU serves it."""
    return {
        "recordsTotal": len(rows),
        "recordsFiltered": len(rows),
        "data": list(reversed(rows)),
    }


def _dense_payload(start: date, closes) -> dict:
    """One bar per consecutive day, raw rial closes."""
    return _payload([
        _row(start + timedelta(days=i), close) for i, close in enumerate(closes)
    ])


def _seed_instrument(engine, code=SYMBOL, notes="Emami coin, bazaar close."):
    """The instruments row whose note the UI shows beside the symbol."""
    with engine.begin() as conn:
        conn.execute(insert(instruments).values(
            code=code, kind="market_price", name_en="Emami gold coin",
            name_fa="سکه امامی",
            domain="gold", quote_currency="IRT", unit="coin", decimals=0,
            calendar_class="tehran_bazaar", quality_tier="official_mirror",
            is_proxy=False, is_derived=False, enabled=True, notes=notes,
            created_at=utcnow(), updated_at=utcnow(),
        ))


def _instrument_notes(engine, code=SYMBOL) -> str:
    with engine.connect() as conn:
        return conn.execute(
            select(instruments.c.notes).where(instruments.c.code == code)
        ).scalar()


def _splice_register(engine) -> dict:
    with engine.connect() as conn:
        value = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == SPLICE_SETTINGS_KEY)
        ).scalar()
    return value or {}


# --- the splice is recorded, not hidden (finding 1) -------------------------


@respx.mock
def test_a_successful_backfill_registers_the_splice(engine, settings):
    """The join is a definition change, so it is written down where it is."""
    _mock_history()
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    register = _splice_register(engine)
    entry = register[SYMBOL]
    assert entry["splice_at"] == LIVE_AT.isoformat()
    assert entry["backfill_first_bar"] == "2010-04-04"
    assert entry["backfill_last_bar"] == "2026-04-08"
    assert entry["backfill_source"] == SOURCE
    assert entry["live_source"] == "tgju"  # the source of the first live row
    # Both sides described in words, so a reader never has to guess which
    # measurement each era is.
    assert "TGJU" in entry["backfill_definition"]
    assert entry["live_definition"] and entry["live_definition"] != entry["backfill_definition"]
    assert report["splice"]["recorded"] is True


@respx.mock
def test_the_instrument_note_says_the_definition_changes_in_words(engine, settings):
    _mock_history()
    _seed_live(engine)
    _seed_instrument(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    notes = _instrument_notes(engine)
    assert report["splice"]["instrument_note_updated"] is True
    # The human-written half survives...
    assert notes.startswith("Emami coin, bazaar close.")
    # ...and the splice sentence names the boundary and both eras.
    assert SPLICE_NOTE_MARKER in notes
    assert "CHANGES on 2026-04-09" in notes
    assert "2026-04-08" in notes and SOURCE in notes
    assert "mixes two conventions" in notes


@respx.mock
def test_rewriting_the_note_replaces_its_own_sentence_instead_of_stacking(
    engine, settings
):
    """A second backfill must not leave two splice sentences on one note."""
    _mock_history()
    _seed_live(engine)
    _seed_instrument(engine)
    backfill_symbol(engine, settings, SYMBOL)

    # Make the same rows eligible again (as if the backfill were re-done).
    with engine.begin() as conn:
        conn.execute(delete(prices).where(prices.c.source == SOURCE))
    backfill_symbol(engine, settings, SYMBOL)

    notes = _instrument_notes(engine)
    assert notes.count(SPLICE_NOTE_MARKER) == 1
    assert notes.startswith("Emami coin, bazaar close.")


@respx.mock
def test_a_missing_instruments_row_is_reported_not_silently_skipped(engine, settings):
    _mock_history()
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    assert report["splice"]["recorded"] is True
    assert report["splice"]["instrument_note_updated"] is False
    assert _splice_register(engine)[SYMBOL]["symbol"] == SYMBOL


@respx.mock
def test_dry_run_reports_the_splice_without_recording_it(engine, settings):
    _mock_history()
    _seed_live(engine)
    _seed_instrument(engine)

    report = backfill_symbol(engine, settings, SYMBOL, dry_run=True)

    assert report["status"] == "dry_run"
    assert report["splice"]["recorded"] is False
    assert report["splice"]["splice_at"] == LIVE_AT.isoformat()
    assert _splice_register(engine) == {}
    assert _instrument_notes(engine) == "Emami coin, bazaar close."


# --- coherence of the whole backfilled span (finding 2) ---------------------


@respx.mock
def test_a_scale_break_inside_the_span_is_refused_even_when_the_join_is_clean(
    engine, settings
):
    """The defect: a mid-span rial/toman switch that the join cannot see.

    Bars 3 and 4 are a 10x round trip, so the LAST bar still joins onto the
    live series perfectly — the only check that used to run.
    """
    _mock_history(payload=_dense_payload(date(2026, 4, 1), [
        1_900_000_000, 1_900_000_000, 190_000_000, 1_906_000_000, 1_910_000_000,
    ]))
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert "not internally coherent" in report["reason"]
    coherence = report["coherence"]
    assert coherence["ok"] is False
    assert coherence["break_count"] == 2  # 10x down, then 10x back up
    first = coherence["breaks"][0]
    assert first["from"] == "2026-04-02" and first["to"] == "2026-04-03"
    assert first["ratio"] == pytest.approx(10.0)
    assert first["gap_days"] == 1
    # Nothing written, and the join check never got to bless the series.
    assert _stored(engine) == []
    assert _count(engine, raw_observations) == 0
    assert report["join"] is None


@respx.mock
def test_a_violent_but_real_devaluation_is_written(engine, settings):
    """A 34% session is honest Iranian history, not a scale break.

    The rial has repeatedly moved this fast (October 2012, the 2018 sanctions
    snapback, June 2025), so a naive 20% bar-to-bar rule would refuse the most
    informative days in the series.
    """
    _mock_history(payload=_dense_payload(date(2026, 4, 6), [
        1_420_000_000, 1_910_000_000,
    ]))
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    assert report["inserted"] == 2
    assert report["coherence"]["ok"] is True
    assert report["coherence"]["max_ratio"] == pytest.approx(1.3451, abs=1e-3)


def test_span_tolerance_covers_a_crisis_session_but_not_a_convention_break():
    # One session: three times the worst session in the historical record...
    assert span_tolerance_ratio(1) == pytest.approx(1.509, abs=1e-3)
    # ...and still far below the smallest real convention factor (mesghal per
    # gram is 4.6083; rial per toman is 10).
    assert span_tolerance_ratio(1) < MIN_CONVENTION_FACTOR
    # A two-week Nowruz closure loosens it, and it is still below that factor.
    assert span_tolerance_ratio(14) < MIN_CONVENTION_FACTOR
    # The guarantee is stated, not assumed: this is the exact gap at which the
    # compounding tolerance reaches the smallest convention factor.
    assert span_tolerance_ratio(SPAN_GUARANTEE_DAYS) <= MIN_CONVENTION_FACTOR
    assert span_tolerance_ratio(SPAN_GUARANTEE_DAYS + 1) > MIN_CONVENTION_FACTOR
    # Monotone in the gap, so a longer closure never tightens the test.
    assert span_tolerance_ratio(30) > span_tolerance_ratio(3) > span_tolerance_ratio(1)


@respx.mock
def test_multi_year_holes_are_reported_as_unverifiable_not_as_passes(engine, settings):
    """The fixture jumps 2010 -> 2013 -> 2026: legal, but nothing is proven."""
    _mock_history()
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    coherence = report["coherence"]
    assert report["status"] == "ok"
    assert coherence["ok"] is True
    assert coherence["unverifiable_gap_count"] == 2
    assert coherence["max_gap_days"] > SPAN_GUARANTEE_DAYS
    assert coherence["unverifiable_gaps"][0]["from"] == "2010-04-04"


# --- bar dates are bounded (finding 3) --------------------------------------


@respx.mock
def test_a_jalali_date_in_the_gregorian_column_refuses_the_payload(engine, settings):
    """1405/01/19 parses as a perfectly valid Gregorian date in the year 1405."""
    rows = [
        _row(date(2026, 4, 8), 1_906_000_000),
        _row(date(2026, 4, 7), 1_900_000_000, gregorian="1405/01/19"),
    ]
    _mock_history(payload=_payload(rows))
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert "1405-01-19" in report["reason"]
    assert "plausible window" in report["reason"]
    # The whole payload is refused: a shifted column makes every row suspect.
    assert _stored(engine) == []
    assert _count(engine, raw_observations) == 0


@respx.mock
def test_a_bar_dated_in_the_future_refuses_the_payload(engine, settings):
    ahead = utcnow().date() + timedelta(days=30)
    _mock_history(payload=_payload([
        _row(date(2026, 4, 8), 1_906_000_000),
        _row(ahead, 1_910_000_000),
    ]))
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert ahead.isoformat() in report["reason"]
    assert _stored(engine) == []


@respx.mock
def test_an_implausibly_dated_live_row_refuses_instead_of_reporting_up_to_date(
    engine, settings
):
    """The bricked-job scenario: one year-1405 row blocks every real bar.

    Before the bound, such a row (written by an earlier unbounded run) made
    every subsequent run report the success-looking 'up_to_date' forever.
    """
    route = _mock_history()
    _seed_live(engine, at=datetime(1405, 4, 28, 12, 0, tzinfo=timezone.utc))

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert report["status"] != "up_to_date"
    assert "1405-04-28" in report["reason"]
    assert "blocking" in report["reason"]
    # Refused before the network was touched.
    assert route.called is False


def test_an_empty_write_set_is_refused_when_it_is_not_actually_up_to_date(
    engine, settings, monkeypatch
):
    """'up_to_date' is reserved for bars that really are already covered."""
    page = tgju.HistoryPage(
        rows=[(date(2026, 4, 8), 0.0)],  # dropped as non-positive, not skipped
        returned_rows=1,
        records_total=1,
    )
    monkeypatch.setattr(
        tgju.TGJUProvider, "fetch_history_page", lambda self, slug, max_rows=None: page
    )
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert "not an up-to-date series" in report["reason"]
    assert report["skipped_non_positive"] == 1


# --- the live anchor is chosen, not assumed (finding 4) ---------------------


@respx.mock
def test_a_synthetic_sample_row_is_never_the_join_reference(engine, settings):
    """prices has four writers; a bundled sample is not a market observation."""
    _mock_history()
    # Oldest row for the symbol is a bundled demo value, an order of magnitude
    # off, written by app/seed/seed_history.py under quality='ok'.
    _seed_live(engine, at=datetime(2026, 4, 9, 5, 0, tzinfo=timezone.utc),
               value=1_000_000.0, source="seed_sample")
    _seed_live(engine)  # the real live row, 191,000,000 IRT

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    # The junk row still bounds the WRITE (data exists on that day)...
    assert report["live_cutoff_source"] == "seed_sample"
    # ...but the value the backfill is checked against is the real one.
    assert report["first_live_source"] == "tgju"
    assert report["join"]["first_live_value"] == pytest.approx(LIVE_VALUE)
    assert report["join"]["ok"] is True
    assert [r["observed_at"].date().isoformat() for r in _stored(engine)][-1] == "2026-04-08"


@respx.mock
def test_a_non_ok_row_is_not_the_join_reference(engine, settings):
    _mock_history()
    with engine.begin() as conn:
        conn.execute(insert(prices).values(
            symbol=SYMBOL, value=1_000_000.0, currency="IRT", unit="coin",
            source="alanchand", observed_at=datetime(2026, 4, 9, 4, 0, tzinfo=timezone.utc),
            collected_at=LIVE_AT, quality="outlier",
        ))
    _seed_live(engine)

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "ok"
    assert report["live_cutoff_quality"] == "outlier"
    assert report["first_live_value"] == pytest.approx(LIVE_VALUE)


@respx.mock
def test_ignore_sources_overrides_a_junk_reference_without_moving_the_cutoff(
    engine, settings
):
    """The override a permanently-bricked symbol needs, and its limit."""
    _mock_history()
    # A trusted-by-default source with a junk oldest value: the join fails and
    # the symbol cannot be backfilled at all...
    _seed_live(engine, at=datetime(2026, 4, 9, 4, 0, tzinfo=timezone.utc),
               value=1_000_000.0, source="pricedb")
    _seed_live(engine)

    blocked = backfill_symbol(engine, settings, SYMBOL)
    assert blocked["status"] == "refused"
    assert "join sanity check failed" in blocked["reason"]

    # ...until an operator names the diagnosed source.
    freed = backfill_symbol(engine, settings, SYMBOL, ignore_sources=["pricedb"])

    assert freed["status"] == "ok"
    assert freed["first_live_source"] == "tgju"
    # The override moved the REFERENCE, never the boundary: the junk row's own
    # day is still off limits.
    assert freed["live_cutoff_source"] == "pricedb"
    assert all(r["observed_at"].date() < date(2026, 4, 9) for r in _stored(engine))


@respx.mock
def test_a_symbol_with_only_untrusted_rows_is_refused_with_that_stated(
    engine, settings
):
    _seed_live(engine, value=1_000_000.0, source="seed_sample")

    report = backfill_symbol(engine, settings, SYMBOL)

    assert report["status"] == "refused"
    assert "none is a trusted live observation" in report["reason"]
    assert "seed_sample" in report["reason"]
    assert _stored(engine) == []


def test_endpoint_passes_ignore_sources_through(client, engine, monkeypatch):
    _stub_history(monkeypatch)
    _seed_live(engine, at=datetime(2026, 4, 9, 4, 0, tzinfo=timezone.utc),
               value=1_000_000.0, source="pricedb")
    _seed_live(engine)

    resp = client.post(
        "/internal/backfill/tgju-history",
        json={"symbols": [SYMBOL], "ignore_sources": ["pricedb"]}, headers=AUTH,
    )

    body = resp.json()
    assert resp.status_code == 200
    assert body["ignore_sources"] == ["pricedb"]
    assert body["symbols"][0]["status"] == "ok"
    assert body["total_inserted"] == 3


# --- the legacy seeder no longer splices XAUUSD (finding 5) -----------------


@respx.mock
def test_the_seeder_does_not_write_xauusd_from_a_spot_source(engine, settings):
    """TGJU 'ons' and Stooq are SPOT; our XAUUSD is the GC=F futures proxy."""
    assert "XAUUSD" not in seed_history.TGJU_SLUGS
    assert "XAUUSD" not in seed_history.NETWORK_SYMBOLS

    inserted = seed_history.seed_from_network(engine, settings, "XAUUSD")

    assert inserted == 0
    # Not "the request failed", but "no request was made": respx would have
    # raised on any unmocked call, and seed_from_network swallows exceptions.
    assert respx.calls.call_count == 0
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(prices).where(prices.c.symbol == "XAUUSD")
        ).scalar_one() == 0


def test_init_sh_gets_xauusd_history_from_the_audited_job():
    """The two paths must agree: what the seeder dropped, init.sh must ask for."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "init.sh",
    )
    if not os.path.exists(path):  # the service image does not ship scripts/
        pytest.skip("scripts/init.sh not present in this checkout")
    script = open(path, "r", encoding="utf-8").read()

    assert "app.seed.seed_history" in script  # still seeds the Iranian symbols
    called = [
        line for line in script.splitlines()
        if "backfill/history" in line and not line.lstrip().startswith("#")
    ]
    assert called, "init.sh must backfill XAUUSD through jobs/backfill.py"
    assert any("XAUUSD" in line for line in called)
