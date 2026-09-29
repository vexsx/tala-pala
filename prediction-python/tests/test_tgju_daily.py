"""TGJU daily settled closes (app/jobs/tgju_daily.py, migration 0031).

Network is mocked with respx against TRIMMED REAL payloads of TGJU's
``summary-table-data`` endpoint, captured 2026-09-29 with the project's honest
User-Agent.  Every row in them is verbatim; the only edit is that
``recordsTotal``/``recordsFiltered`` are rewritten to the trimmed row count, so
a fixture served whole is a COMPLETE table as far as the truncation check can
tell.  The mock honours ``length`` the way TGJU does (the N newest rows, the
total unchanged), which is what lets the incremental pass be tested at all.

* tgju_daily_silver_999.json — 2021-07-15..2021-09-20 and 2022-09-05..
  2022-10-25: both eras of silver's junk bars (8,000 rial on 2021-08-10, 8,200
  on 2021-08-29, 2,008,800 on 2022-09-30) with honest bars around them.  Silver
  sat near 200,000 rial per gram in both windows, so joining them does not put
  a false level break between two neighbours.
* tgju_daily_silver_999_2026.json and the sekeb/nim/rob/gerami/geram24/
  mesghal/geram18 files — 2026-08-01..2026-09-28, the newest rows.
* tgju_daily_sekee.json — 2026-04-11..2026-09-28, which spans the Emami coin's
  83-day hole in production.

The endpoint tests stub the provider method instead of using respx, as
tests/test_tgju_backfill.py does: respx and the FastAPI TestClient both want to
own httpx's transport.
"""
from __future__ import annotations

import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import func, insert, select, text

from app.core.normalize import SYMBOL_META
from app.db import app_settings, data_providers, instruments, prices, raw_observations, utcnow
from app.jobs import tgju_daily
from app.jobs.tgju_backfill import MAX_HISTORY_ROWS, SPLICE_NOTE_MARKER
from app.jobs.tgju_daily import (
    GAP_FILL_FROM,
    GAP_FILL_NOTE_MARKER,
    GAP_FILL_SLUGS,
    RECENT_ROWS,
    REGISTER_KEY,
    SERIES_SLUGS,
    SUSPECT_RATIO,
    DailyIngestFailed,
    ingest_symbol,
    judge_bars,
    run_tgju_daily,
    tehran_today,
)
from app.providers import tgju

from .conftest import TEST_TOKEN, load_fixture_json

AUTH = {"X-Internal-Token": TEST_TOKEN}
HISTORY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}"
SOURCE = "tgju_history"

# The scheduled run the morning after the newest fixture bar: 00:25 UTC is
# 03:55 Tehran on 2026-09-29, so every fixture day (<= 09-28) has settled.
NOW = datetime(2026, 9, 29, 0, 25, tzinfo=timezone.utc)

SILVER = "IR_SILVER_999"
EMAMI = "IR_COIN_EMAMI"
SILVER_JUNK = {date(2021, 8, 10), date(2021, 8, 29), date(2022, 9, 30)}

MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "database", "migrations", "0031_iran_commodities.up.sql",
)


def _fixture_for(slug: str) -> str:
    return f"tgju_daily_{slug}.json"


