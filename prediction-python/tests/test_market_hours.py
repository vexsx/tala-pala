"""Market-hours awareness (Addendum 1): 24h Iranian gold, windowed Tehran
session, global weekend, last-session freshness — all with fixed datetimes so
results never depend on when the suite runs.  2026-07-20 is a Monday;
Asia/Tehran is a fixed UTC+03:30 (no DST since 2022), so 09:00 Tehran =
05:30 UTC and 20:00 Tehran = 16:30 UTC.  Reference week: 2026-07-15 Wed,
16 Thu, 17 Fri, 18 Sat, 19 Sun, 20 Mon."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.core.market_hours import (
    DAILY_CLOSE_STALE_DAYS,
    SOURCE_TGJU_HISTORY,
    SOURCE_TSETMC_CLOSES,
    TGJU_DAILY_CLOSES,
    WEEKLY_FETCH_CLOSES,
    WEEKLY_FETCH_STALE_DAYS,
    closure_started_at,
    daily_close_max_age,
    is_acceptably_fresh,
    is_market_open,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture()
def mh_settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        stale_minutes=30,
        market_tehran_open="09:00",
        market_tehran_close="20:00",
    )


# --- IR_GOLD_18K: 24h on Iranian trading days (Sat-Wed), Thu+Fri closed ------


def test_18k_trades_around_the_clock_on_trading_days(mh_settings):
    # Monday 2026-07-20: open at every hour — the Tehran window is ignored
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 5, 29), mh_settings)   # 08:59 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 12, 0), mh_settings)   # 15:30 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 16, 30), mh_settings)  # 20:00 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 20, 0), mh_settings)   # 23:30 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 19, 20, 31), mh_settings)  # Mon 00:01 Tehran


def test_18k_and_usd_open_all_week(mh_settings):
    # Always-open symbols (Hamrah Gold / USDT market quote 24/7): Thursday
    # 2026-07-16 and Friday 2026-07-17 are OPEN at any hour
    for day in (16, 17):
        for hour in (5, 8, 12, 16):
            assert is_market_open("IR_GOLD_18K", utc(2026, 7, day, hour, 0), mh_settings)
            assert is_market_open("USD_IRT", utc(2026, 7, day, hour, 0), mh_settings)


def test_18k_ignores_configured_window(mh_settings):
    # Even a narrow custom window changes nothing for the 24h symbol
    mh_settings.market_tehran_open = "10:30"
    mh_settings.market_tehran_close = "18:00"
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 6, 30), mh_settings)   # 10:00 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 14, 30), mh_settings)  # 18:00 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 16, 12, 0), mh_settings)   # Thursday too


def test_always_open_symbols_never_enter_closure(mh_settings):
    # 18k and USD never close: closure_started_at is None at any instant
    for day, hour in ((16, 8), (17, 8), (18, 1), (20, 23)):
        assert closure_started_at(
            "IR_GOLD_18K", utc(2026, 7, day, hour, 30), mh_settings
        ) is None
        assert closure_started_at(
            "USD_IRT", utc(2026, 7, day, hour, 30), mh_settings
        ) is None


# --- windowed Iranian symbols: Sat-Wed 09:00-20:00 Tehran, Thu+Fri closed ----


def test_tehran_open_hours_monday(mh_settings):
    # Monday 2026-07-20; boundaries: open inclusive, close exclusive.
    # The coin is the windowed symbol now (USD moved to always-open).
    mh_settings.market_tehran_open = "09:00"
    assert not is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 5, 29), mh_settings)  # 08:59
    assert is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 5, 30), mh_settings)      # 09:00
    assert is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 12, 0), mh_settings)      # 15:30
    assert is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 16, 29), mh_settings)     # 19:59
    assert not is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 16, 30), mh_settings)  # 20:00


def test_tehran_closed_all_thursday_and_friday(mh_settings):
    # Thursday 2026-07-16 and Friday 2026-07-17, mid-session hours still closed
    # for the windowed coin symbol (18k/USD stay open: always-open sources)
    for day in (16, 17):
        for hour in (5, 8, 12, 16):
            assert not is_market_open("IR_COIN_EMAMI", utc(2026, 7, day, hour, 0), mh_settings)


def test_tehran_saturday_is_a_trading_day(mh_settings):
    # Saturday 2026-07-18 12:30 Tehran = 09:00 UTC (coin window open)
    assert is_market_open("IR_COIN_EMAMI", utc(2026, 7, 18, 9, 0), mh_settings)


def test_tehran_configurable_hours(mh_settings):
    mh_settings.market_tehran_open = "11:30"
    mh_settings.market_tehran_close = "18:00"
    assert not is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 7, 30), mh_settings)  # 11:00
    assert is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 8, 0), mh_settings)       # 11:30
    assert not is_market_open("IR_COIN_EMAMI", utc(2026, 7, 20, 14, 30), mh_settings)  # 18:00


def test_iranian_closure_start_overnight(mh_settings):
    # Monday 04:30 UTC (08:00 Tehran, before open): closure began Sunday
    # 20:00 Tehran = Sunday 16:30 UTC (coin: the windowed symbol)
    assert closure_started_at(
        "IR_COIN_EMAMI", utc(2026, 7, 20, 4, 30), mh_settings
    ) == utc(2026, 7, 19, 16, 30)


def test_iranian_closure_start_skips_thursday_and_friday(mh_settings):
    # Thursday noon, Friday noon and Saturday pre-open all trace back to
    # WEDNESDAY's close (2026-07-15 20:00 Tehran = 16:30 UTC) — neither
    # Thursday nor Friday has a session to close
    wednesday_close = utc(2026, 7, 15, 16, 30)
    assert closure_started_at(
        "IR_COIN_EMAMI", utc(2026, 7, 16, 8, 30), mh_settings
    ) == wednesday_close
    assert closure_started_at(
        "IR_COIN_EMAMI", utc(2026, 7, 17, 8, 30), mh_settings
    ) == wednesday_close
    assert closure_started_at(
        "IR_COIN_EMAMI", utc(2026, 7, 18, 1, 30), mh_settings  # Sat 05:00 Tehran
    ) == wednesday_close


TGJU_DAILY_CODES = (
    "IR_SILVER_999", "IR_COIN_BAHAR", "IR_COIN_HALF", "IR_COIN_QUARTER",
    "IR_COIN_GERAMI", "IR_GOLD_24K", "IR_GOLD_MESGHAL",
)


@pytest.mark.parametrize("symbol", TGJU_DAILY_CODES)
def test_the_tgju_daily_codes_keep_the_bazaar_calendar(symbol, mh_settings):
    """Migration 0031's codes are bazaar closes, not a 24/7 or global quote."""
    assert is_market_open(symbol, utc(2026, 7, 20, 8, 0), mh_settings)       # Mon 11:30
    assert not is_market_open(symbol, utc(2026, 7, 16, 8, 30), mh_settings)  # Thursday
    assert not is_market_open(symbol, utc(2026, 7, 17, 8, 30), mh_settings)  # Friday
    assert is_market_open(symbol, utc(2026, 7, 18, 9, 0), mh_settings)       # Saturday
    # A close from Wednesday's session is still the latest settled one on Friday.
    assert closure_started_at(symbol, utc(2026, 7, 17, 8, 30), mh_settings) == utc(
        2026, 7, 15, 16, 30)


