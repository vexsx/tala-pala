"""Trend-alignment rules: the exact cases the specification pins."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.trend_alignment import (Candle, TrendConfig, alignment,
                                        bucket_start, completed, ema, evaluate,
                                        evaluate_timeframe, resample, sma,
                                        trend_state)

UTC = timezone.utc


# --- trend_state -------------------------------------------------------------

def test_bullish_strict_stack():
    assert trend_state(110, 100, 90, 80) == "bullish"


def test_bearish_strict_stack():
    assert trend_state(70, 80, 90, 100) == "bearish"


def test_neutral_when_stack_is_broken():
    # price above ma26 but ma26 below ma48 -> not a trend
    assert trend_state(110, 100, 105, 80) == "neutral"


def test_equal_values_are_neutral_not_bullish():
    """Two equal MAs are a crossover in progress, not a trend."""
    assert trend_state(110, 100, 100, 80) == "neutral"
    assert trend_state(100, 100, 90, 80) == "neutral"


def test_missing_input_is_unavailable():
    assert trend_state(110, 100, None, 80) == "unavailable"
    assert trend_state(None, 100, 90, 80) == "unavailable"


# --- alignment ---------------------------------------------------------------

def test_full_bullish_requires_all_three():
    assert alignment("bullish", "bullish", "bullish") == "full_bullish"


def test_full_bearish_requires_all_three():
    assert alignment("bearish", "bearish", "bearish") == "full_bearish"


def test_mixed_is_not_aligned():
    assert alignment("bullish", "bullish", "bearish") == "not_aligned"
    assert alignment("bearish", "neutral", "bearish") == "not_aligned"


def test_unavailable_timeframe_blocks_alignment():
    assert alignment("bullish", "bullish", "unavailable") == "not_aligned"
    assert alignment("bearish", "unavailable", "bearish") == "not_aligned"


# --- moving averages ---------------------------------------------------------

def test_sma_and_ema_need_full_period():
    assert sma([1, 2, 3], 5) is None
    assert ema([1, 2, 3], 5) is None
    assert sma([1, 2, 3, 4], 4) == pytest.approx(2.5)


def test_ema_is_seeded_with_the_sma_of_the_first_window():
    # With exactly `period` values the EMA equals the seed SMA.
    assert ema([2, 4, 6, 8], 4) == pytest.approx(5.0)
    # A rising series pulls the EMA above the seed but below the last value.
    value = ema([1, 2, 3, 4, 10], 4)
    assert 2.5 < value < 10


# --- candle mechanics --------------------------------------------------------

def test_bucket_start_floors_in_utc_epoch_space():
    """A local-time floor would shift 4H boundaries by the zone offset."""
    at = datetime(2026, 8, 9, 7, 45, tzinfo=UTC)
    assert bucket_start(at, 4 * 3600) == datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    tehran = timezone(timedelta(hours=3, minutes=30))
    assert bucket_start(at.astimezone(tehran), 4 * 3600) == datetime(2026, 8, 9, 4, 0, tzinfo=UTC)


def _hours(n: int, start: datetime, price=lambda i: 100.0 + i) -> list[Candle]:
    return [
        Candle(start=start + timedelta(hours=i), open=price(i), high=price(i) + 1,
               low=price(i) - 1, close=price(i))
        for i in range(n)
    ]


def test_resample_uses_true_ohlc_aggregation():
    start = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    bars = _hours(4, start)
    out = resample(bars, 4 * 3600)
    assert len(out) == 1
    assert out[0].start == start
    assert out[0].open == bars[0].open
    assert out[0].close == bars[-1].close
    assert out[0].high == max(b.high for b in bars)
    assert out[0].low == min(b.low for b in bars)


def test_forming_candle_is_excluded():
    start = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    bars = _hours(3, start)
    # now sits inside the third candle: only two are complete.
    now = start + timedelta(hours=2, minutes=30)
    assert len(completed(bars, 3600, now)) == 2


# --- timeframe evaluation ----------------------------------------------------

def _rising(n: int) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return _hours(n, start, price=lambda i: 100.0 + i * 0.5)


def test_insufficient_history_is_unavailable_not_neutral():
    cfg = TrendConfig()
    bars = _rising(100)  # < 220
    now = bars[-1].start + timedelta(hours=1)
    res = evaluate_timeframe("1h", bars, now, cfg)
    assert res.trend == "unavailable"
    assert "220" in res.reason
    assert res.ma220 is None


def test_stale_series_is_unavailable():
    cfg = TrendConfig()
    bars = _rising(300)
    now = bars[-1].start + timedelta(hours=50)  # long past the last candle
    res = evaluate_timeframe("1h", bars, now, cfg)
    assert res.trend == "unavailable"
    assert res.data_fresh is False
    assert "buckets old" in res.reason


def test_rising_series_reports_bullish_from_the_closed_candle():
    cfg = TrendConfig()
    bars = _rising(400)
    now = bars[-1].start + timedelta(hours=1)
    res = evaluate_timeframe("1h", bars, now, cfg)
    assert res.trend == "bullish"
    assert res.confirmed is True
    assert res.price > res.ma26 > res.ma48 > res.ma220
    # The reported price is the CLOSED candle's close, not a live tick.
    assert res.price == bars[-1].close


def test_falling_series_reports_bearish():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = _hours(400, start, price=lambda i: 1000.0 - i * 0.5)
    now = bars[-1].start + timedelta(hours=1)
    res = evaluate_timeframe("1h", bars, now, cfg := TrendConfig())
    assert res.trend == "bearish"
    assert res.price < res.ma26 < res.ma48 < res.ma220


def test_ma_type_is_configurable():
    """EMA and SMA must be genuinely different maths, not a relabelled field.

    A constant-slope ramp is the one series where they coincide at steady
    state, so the difference is probed with a curved series instead.
    """
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = _hours(400, start, price=lambda i: 100.0 + (i ** 1.7) / 500.0)
    now = bars[-1].start + timedelta(hours=1)
    e = evaluate_timeframe("1h", bars, now, TrendConfig(ma_type="ema"))
    s = evaluate_timeframe("1h", bars, now, TrendConfig(ma_type="sma"))
    assert e.ma_type == "ema" and s.ma_type == "sma"
    assert e.ma220 != s.ma220
    # On an accelerating series the EMA tracks the recent end more closely.
    assert e.ma220 > s.ma220


def test_invalid_periods_are_rejected():
    for bad in (TrendConfig(fast=48, mid=26), TrendConfig(slow=10),
                TrendConfig(ma_type="wma")):  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            bad.validate()


# --- full evaluation ---------------------------------------------------------

def test_evaluate_computes_three_independent_timeframes():
    hourly = _rising(1200)
    daily_start = datetime(2024, 1, 1, tzinfo=UTC)
    daily = [
        Candle(start=daily_start + timedelta(days=i), open=100.0 + i, high=101.0 + i,
               low=99.0 + i, close=100.0 + i)
        for i in range(400)
    ]
    now = max(hourly[-1].start, daily[-1].start) + timedelta(days=1)
    res = evaluate("IR_GOLD_18K", hourly, daily, now)
    assert set(res.timeframes) == {"1d", "4h", "1h"}
    # 4H is resampled from hourly, so it has ~1/4 the candles.
    assert res.timeframes["4h"].history_points < res.timeframes["1h"].history_points
    # Identity covers all three timeframes. The values may coincide when a
    # boundary is shared (an hour close at 00:00 is also a 4H close), so the
    # contract is completeness, not distinctness.
    ident = res.candle_identity()
    assert set(ident) == {"1d", "4h", "1h"}
    assert all(v is not None for v in ident.values())


def test_candle_identity_is_stable_for_the_same_closed_candles():
    hourly = _rising(1200)
    daily = [
        Candle(start=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i),
               open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.0 + i)
        for i in range(400)
    ]
    now = datetime(2026, 6, 1, tzinfo=UTC)
    a = evaluate("IR_GOLD_18K", hourly, daily, now)
    b = evaluate("IR_GOLD_18K", hourly, daily, now + timedelta(minutes=7))
    assert a.candle_identity() == b.candle_identity(), "identity must not drift within a bucket"
