"""Market-hours awareness (Addendum 1): 24h Iranian gold, windowed Tehran
session, global weekend, last-session freshness — all with fixed datetimes so
results never depend on when the suite runs.  2026-07-20 is a Monday;
Asia/Tehran is a fixed UTC+03:30 (no DST since 2022), so 09:00 Tehran =
05:30 UTC and 20:00 Tehran = 16:30 UTC.  Reference week: 2026-07-15 Wed,
16 Thu, 17 Fri, 18 Sat, 19 Sun, 20 Mon."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.core.market_hours import (
    closure_started_at,
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


def test_18k_closed_all_thursday_and_friday(mh_settings):
    # Thursday 2026-07-16 and Friday 2026-07-17: closed at any hour
    for day in (16, 17):
        for hour in (5, 8, 12, 16):
            assert not is_market_open("IR_GOLD_18K", utc(2026, 7, day, hour, 0), mh_settings)


def test_18k_ignores_configured_window(mh_settings):
    # Even a narrow custom window changes nothing for the 24h symbol
    mh_settings.market_tehran_open = "10:30"
    mh_settings.market_tehran_close = "18:00"
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 6, 30), mh_settings)   # 10:00 Tehran
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 20, 14, 30), mh_settings)  # 18:00 Tehran
    assert not is_market_open("IR_GOLD_18K", utc(2026, 7, 16, 12, 0), mh_settings)  # Thursday


def test_18k_closure_starts_thursday_midnight_tehran(mh_settings):
    # Thursday noon and Friday noon Tehran both belong to the same Thu+Fri
    # block: closure began Thursday 00:00 Tehran = Wednesday 20:30 UTC
    block_start = utc(2026, 7, 15, 20, 30)
    assert closure_started_at(
        "IR_GOLD_18K", utc(2026, 7, 16, 8, 30), mh_settings
    ) == block_start
    assert closure_started_at(
        "IR_GOLD_18K", utc(2026, 7, 17, 8, 30), mh_settings
    ) == block_start
    # Saturday 05:00 Tehran (2026-07-18 01:30 UTC) is a trading day again:
    # open around the clock, so no closure in progress
    assert closure_started_at(
        "IR_GOLD_18K", utc(2026, 7, 18, 1, 30), mh_settings
    ) is None


# --- windowed Iranian symbols: Sat-Wed 09:00-20:00 Tehran, Thu+Fri closed ----


def test_tehran_open_hours_monday(mh_settings):
    # Monday 2026-07-20; boundaries: open inclusive, close exclusive
    assert not is_market_open("USD_IRT", utc(2026, 7, 20, 5, 29), mh_settings)  # 08:59
    assert is_market_open("USD_IRT", utc(2026, 7, 20, 5, 30), mh_settings)      # 09:00
    assert is_market_open("USD_IRT", utc(2026, 7, 20, 12, 0), mh_settings)      # 15:30
    assert is_market_open("USD_IRT", utc(2026, 7, 20, 16, 29), mh_settings)     # 19:59
    assert not is_market_open("USD_IRT", utc(2026, 7, 20, 16, 30), mh_settings)  # 20:00


def test_tehran_closed_all_thursday_and_friday(mh_settings):
    # Thursday 2026-07-16 and Friday 2026-07-17, mid-session hours still closed
    for day in (16, 17):
        for hour in (5, 8, 12, 16):
            assert not is_market_open("USD_IRT", utc(2026, 7, day, hour, 0), mh_settings)
            assert not is_market_open("IR_COIN_EMAMI", utc(2026, 7, day, hour, 0), mh_settings)


def test_tehran_saturday_is_a_trading_day(mh_settings):
    # Saturday 2026-07-18 10:00 Tehran = 06:30 UTC
    assert is_market_open("USD_IRT", utc(2026, 7, 18, 6, 30), mh_settings)


def test_tehran_configurable_hours(mh_settings):
    mh_settings.market_tehran_open = "10:30"
    mh_settings.market_tehran_close = "18:00"
    assert not is_market_open("USD_IRT", utc(2026, 7, 20, 6, 30), mh_settings)  # 10:00
    assert is_market_open("USD_IRT", utc(2026, 7, 20, 7, 0), mh_settings)       # 10:30
    assert not is_market_open("USD_IRT", utc(2026, 7, 20, 14, 30), mh_settings)  # 18:00


def test_iranian_closure_start_overnight(mh_settings):
    # Monday 04:30 UTC (08:00 Tehran, before open): closure began Sunday
    # 20:00 Tehran = Sunday 16:30 UTC
    assert closure_started_at(
        "USD_IRT", utc(2026, 7, 20, 4, 30), mh_settings
    ) == utc(2026, 7, 19, 16, 30)


def test_iranian_closure_start_skips_thursday_and_friday(mh_settings):
    # Thursday noon, Friday noon and Saturday pre-open all trace back to
    # WEDNESDAY's close (2026-07-15 20:00 Tehran = 16:30 UTC) — neither
    # Thursday nor Friday has a session to close
    wednesday_close = utc(2026, 7, 15, 16, 30)
    assert closure_started_at(
        "USD_IRT", utc(2026, 7, 16, 8, 30), mh_settings
    ) == wednesday_close
    assert closure_started_at(
        "USD_IRT", utc(2026, 7, 17, 8, 30), mh_settings
    ) == wednesday_close
    assert closure_started_at(
        "USD_IRT", utc(2026, 7, 18, 1, 30), mh_settings  # Sat 05:00 Tehran
    ) == wednesday_close


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
    # USD_IRT Monday 04:30 UTC: closed since Sunday 16:30 UTC; threshold
    # 16:00 UTC
    now = utc(2026, 7, 20, 4, 30)
    assert is_acceptably_fresh("USD_IRT", utc(2026, 7, 19, 16, 20), now, mh_settings)
    assert is_acceptably_fresh("USD_IRT", utc(2026, 7, 19, 16, 0), now, mh_settings)   # boundary
    assert not is_acceptably_fresh("USD_IRT", utc(2026, 7, 19, 15, 59), now, mh_settings)


def test_18k_pre_block_data_survives_thursday_and_friday(mh_settings):
    # 18k Friday evening: closure began Thursday 00:00 Tehran = Wednesday
    # 20:30 UTC; threshold 20:00 UTC.  Wednesday-night data is still
    # acceptably fresh, Wednesday-morning data is not
    now = utc(2026, 7, 17, 18, 0)
    assert is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 15, 20, 15), now, mh_settings)
    assert is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 15, 20, 0), now, mh_settings)   # boundary
    assert not is_acceptably_fresh("IR_GOLD_18K", utc(2026, 7, 15, 19, 59), now, mh_settings)


def test_usd_pre_block_data_survives_thursday_and_friday(mh_settings):
    # USD_IRT Friday evening: closure began Wednesday 20:00 Tehran = 16:30
    # UTC; Wednesday-session data is still acceptably fresh, older is not
    now = utc(2026, 7, 17, 18, 0)
    assert is_acceptably_fresh("USD_IRT", utc(2026, 7, 15, 16, 15), now, mh_settings)
    assert not is_acceptably_fresh("USD_IRT", utc(2026, 7, 15, 10, 0), now, mh_settings)


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