def test_commodity_funds_of_every_underlying_follow_the_tse_session(mh_settings):
    """Silver and saffron funds get the session the gold funds always had."""
    from app.core.market_hours import is_tse_fund

    for symbol in ("IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_FLOW", "IR_SILVER_FUND_SILVER",
                   "IR_SILVER_FUND_SIMIN", "IR_SAFFRON_FUND_SAFRON"):
        assert is_tse_fund(symbol), symbol
        assert is_market_open(symbol, utc(2026, 7, 21, 9, 30), mh_settings)        # Tue 13:00
        assert not is_market_open(symbol, utc(2026, 7, 21, 14, 30), mh_settings)   # Tue 18:00
        assert not is_market_open(symbol, utc(2026, 7, 23, 9, 30), mh_settings)    # Thursday
    for symbol in ("IR_SILVER_999", "IR_GOLD_18K", "IR_COIN_EMAMI", "FUND", "XAUUSD"):
        assert not is_tse_fund(symbol), symbol


def test_closure_start_none_while_open(mh_settings):
    assert closure_started_at("IR_GOLD_18K", utc(2026, 7, 20, 12, 0), mh_settings) is None
    assert closure_started_at("USD_IRT", utc(2026, 7, 20, 12, 0), mh_settings) is None
    assert closure_started_at("XAUUSD", utc(2026, 7, 22, 12, 0), mh_settings) is None