def _serve(slug: str, payload: dict, status: int = 200):
    """Mock one slug, honouring ``length`` exactly as TGJU does."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="upstream error")
        length = int(request.url.params.get("length", "0") or 0)
        rows = payload["data"]
        body = dict(payload, data=rows[:length] if length > 0 else rows)
        return httpx.Response(200, json=body)

    return respx.get(HISTORY_URL.format(slug=slug)).mock(side_effect=handler)


def _serve_fixture(slug: str, name: str = None):
    return _serve(slug, load_fixture_json(name or _fixture_for(slug)))


def _serve_all(except_slug: str = None, **status_by_slug):
    routes = {}
    for symbol, slug in {**SERIES_SLUGS, **GAP_FILL_SLUGS}.items():
        if slug == except_slug:
            continue
        name = "tgju_daily_silver_999_2026.json" if slug == "silver_999" else None
        payload = load_fixture_json(name or _fixture_for(slug))
        routes[slug] = _serve(slug, payload, status=status_by_slug.get(slug, 200))
    return routes


def _row(day: date, close: float) -> list:
    """One summary-table-data row: [o, l, h, close, chg, chg%, greg, jalali]."""
    return ["1", "1", "1", f"{close:,.0f}", "-", "-", day.strftime("%Y/%m/%d"), "1405/01/01"]


def _payload(rows: list) -> dict:
    """A complete payload, newest first as TGJU serves it."""
    return {"recordsTotal": len(rows), "recordsFiltered": len(rows),
            "data": list(reversed(rows))}


def _stored(engine, symbol=SILVER, source=SOURCE):
    with engine.connect() as conn:
        rows = conn.execute(
            select(prices)
            .where(prices.c.symbol == symbol, prices.c.source == source)
            .order_by(prices.c.observed_at)
        ).mappings().all()
    return [dict(r) for r in rows]


def _stored_days(engine, symbol=SILVER, source=SOURCE) -> list[date]:
    return [r["observed_at"].date() for r in _stored(engine, symbol, source)]


def _raw(engine, symbol=SILVER, quality=None):
    stmt = select(raw_observations).where(raw_observations.c.symbol == symbol)
    if quality is not None:
        stmt = stmt.where(raw_observations.c.quality == quality)
    with engine.connect() as conn:
        rows = conn.execute(stmt.order_by(raw_observations.c.observed_at)).mappings().all()
    return [dict(r) for r in rows]


def _count(engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _register(engine) -> dict:
    with engine.connect() as conn:
        value = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == REGISTER_KEY)
        ).scalar()
    return value or {}


def _seed_price(engine, symbol, at, value, source, unit="coin"):
    with engine.begin() as conn:
        conn.execute(insert(prices).values(
            symbol=symbol, value=value, currency="IRT", unit=unit, source=source,
            observed_at=at, collected_at=at, quality="ok",
        ))


def _requested_lengths(route) -> list[int]:
    return [int(call.request.url.params["length"]) for call in route.calls]


def _stamp(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 23, 0, tzinfo=timezone.utc)


# --- the provider: history-only slugs are never collected live -----------------


def test_history_only_slugs_are_not_parsed_from_the_live_snapshot():
    """0031's symbols are daily settled closes; a live tick would be a lie."""
    payload = load_fixture_json("tgju_live.json")
    for slug in tgju.HISTORY_SLUG_MAP:
        payload["current"][slug] = dict(payload["current"]["geram18"])

    symbols = {o.symbol for o in tgju.parse_live(payload)}

    assert symbols == {"IR_GOLD_18K", "IR_COIN_EMAMI", "USD_IRT", "XAUUSD"}
    assert not set(tgju.HISTORY_SLUG_MAP) & set(tgju.SLUG_MAP)


def test_history_rows_keep_the_published_close_and_the_jalali_date():
    bars = tgju.parse_history_rows(load_fixture_json("tgju_daily_silver_999.json"), "silver_999")

    assert bars[0].day == date(2021, 7, 15)  # oldest first
    junk = next(b for b in bars if b.day == date(2021, 8, 10))
    assert junk.close == 8_000.0
    assert junk.close_text == "8,000"
    assert junk.jalali == "1400/05/19"
    # The pairs the deep backfill reads are the same bars.
    assert tgju.parse_history(load_fixture_json("tgju_daily_silver_999.json"),
                              "silver_999") == [(b.day, b.close) for b in bars]


def test_history_page_carries_the_bars_beside_the_pairs():
    page = tgju.history_page(load_fixture_json("tgju_daily_sekeb.json"), "sekeb")
    assert page.records_total == page.returned_rows == len(page.bars) == 47
    assert [(b.day, b.close) for b in page.bars] == page.rows
    assert tgju.normalize_history_value("sekeb", 2_408_000_000.0) == pytest.approx(240_800_000.0)


def test_every_job_symbol_agrees_with_the_stored_unit():
    """A mesghal price stored per gram is a 4.6x error no range check sees."""
    for symbol, slug in {**SERIES_SLUGS, **GAP_FILL_SLUGS}.items():
        mapped, raw_unit, raw_currency = tgju.slug_meta(slug)
        assert mapped == symbol
        assert raw_currency == "IRR"
        currency, unit = SYMBOL_META[symbol]
        assert currency == "IRT"
        assert raw_unit == f"IRR/{unit}", symbol
    assert SYMBOL_META["IR_GOLD_MESGHAL"] == ("IRT", "mesghal")
    assert SYMBOL_META["IR_SILVER_999"] == ("IRT", "gram")


def test_the_derived_series_are_the_stated_multiples_of_18k():
    """The claim migration 0031 writes into the registry, checked on TGJU's data."""
    g18 = dict(tgju.parse_history(load_fixture_json("tgju_daily_geram18.json"), "geram18"))
    for slug, factor in (("geram24", 4 / 3), ("mesghal", 4.3318)):
        other = dict(tgju.parse_history(load_fixture_json(_fixture_for(slug)), slug))
        ratios = [other[d] / g18[d] for d in sorted(set(g18) & set(other))]
        assert len(ratios) >= 40
        assert statistics.median(ratios) == pytest.approx(factor, rel=1e-3)


# --- the registry rows ---------------------------------------------------------


def _migration_statements() -> list[str]:
    sql = open(MIGRATION, encoding="utf-8").read()
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip()]


def test_migration_0031_registers_exactly_the_jobs_series(engine):
    """The seed is data the job depends on; run it, then read it back."""
    statements = _migration_statements()
    assert statements[0].startswith("ALTER TABLE instruments")  # the column, mirrored in db.py
    with engine.begin() as conn:
        # Postgres' now(), which SQLite does not have, for the UPDATE.
        conn.connection.dbapi_connection.create_function(
            "now", 0, lambda: utcnow().isoformat())
        for statement in statements[1:]:
            conn.execute(text(statement.rstrip(";")))
        rows = {
            r["code"]: dict(r)
            for r in conn.execute(select(instruments)).mappings().all()
        }

    assert set(rows) == set(SERIES_SLUGS)
    for code, row in rows.items():
        assert row["kind"] == "market_price"
        assert row["quote_currency"] == "IRT"
        assert row["calendar_class"] == "tehran_bazaar"
        assert (row["quote_currency"], row["unit"]) == SYMBOL_META[code]
        assert row["is_proxy"] is False or row["is_proxy"] == 0
        assert row["enabled"] in (True, 1)
        notes = row["notes"]
        assert "rials" in notes and "toman" in notes, code
        assert "daily settled close" in notes and "not a live quote" in notes, code
        assert SERIES_SLUGS[code] in notes, code
    assert rows["IR_SILVER_999"]["quality_tier"] == "commercial"
    assert rows["IR_SILVER_999"]["domain"] == "silver"
    for code in set(rows) - {"IR_SILVER_999"}:
        assert rows[code]["quality_tier"] == "official_mirror", code
        assert rows[code]["domain"] == "gold", code
    for code in ("IR_GOLD_24K", "IR_GOLD_MESGHAL"):
        assert rows[code]["derived_from"] == "IR_GOLD_18K"
        assert "constant by construction" in rows[code]["notes"]
    assert {c for c, r in rows.items() if r["derived_from"]} == {"IR_GOLD_24K", "IR_GOLD_MESGHAL"}
    silver = rows["IR_SILVER_999"]["notes"]
    for fragment in ("8,000", "8,200", "2,008,800", "x1.6", "XAGUSD", "premium"):
        assert fragment in silver


def test_migration_0031_down_undoes_exactly_the_registration():
    down = open(MIGRATION.replace(".up.sql", ".down.sql"), encoding="utf-8").read()
    codes = set(re.findall(r"'(IR_[A-Z0-9_]+)'", down))
    assert codes == set(SERIES_SLUGS)
    assert "DROP COLUMN IF EXISTS derived_from" in down
    # Measured history is not deleted to reverse a registration.
    assert "DELETE FROM prices" not in down and "DELETE FROM raw_observations" not in down


# --- the junk-bar rule ---------------------------------------------------------


def test_the_rule_flags_exactly_the_three_measured_silver_junk_bars():
    judged = judge_bars(tgju.parse_history_rows(
        load_fixture_json("tgju_daily_silver_999.json"), "silver_999"))

    suspect = {j.bar.day for j in judged if j.verdict == "suspect"}
    assert suspect == SILVER_JUNK
    assert all(j.verdict == "ok" for j in judged if j.bar.day not in SILVER_JUNK)
    by_day = {j.bar.day: j for j in judged}
    # ~25x below and ~10x above their neighbours: nowhere near the x1.6 line.
    assert by_day[date(2021, 8, 10)].median / 8_000 > 20
    assert 2_008_800 / by_day[date(2022, 9, 30)].median > 9


@pytest.mark.parametrize("name", [
    "tgju_daily_silver_999_2026.json", "tgju_daily_sekeb.json", "tgju_daily_nim.json",
    "tgju_daily_rob.json", "tgju_daily_gerami.json", "tgju_daily_geram24.json",
    "tgju_daily_mesghal.json", "tgju_daily_sekee.json",
])
def test_the_rule_flags_nothing_in_honest_tables(name):
    slug = re.match(r"tgju_daily_(.+?)(_2026)?\.json", name).group(1)
    judged = judge_bars(tgju.parse_history_rows(load_fixture_json(name), slug))
    assert [j.bar.day for j in judged if j.verdict != "ok"] == []


def test_a_bar_with_too_few_neighbours_is_held_rather_than_guessed():
    bars = tgju.parse_history_rows(_payload([
        _row(date(2026, 9, d), 1_000_000) for d in (1, 2, 3)
    ]), "silver_999")
    assert {j.verdict for j in judge_bars(bars)} == {"unjudged"}