# --- Global symbols: closed Fri 21:00 UTC -> Sun 22:00 UTC -------------------


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD", "BRENT_OIL", "DXY", "US10Y"])
def test_global_weekend_boundaries(symbol, mh_settings):
    assert is_market_open(symbol, utc(2026, 7, 17, 20, 59), mh_settings)      # Fri 20:59
    assert not is_market_open(symbol, utc(2026, 7, 17, 21, 0), mh_settings)   # Fri 21:00
    assert not is_market_open(symbol, utc(2026, 7, 18, 12, 0), mh_settings)   # Saturday
    assert not is_market_open(symbol, utc(2026, 7, 19, 21, 59), mh_settings)  # Sun 21:59
    assert is_market_open(symbol, utc(2026, 7, 19, 22, 0), mh_settings)       # Sun 22:00
    assert is_market_open(symbol, utc(2026, 7, 22, 3, 0), mh_settings)        # Wed night


def test_global_closure_start_is_friday_2100(mh_settings):
    friday_close = utc(2026, 7, 17, 21, 0)
    for closed_at in (
        utc(2026, 7, 17, 21, 0),   # the boundary itself
        utc(2026, 7, 18, 12, 0),   # Saturday
        utc(2026, 7, 19, 21, 59),  # Sunday just before reopen
    ):
        assert closure_started_at("XAUUSD", closed_at, mh_settings) == friday_close


# --- unknown symbols: no calendar, always-open semantics ---------------------


def test_unknown_symbol_always_open(mh_settings):
    assert is_market_open("SOMETHING_ELSE", utc(2026, 7, 18, 12, 0), mh_settings)
    assert closure_started_at("SOMETHING_ELSE", utc(2026, 7, 18, 12, 0), mh_settings) is None


# --- is_acceptably_fresh -----------------------------------------------------


def test_fresh_while_open_uses_plain_age_rule(mh_settings):
    now = utc(2026, 7, 20, 12, 0)  # Monday, Tehran market open
    assert is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 20, 11, 30), now, mh_settings)
    assert not is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 20, 11, 29), now, mh_settings)
    assert not is_acceptably_fresh("IR_GOLD_18K", None, now, mh_settings)


def test_18k_open_evening_uses_plain_age_rule(mh_settings):
    # Monday 21:30 Tehran (18:00 UTC): open for the 24h symbol, so a paused
    # feed goes honestly stale after STALE_MINUTES
    now = utc(2026, 7, 20, 18, 0)
    assert is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 20, 17, 30), now, mh_settings)
    assert not is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 20, 17, 29), now, mh_settings)


def test_last_session_data_is_fresh_during_iranian_closure(mh_settings):
    # IR_COIN_EMAMI Monday 04:30 UTC: closed since Sunday 16:30 UTC;
    # threshold 16:00 UTC
    now = utc(2026, 7, 20, 4, 30)
    assert is_acceptably_fresh("IR_COIN_EMAMI", utc(2026, 7, 19, 16, 20), now, mh_settings)
    assert is_acceptably_fresh("IR_COIN_EMAMI", utc(2026, 7, 19, 16, 0), now, mh_settings)   # boundary
    assert not is_acceptably_fresh("IR_COIN_EMAMI", utc(2026, 7, 19, 15, 59), now, mh_settings)


def test_always_open_symbols_use_plain_age_rule_on_offdays(mh_settings):
    # 18k and USD are open on Thursday/Friday too: the plain STALE_MINUTES
    # rule applies — data older than the window is honestly stale even
    # though the sources quote continuously with lower update frequency
    now = utc(2026, 7, 17, 18, 0)  # Friday evening
    for symbol in ("IR_GOLD_18K", "USD_IRT"):
        assert is_acceptably_fresh(symbol, utc(2026, 7, 17, 17, 31), now, mh_settings)
        assert not is_acceptably_fresh(symbol, utc(2026, 7, 17, 17, 29), now, mh_settings)


def test_global_weekend_last_session_freshness(mh_settings):
    now = utc(2026, 7, 19, 12, 0)  # Sunday, global market closed
    # closure began Fri 21:00; threshold Fri 20:30
    assert is_acceptably_fresh("XAUUSD", utc(2026, 7, 17, 20, 45), now, mh_settings)
    assert is_acceptably_fresh("XAUUSD", utc(2026, 7, 17, 20, 30), now, mh_settings)  # boundary
    assert not is_acceptably_fresh("XAUUSD", utc(2026, 7, 17, 18, 0), now, mh_settings)


def test_naive_datetimes_are_treated_as_utc(mh_settings):
    now = datetime(2026, 7, 20, 12, 0)  # naive -> UTC (Monday, open)
    assert is_market_open("IR_GOLD_18K", now, mh_settings)
    assert is_acceptably_fresh(
        "IR_GOLD_18K", datetime(2026, 7, 20, 11, 45), now, mh_settings
    )


# --- one settled close per session: stale after four calendar days -----------