def test_the_threshold_sits_between_a_crisis_session_and_a_junk_bar():
    # The deep backfill tolerates x1.5 for one honest session; the smallest
    # junk signature measured in TGJU's tables is x10.
    assert 1.5 < SUSPECT_RATIO < 10


# --- a full first pass ---------------------------------------------------------


@respx.mock
def test_a_first_pass_reads_the_whole_table_and_holds_the_junk(engine, settings):
    route = _serve_fixture("silver_999", "tgju_daily_silver_999.json")

    report = ingest_symbol(engine, settings, SILVER, now=NOW)

    assert _requested_lengths(route) == [MAX_HISTORY_ROWS]  # never -1
    assert report["status"] == "ok"
    assert report["mode"] == "full"
    assert "no 'tgju_history' row" in report["full_history_reason"]
    assert report["fetched"] == report["records_total"] == 108
    assert report["suspect"] == 3
    assert report["inserted"] == 105
    assert {s["date"] for s in report["suspects"]} == {d.isoformat() for d in SILVER_JUNK}
    stored = _stored_days(engine)
    assert len(stored) == 105
    assert not SILVER_JUNK & set(stored)


@respx.mock
def test_stored_closes_are_toman_stamped_23_utc_on_their_own_date(engine, settings):
    _serve_fixture("silver_999", "tgju_daily_silver_999.json")

    ingest_symbol(engine, settings, SILVER, now=NOW)

    rows = {r["observed_at"].date(): r for r in _stored(engine)}
    row = rows[date(2021, 8, 11)]
    assert float(row["value"]) == pytest.approx(19_900.0)  # 199,000 rial / 10
    assert (row["currency"], row["unit"], row["quality"]) == ("IRT", "gram", "ok")
    for r in rows.values():
        observed = r["observed_at"].replace(tzinfo=timezone.utc)
        assert observed == _stamp(observed.date())


@respx.mock
def test_every_stored_close_has_its_rial_audit_row(engine, settings):
    _serve_fixture("silver_999", "tgju_daily_silver_999.json")

    ingest_symbol(engine, settings, SILVER, now=NOW)

    ok = _raw(engine, quality="ok")
    assert len(ok) == len(_stored(engine)) == 105
    audit = next(r for r in ok if r["raw_payload"]["bar_date"] == "2021-08-11")
    assert float(audit["raw_value"]) == pytest.approx(199_000.0)  # RIALS
    assert (audit["unit"], audit["currency"], audit["provider_code"]) == ("IRR/gram", "IRR", "tgju")
    assert audit["raw_payload"]["close_text"] == "199,000"
    assert audit["raw_payload"]["jalali_date"] == "1400/05/20"
    assert audit["raw_payload"]["normalized_value"] == pytest.approx(19_900.0)
    # The deep backfill's key for the same fact, so the two never double-audit.
    assert audit["dedupe_key"] == "tgju|IR_SILVER_999|history|2021-08-11"


@respx.mock
def test_a_suspect_close_is_audited_and_never_stored(engine, settings):
    _serve_fixture("silver_999", "tgju_daily_silver_999.json")

    ingest_symbol(engine, settings, SILVER, now=NOW)

    held = {r["raw_payload"]["bar_date"]: r for r in _raw(engine, quality="suspect")}
    assert set(held) == {d.isoformat() for d in SILVER_JUNK}
    junk = held["2022-09-30"]
    assert float(junk["raw_value"]) == pytest.approx(2_008_800.0)
    assert junk["raw_payload"]["close_text"] == "2,008,800"
    assert junk["raw_payload"]["neighbour_median_rial"] == pytest.approx(206_450.0)
    assert "median" in junk["raw_payload"]["suspect_reason"]
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(prices)
            .where(prices.c.symbol == SILVER, prices.c.quality != "ok")
        ).scalar_one() == 0


# --- incremental passes, idempotency, restatements ----------------------------


@respx.mock
def test_a_second_pass_reads_forty_rows_and_writes_nothing(engine, settings):
    route = _serve_fixture("silver_999", "tgju_daily_silver_999_2026.json")
    first = ingest_symbol(engine, settings, SILVER, now=NOW)
    prices_before, raw_before = _count(engine, prices), _count(engine, raw_observations)

    second = ingest_symbol(engine, settings, SILVER, now=NOW + timedelta(hours=12))

    assert first["inserted"] == 56
    assert _requested_lengths(route) == [MAX_HISTORY_ROWS, RECENT_ROWS]
    assert second["mode"] == "recent"
    assert second["fetched"] == RECENT_ROWS
    assert second["inserted"] == 0
    assert second["unchanged"] == RECENT_ROWS
    assert second["restated"] == 0
    assert (_count(engine, prices), _count(engine, raw_observations)) == (prices_before, raw_before)


@respx.mock
def test_a_restated_close_is_counted_and_left_as_stored(engine, settings):
    _serve_fixture("silver_999", "tgju_daily_silver_999_2026.json")
    ingest_symbol(engine, settings, SILVER, now=NOW)
    with engine.begin() as conn:
        conn.execute(
            prices.update()
            .where(prices.c.symbol == SILVER, prices.c.observed_at == _stamp(date(2026, 9, 20)))
            .values(value=123_456.0)
        )

    report = ingest_symbol(engine, settings, SILVER, now=NOW + timedelta(hours=12))

    assert report["restated"] == 1
    assert report["inserted"] == 0
    restated = report["restatements"][0]
    assert restated["date"] == "2026-09-20"
    assert restated["stored"] == pytest.approx(123_456.0)
    kept = {r["observed_at"].date(): float(r["value"]) for r in _stored(engine)}
    assert kept[date(2026, 9, 20)] == pytest.approx(123_456.0)  # never overwritten


@respx.mock
def test_dry_run_runs_every_check_and_writes_nothing(engine, settings):
    _serve_fixture("silver_999", "tgju_daily_silver_999.json")

    report = ingest_symbol(engine, settings, SILVER, dry_run=True, now=NOW)

    assert report["status"] == "dry_run"
    assert report["would_insert"] == 105
    assert report["suspect"] == 3
    assert report["inserted"] == 0
    assert _count(engine, prices) == 0
    assert _count(engine, raw_observations) == 0
    assert _register(engine) == {}


@respx.mock
def test_a_truncated_full_table_is_refused(engine, settings):
    payload = load_fixture_json("tgju_daily_silver_999_2026.json")
    payload["recordsTotal"] = payload["recordsFiltered"] = 1674
    _serve("silver_999", payload)

    report = ingest_symbol(engine, settings, SILVER, now=NOW)

    assert report["status"] == "refused"
    assert "truncated" in report["reason"]
    assert _count(engine, prices) == 0


@respx.mock
def test_a_jalali_date_in_the_gregorian_column_refuses_the_payload(engine, settings):
    rows = [_row(date(2026, 9, d), 5_000_000) for d in range(1, 20)]
    rows[5][6] = "1405/06/10"
    _serve("silver_999", _payload(rows))

    report = ingest_symbol(engine, settings, SILVER, now=NOW)

    assert report["status"] == "refused"
    assert "1405-06-10" in report["reason"]
    assert _count(engine, prices) == 0


@respx.mock
def test_an_outage_longer_than_the_recent_page_reads_the_whole_table(engine, settings):
    route = _serve_fixture("silver_999", "tgju_daily_silver_999_2026.json")
    ingest_symbol(engine, settings, SILVER, now=NOW)
    # As if the last pass had run in July: the 40 newest rows no longer reach it.
    with engine.begin() as conn:
        register = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == REGISTER_KEY)
        ).scalar()
        register[SILVER]["newest_bar_seen"] = "2026-07-15"
        conn.execute(
            app_settings.update().where(app_settings.c.key == REGISTER_KEY)
            .values(value=dict(register))
        )

    report = ingest_symbol(engine, settings, SILVER, now=NOW)

    assert _requested_lengths(route) == [MAX_HISTORY_ROWS, RECENT_ROWS, MAX_HISTORY_ROWS]
    assert report["mode"] == "full"
    assert "never read" in report["full_history_reason"]
    assert report["fetched"] == 56


# --- only settled days ---------------------------------------------------------


@respx.mock
def test_the_day_in_progress_in_tehran_is_never_written(engine, settings):
    _serve_fixture("silver_999", "tgju_daily_silver_999_2026.json")
    midday = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)  # 13:30 Tehran, 09-28

    report = ingest_symbol(engine, settings, SILVER, now=midday)

    assert report["tehran_today"] == "2026-09-28"
    assert report["skipped_not_settled"] == 1
    assert max(_stored_days(engine)) == date(2026, 9, 27)


@respx.mock
def test_a_close_is_not_written_before_its_23_utc_stamp(engine, settings):
    """21:00 UTC is already 09-28 in Tehran, but 09-27's stamp is an hour away."""
    _serve_fixture("silver_999", "tgju_daily_silver_999_2026.json")
    evening = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
    assert tehran_today(evening) == date(2026, 9, 28)

    report = ingest_symbol(engine, settings, SILVER, now=evening)

    assert report["skipped_not_settled"] == 2  # 09-27 (stamp pending) and 09-28
    assert max(_stored_days(engine)) == date(2026, 9, 26)
    assert all(r["observed_at"].replace(tzinfo=timezone.utc) <= evening
               for r in _stored(engine))