@pytest.mark.parametrize("symbol", ["IR_SILVER_999", "IR_GOLD_MESGHAL", "IR_COIN_BAHAR"])
def test_a_daily_close_is_fresh_until_it_is_more_than_four_days_old(mh_settings, symbol):
    """Mirrors backend-go markethours: yesterday's 23:00 UTC close is the
    newest that can exist during today's session, so the minutes rule can only
    call it stale."""
    wednesday_close = utc(2026, 7, 15, 23, 0)
    assert is_acceptably_fresh(symbol, utc(2026, 7, 19, 23, 0), utc(2026, 7, 20, 9, 30),
                               mh_settings)
    assert is_acceptably_fresh(symbol, wednesday_close, utc(2026, 7, 18, 6, 0), mh_settings)
    assert is_acceptably_fresh(symbol, wednesday_close, utc(2026, 7, 19, 20, 29), mh_settings)
    assert not is_acceptably_fresh(symbol, wednesday_close, utc(2026, 7, 19, 20, 31), mh_settings)


def test_live_quote_series_keep_the_session_rule(mh_settings):
    for symbol in ("IR_GOLD_FUND_AYAR", "IR_COIN_EMAMI"):
        assert not is_acceptably_fresh(symbol, utc(2026, 7, 19, 23, 0), utc(2026, 7, 20, 9, 30),
                                       mh_settings)


@pytest.mark.parametrize("symbol", [
    "IR_SILVER_FUND_SILVER", "IR_SILVER_FUND_SIMIN", "IR_GOLD_FUND_KAHRABA",
])
@pytest.mark.parametrize("source", [None, "tsetmc_cdn"])
def test_a_weekly_fetched_fund_close_keeps_the_weekly_runs_bound(mh_settings, symbol, source):
    """Mirrors backend-go: Wednesday's close stored by a Friday run read stale
    from Monday to Thursday on the four-day rule, beside index charts of the
    same weekly run reading fresh; its bound is that run's, ten days."""
    wednesday_close = utc(2026, 7, 15, 23, 0)
    for day in range(18, 26):
        assert is_acceptably_fresh(symbol, wednesday_close, utc(2026, 7, day, 9, 30), mh_settings,
                                   source=source)
    assert not is_acceptably_fresh(symbol, wednesday_close, utc(2026, 7, 26, 9, 30), mh_settings,
                                   source=source)


def test_the_newest_rows_source_decides_the_daily_close_rule(mh_settings):
    """A fund configured live (TSETMC_FUNDS naming کهربا) keeps the session rule
    for its live quotes; a symbol with a live source by default is never
    judged as a daily close, whatever wrote its newest row."""
    monday = utc(2026, 7, 20, 9, 30)
    assert not is_acceptably_fresh("IR_GOLD_FUND_KAHRABA", utc(2026, 7, 18, 9, 0), monday,
                                   mh_settings, source="tse_funds")
    assert daily_close_max_age("IR_GOLD_FUND_KAHRABA", "tse_funds") is None
    assert daily_close_max_age("IR_GOLD_FUND_KAHRABA", "tsetmc_cdn") == WEEKLY_FETCH_STALE_DAYS
    assert daily_close_max_age("IR_SILVER_999", "tgju_history") == DAILY_CLOSE_STALE_DAYS
    for symbol in ("IR_COIN_EMAMI", "IR_GOLD_18K", "IR_GOLD_FUND_AYAR"):
        assert daily_close_max_age(symbol, "tgju_history") is None
        assert daily_close_max_age(symbol, "tsetmc_cdn") is None


def test_the_rule_matches_backend_go():
    """The Go package is the other half of this mirror: same lists, same bounds."""
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "backend-go", "internal", "markethours", "markethours.go"),
        encoding="utf-8").read()
    def block(name):
        body = source.split(name + " = map[string]bool{", 1)[1].split("}", 1)[0]
        return frozenset(re.findall(r'"([A-Z0-9_]+)"', body))
    assert block("tgjuDailyCloses") == TGJU_DAILY_CLOSES
    assert block("weeklyFetchCloses") == WEEKLY_FETCH_CLOSES
    assert f"const DailyCloseStaleDays = {DAILY_CLOSE_STALE_DAYS}" in source
    assert f"const WeeklyFetchStaleDays = {WEEKLY_FETCH_STALE_DAYS}" in source
    assert f'SourceTGJUHistory = "{SOURCE_TGJU_HISTORY}"' in source
    assert f'SourceTSETMCCloses = "{SOURCE_TSETMC_CLOSES}"' in source