@respx.mock
def test_a_held_level_shift_is_written_once_its_later_neighbours_exist(engine, settings):
    """Junk and an honest jump look alike until the days after the jump exist."""
    start = date(2026, 9, 1)
    before = [_row(start + timedelta(days=i), 1_000_000) for i in range(10)]
    jump = [_row(start + timedelta(days=10 + i), 2_000_000) for i in range(8)]
    _serve("silver_999", _payload(before + jump[:1]))

    first = ingest_symbol(engine, settings, SILVER, now=NOW)

    assert first["inserted"] == 10
    assert [s["date"] for s in first["suspects"]] == ["2026-09-11"]

    # A week later TGJU has published the days after the jump (respx replaces
    # the route registered for the same URL).
    _serve("silver_999", _payload(before + jump))
    second = ingest_symbol(engine, settings, SILVER, now=NOW + timedelta(days=1))

    assert second["suspect"] == 0
    assert second["inserted"] == 8
    assert date(2026, 9, 11) in _stored_days(engine)


# --- the Emami coin's hole -------------------------------------------------------


def _seed_emami_like_production(engine):
    """TGJU history to 04-26, one pricedb row on 04-27, live rows from 07-19."""
    sekee = dict(tgju.parse_history(load_fixture_json("tgju_daily_sekee.json"), "sekee"))
    for day, close in sekee.items():
        if day <= date(2026, 4, 26):
            _seed_price(engine, EMAMI, _stamp(day), close / 10, SOURCE)
    _seed_price(engine, EMAMI, datetime(2026, 4, 27, 20, 0, tzinfo=timezone.utc),
                230_000_000.0, "pricedb")
    day = date(2026, 7, 19)
    while day <= date(2026, 9, 28):
        _seed_price(engine, EMAMI, datetime(day.year, day.month, day.day, 9, 0,
                                            tzinfo=timezone.utc), 250_000_000.0, "brsapi")
        day += timedelta(days=1)
    return sekee


@respx.mock
def test_emami_is_filled_only_on_days_that_hold_no_observation(engine, settings):
    sekee = _seed_emami_like_production(engine)
    route = _serve_fixture("sekee")

    report = ingest_symbol(engine, settings, EMAMI, now=NOW)

    hole = sorted(d for d in sekee if date(2026, 4, 28) <= d <= date(2026, 7, 18))
    assert report["role"] == "gap_fill"
    # It HAS tgju_history rows, but this job never made a full pass for it:
    # the first pass reads the whole table, which is what reaches the hole.
    assert _requested_lengths(route) == [MAX_HISTORY_ROWS]
    assert "full pass" in report["full_history_reason"]
    assert report["inserted"] == len(hole) > 60
    assert report["first_written"] == hole[0].isoformat()
    assert report["last_written"] == hole[-1].isoformat()
    written = [d for d in _stored_days(engine, EMAMI) if d > date(2026, 4, 26)]
    assert written == hole
    # The live era and the pricedb day kept exactly what they had.
    assert report["skipped_other_source"] == sum(
        1 for d in sekee if d == date(2026, 4, 27) or d >= date(2026, 7, 19))
    # The backfilled era matched TGJU to the rial: nothing restated.
    assert report["unchanged"] == sum(1 for d in sekee if d <= date(2026, 4, 26))
    assert report["restated"] == 0
    with engine.connect() as conn:
        live_days = conn.execute(
            select(func.count()).select_from(prices)
            .where(prices.c.symbol == EMAMI, prices.c.source == "brsapi")
        ).scalar_one()
    assert live_days == (date(2026, 9, 28) - date(2026, 7, 19)).days + 1


@respx.mock
def test_emami_is_not_filled_on_a_utc_day_that_has_not_ended(engine, settings):
    """At 23:30 UTC the bar's stamp has passed, but its UTC day has not: a live
    row can still land in it, so "no observation that day" is not known yet."""
    _seed_emami_like_production(engine)
    with engine.begin() as conn:
        conn.execute(prices.delete().where(
            prices.c.symbol == EMAMI, prices.c.source == "brsapi",
            prices.c.observed_at >= datetime(2026, 9, 28, tzinfo=timezone.utc)))
    _serve_fixture("sekee")
    late = datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc)
    assert tehran_today(late) == date(2026, 9, 29)  # the Tehran date alone would allow it

    report = ingest_symbol(engine, settings, EMAMI, now=late)

    assert report["status"] == "ok"
    assert date(2026, 9, 28) not in _stored_days(engine, EMAMI)
    assert report["skipped_not_settled"] == 1
    # The morning run, with the UTC day over and still empty, fills it.
    _serve_fixture("sekee")
    morning = ingest_symbol(engine, settings, EMAMI, now=NOW)
    assert morning["inserted"] == 1
    assert date(2026, 9, 28) in _stored_days(engine, EMAMI)


@respx.mock
def test_emami_is_not_filled_before_the_hole(engine, settings):
    """An empty day inside the backfilled era is a Friday or a holiday the
    bazaar did not trade — TGJU has a close for it, and the deep backfill
    deliberately stored none. The gap-fill starts at the hole (2026-04-27)."""
    assert GAP_FILL_FROM[EMAMI] == date(2026, 4, 27)
    _seed_emami_like_production(engine)
    with engine.begin() as conn:
        conn.execute(prices.delete().where(
            prices.c.symbol == EMAMI,
            prices.c.observed_at >= datetime(2026, 4, 18, tzinfo=timezone.utc),
            prices.c.observed_at < datetime(2026, 4, 19, tzinfo=timezone.utc)))
    _serve_fixture("sekee")

    report = ingest_symbol(engine, settings, EMAMI, now=NOW)

    assert report["skipped_before_window"] == 1
    assert date(2026, 4, 18) not in _stored_days(engine, EMAMI)
    assert report["first_written"] == "2026-04-28"


@respx.mock
def test_a_switched_off_provider_skips_the_pass_without_failing_it(engine, settings):
    """data_providers.enabled = FALSE for tgju stops this job as it stops the
    live collection: every symbol is skipped with the reason, nothing is
    requested, and the pass is not a failure (the Go scheduler reads 200)."""
    with engine.begin() as conn:
        conn.execute(insert(data_providers).values(
            code="tgju", name="TGJU", category="iran_gold", enabled=False))
    route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))

    out = run_tgju_daily(engine, settings, now=NOW)

    assert not route.called
    assert out["skipped"] and "switched TGJU off" in out["skipped"]
    assert out["failed"] == [] and out["total_inserted"] == 0
    assert {r["status"] for r in out["symbols"]} == {"skipped"}
    assert len(out["symbols"]) == len(SERIES_SLUGS) + len(GAP_FILL_SLUGS)


@respx.mock
def test_emami_reads_forty_rows_once_its_full_pass_is_recorded(engine, settings):
    _seed_emami_like_production(engine)
    route = _serve_fixture("sekee")
    ingest_symbol(engine, settings, EMAMI, now=NOW)

    again = ingest_symbol(engine, settings, EMAMI, now=NOW + timedelta(hours=12))

    assert _requested_lengths(route) == [MAX_HISTORY_ROWS, RECENT_ROWS]
    assert again["inserted"] == 0
    assert again["skipped_other_source"] == RECENT_ROWS


@respx.mock
def test_the_gap_fill_is_stated_in_the_note_before_the_splice_sentence(engine, settings):
    _seed_emami_like_production(engine)
    splice = f"{SPLICE_NOTE_MARKER} The definition of this series CHANGES on 2026-04-27."
    with engine.begin() as conn:
        conn.execute(insert(instruments).values(
            code=EMAMI, kind="market_price", name_en="Emami gold coin", name_fa="سکه امامی",
            domain="gold", quote_currency="IRT", unit="coin", decimals=0,
            calendar_class="tehran_bazaar", quality_tier="official_mirror",
            is_proxy=False, is_derived=False, enabled=True,
            notes=f"Emami coin. {splice}", created_at=utcnow(), updated_at=utcnow(),
        ))
    _serve_fixture("sekee")

    report = ingest_symbol(engine, settings, EMAMI, now=NOW)

    assert report["instrument_note_updated"] is True
    with engine.connect() as conn:
        notes = conn.execute(select(instruments.c.notes)
                             .where(instruments.c.code == EMAMI)).scalar_one()
    assert notes.startswith("Emami coin. [tgju daily gap-fill]")
    assert notes.index(GAP_FILL_NOTE_MARKER) < notes.index(SPLICE_NOTE_MARKER)
    assert notes.endswith(splice)
    assert f"On {report['inserted']} UTC day(s)" in notes
    assert "sekee" in notes and "not a live quote" in notes
    # tgju_backfill keeps what precedes its marker when it rewrites its own
    # sentence, so this one survives a re-run of the deep backfill.
    assert GAP_FILL_NOTE_MARKER in notes.split(SPLICE_NOTE_MARKER, 1)[0]
    stats = _register(engine)[EMAMI]["gap_fill"]
    assert stats["days"] == report["inserted"]


def test_usd_and_18k_are_refused_with_their_reasons(engine, settings):
    with pytest.raises(ValueError) as exc:
        run_tgju_daily(engine, settings, symbols=["USD_IRT", "IR_GOLD_18K", "NOPE"], now=NOW)
    message = str(exc.value)
    assert "USDT/toman" in message
    assert "IR_GOLD_18K" in message and "no empty day" in message
    assert "NOPE: not a TGJU daily-close symbol" in message


# --- the whole pass ---------------------------------------------------------------


@respx.mock
def test_the_default_pass_covers_all_eight_and_isolates_a_failing_slug(engine, settings):
    _serve_all(**{"nim": 500})

    result = run_tgju_daily(engine, settings, now=NOW)

    assert [r["symbol"] for r in result["symbols"]] == list(SERIES_SLUGS) + [EMAMI]
    statuses = {r["symbol"]: r["status"] for r in result["symbols"]}
    assert statuses["IR_COIN_HALF"] == "error"
    assert {s for s, v in statuses.items() if v != "ok"} == {"IR_COIN_HALF"}
    assert result["failed"][0]["symbol"] == "IR_COIN_HALF"
    assert result["total_inserted"] > 0
    assert _stored_days(engine, "IR_COIN_HALF") == []
    assert len(_stored_days(engine, "IR_GOLD_MESGHAL")) == 47


@respx.mock
def test_a_pass_where_every_symbol_fails_raises_with_the_report(engine, settings):
    _serve_all(**{slug: 503 for slug in {**SERIES_SLUGS, **GAP_FILL_SLUGS}.values()})

    with pytest.raises(DailyIngestFailed) as exc:
        run_tgju_daily(engine, settings, now=NOW)

    assert len(exc.value.report["failed"]) == 8
    assert _count(engine, prices) == 0


# --- endpoint -----------------------------------------------------------------------


def _stub_history(monkeypatch, fail=False):
    def fake(self, slug, max_rows=None):
        if fail:
            raise tgju.ProviderError("tgju: request failed")
        name = "tgju_daily_silver_999_2026.json" if slug == "silver_999" else _fixture_for(slug)
        payload = load_fixture_json(name)
        if max_rows and max_rows < len(payload["data"]):
            payload = dict(payload, data=payload["data"][:max_rows])
        return tgju.history_page(payload, slug)

    monkeypatch.setattr(tgju.TGJUProvider, "fetch_history_page", fake)


def test_endpoint_stores_the_closes_and_is_idempotent(client, engine, monkeypatch):
    _stub_history(monkeypatch)

    first = client.post("/internal/tgju/daily", json={"symbols": ["IR_COIN_BAHAR"]},
                        headers=AUTH)
    again = client.post("/internal/tgju/daily", json={"symbols": ["IR_COIN_BAHAR"]},
                        headers=AUTH)

    assert first.status_code == 200
    body = first.json()
    # Wall-clock run: every fixture bar (<= 2026-09-28) has long settled.
    assert body["total_inserted"] == 47
    assert body["symbols"][0]["status"] == "ok"
    assert again.json()["total_inserted"] == 0
    assert len(_stored_days(engine, "IR_COIN_BAHAR")) == 47


def test_endpoint_dry_run_writes_nothing(client, engine, monkeypatch):
    _stub_history(monkeypatch)

    resp = client.post("/internal/tgju/daily", json={"dry_run": True}, headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["total_would_insert"] > 0
    assert body["total_inserted"] == 0
    assert _count(engine, prices) == 0


def test_endpoint_refuses_usd_irt_with_400(client):
    resp = client.post("/internal/tgju/daily", json={"symbols": ["USD_IRT"]}, headers=AUTH)
    assert resp.status_code == 400
    assert "USDT/toman" in resp.json()["error"]["message"]


def test_endpoint_answers_502_when_every_symbol_failed(client, monkeypatch):
    _stub_history(monkeypatch, fail=True)

    resp = client.post("/internal/tgju/daily", json={}, headers=AUTH)

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "upstream_failed"
    assert len(body["failed"]) == 8


def test_endpoint_requires_the_internal_token(client):
    assert client.post("/internal/tgju/daily", json={}).status_code == 401


def test_the_job_is_not_part_of_live_collection():
    """Daily settled closes only: collect must never ask a provider for these."""
    from app.jobs.collect import JOB_SYMBOLS

    collected = set().union(*JOB_SYMBOLS.values())
    assert not collected & set(SERIES_SLUGS)
    assert tgju_daily.RECENT_ROWS < MAX_HISTORY_ROWS
