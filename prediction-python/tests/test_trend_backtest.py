"""Track record of the trend alignment (``app/models/trend_backtest.py``).

Three things can go wrong in a backtest, and only one of them is a crash:

* **look-ahead** — a bar decided with knowledge it could not have had. Every
  number in the section becomes a lie, and the lie is flattering, so it is the
  first thing tested here and it is tested twice: once against the pure replay,
  and once against the job, where a price row stamped in the future must not
  reach a window that was already computed.
* **a mislabelled basis** — a daily-only result presented as the 1D+4H+1H
  alignment. The intraday series is weeks long and the daily series is years
  long, so the long windows CANNOT be the multi-timeframe measurement; a test
  that only checked the arithmetic would pass while the row lied about what was
  measured.
* **a zero standing in for an absence** — "never happened" stored as 0.0. It
  reads as a measurement of the indicator and it is the opposite.

Periods are shrunk to 2/3/4 exactly as in ``test_trend_alignment_job.py``: the
replay is period-agnostic, and a 220-period warm-up would only make the fixtures
slower, not more truthful.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import prices, trend_alignment_performance, trend_alignment_states
from app.jobs.trend_alignment import refresh_performance, run_trend_alignment, trend_config
from app.models.trend_alignment import Candle, TrendConfig
from app.models.trend_backtest import (
    FORWARD_HORIZON_SECONDS,
    WINDOW_DAYS,
    backtest,
    intraday_since,
    measure_window,
    replay,
)

UTC = timezone.utc
BASE = datetime(2026, 6, 1, tzinfo=UTC)
GOLD = "IR_GOLD_18K"

# The same shrunken periods the job tests use; warm-up is int(4 * 1.3) = 5.
CONFIG = TrendConfig(fast=2, mid=3, slow=4)
WARM = 5

# Every column the table stores, minus the two the job stamps itself. Pinned as
# a set so a Sharpe ratio, a p-value or any other significance decoration cannot
# be added without this test objecting.
ROW_COLUMNS = {
    "symbol", "window_days", "basis", "evaluated_from", "evaluated_to", "samples",
    "bullish_episodes", "bearish_episodes", "bullish_bars", "bearish_bars",
    "unaligned_bars", "fwd_return_bullish_pct", "fwd_return_bearish_pct",
    "fwd_return_baseline_pct", "hit_rate_bullish", "hit_rate_bearish", "note",
}


# --- fixture series ----------------------------------------------------------


def candles(values, start: datetime, step: timedelta) -> list[Candle]:
    """One flat OHLC candle per value, on a fixed grid."""
    return [
        Candle(start=start + step * i, open=float(v), high=float(v), low=float(v), close=float(v))
        for i, v in enumerate(values)
    ]


def daily_candles(values, start: datetime = BASE) -> list[Candle]:
    return candles(values, start, timedelta(days=1))


def hourly_candles(values, start: datetime = BASE) -> list[Candle]:
    return candles(values, start, timedelta(hours=1))


def ramp(count: int, start: float, step: float) -> list[float]:
    return [start + step * (i + 1) for i in range(count)]


# Flat, up, sharply down, up again: the daily leg walks bullish -> unaligned ->
# bearish -> unaligned -> bullish, so a single fixture exercises both directions,
# both hit rates and a re-entry (two bullish episodes, not one long one).
TURNING_VALUES = (
    [100.0] * 6 + ramp(10, 100.0, 3.0) + ramp(8, 130.0, -6.0) + ramp(12, 82.0, 4.0)
)
TURNING_NOW = BASE + timedelta(days=len(TURNING_VALUES))


def turning_series() -> list[Candle]:
    return daily_candles(TURNING_VALUES)


def measured_bars(bars):
    return [b for b in bars if b.forward_return_pct is not None]


# --- look-ahead: the property the whole section rests on ---------------------


def test_a_future_price_spike_cannot_change_a_past_bars_state():
    """Candles that had not closed at the bar's close must not be visible.

    The spike is appended AFTER ``now``, which is exactly the shape of the bug:
    a replay that hands the engine the whole series and forgets to say when
    "now" was would let tomorrow decide yesterday.
    """
    series = turning_series()
    window_start = TURNING_NOW - timedelta(days=60)
    before = replay(GOLD, "daily_only", [], series, window_start, TURNING_NOW, CONFIG)

    spiked = series + daily_candles([10_000.0, 20_000.0], start=TURNING_NOW)
    after = replay(GOLD, "daily_only", [], spiked, window_start, TURNING_NOW, CONFIG)

    assert after == before
    assert before, "the fixture must actually produce bars, or this proves nothing"


def test_a_later_candle_cannot_rewrite_an_earlier_bars_state():
    """Tampering INSIDE the series, not past its end.

    A bar's state may only depend on candles that had closed when it closed;
    its forward return, by construction, depends on a candle that had not. The
    cutoff bar's return is therefore allowed to move — its state is not.
    """
    tampered_values = list(TURNING_VALUES)
    tampered_values[30] = 100_000.0
    cutoff = BASE + timedelta(days=30)  # the tampered candle's OPEN
    window_start = TURNING_NOW - timedelta(days=90)

    plain = replay(GOLD, "daily_only", [], turning_series(), window_start, TURNING_NOW, CONFIG)
    spiked = replay(
        GOLD, "daily_only", [], daily_candles(tampered_values), window_start, TURNING_NOW, CONFIG
    )

    def states_until(bars):
        return [(b.close_time, b.state) for b in bars if b.close_time <= cutoff]

    assert states_until(spiked) == states_until(plain)
    # ...and the tampering really did reach the later bars, so the assertion
    # above is not vacuously comparing two identical replays.
    assert [b.state for b in spiked] != [b.state for b in plain]


def test_the_replay_never_scores_a_bar_on_a_candle_that_had_not_closed():
    """Each bar's forward return is the NEXT bucket's close, never a later one."""
    series = turning_series()
    bars = replay(
        GOLD, "daily_only", [], series, TURNING_NOW - timedelta(days=90), TURNING_NOW, CONFIG
    )
    closes = {c.start + timedelta(days=1): c.close for c in series}
    horizon = timedelta(seconds=FORWARD_HORIZON_SECONDS)
    for bar in bars:
        expected = closes.get(bar.close_time + horizon)
        if expected is None:
            assert bar.forward_return_pct is None
        else:
            assert bar.forward_return_pct == pytest.approx(
                (expected / bar.price - 1.0) * 100.0
            )


# --- which basis the data actually supports ----------------------------------


def test_one_tick_per_day_is_not_intraday_coverage():
    """The daily-only era yields one hourly bucket a day — not an hourly series.

    Treating those as 1H candles is the single mistake that would manufacture
    the intraday history this module exists to admit it does not have.
    """
    daily_era = [
        Candle(start=BASE + timedelta(days=i, hours=12), open=100.0, high=100.0,
               low=100.0, close=100.0)
        for i in range(40)
    ]
    assert intraday_since(daily_era, BASE + timedelta(days=40)) is None


def test_a_real_intraday_stream_is_detected_from_its_first_full_day():
    hourly = hourly_candles(ramp(240, 1000.0, 1.0))
    assert intraday_since(hourly, BASE + timedelta(days=10)) == BASE


def test_the_current_day_alone_is_not_yet_intraday_coverage():
    """A partial day holding one bucket looks exactly like a daily-only day."""
    hourly = [Candle(start=BASE + timedelta(hours=1), open=1.0, high=1.0, low=1.0, close=1.0)]
    assert intraday_since(hourly, BASE + timedelta(hours=3)) is None


def test_the_ninety_day_window_comes_back_daily_only_and_says_why():
    """The headline constraint: no 90-day multi-timeframe track record exists.

    Intraday history starts partway through the window, so the 4H and 1H legs
    cannot reach back that far and the row must say so in plain language rather
    than quietly presenting the daily leg as the alignment.
    """
    hourly = hourly_candles(ramp(240, 1000.0, 1.0), start=BASE + timedelta(days=80))
    daily = daily_candles(ramp(120, 500.0, 1.0))
    now = BASE + timedelta(days=90)

    row = measure_window(GOLD, 90, hourly, daily, now, CONFIG)

    assert row.basis == "daily_only"
    assert "NOT the multi-timeframe alignment" in row.note
    assert "intraday prices only begin" in row.note
    assert "4H and 1H legs have no history reaching back that far" in row.note


def test_no_intraday_history_at_all_is_reported_as_such():
    row = measure_window(GOLD, 30, [], turning_series(), TURNING_NOW, CONFIG)
    assert row.basis == "daily_only"
    assert "no intraday price history at all" in row.note


def test_full_mtf_is_chosen_only_when_all_three_legs_cover_the_window():
    """Ten days of true hourly candles support a 2-day multi-timeframe window."""
    hourly = hourly_candles(ramp(240, 1000.0, 1.0))
    daily = daily_candles(ramp(40, 500.0, 1.0), start=BASE - timedelta(days=30))
    now = BASE + timedelta(days=10)

    two_day = measure_window(GOLD, 2, hourly, daily, now, CONFIG)
    thirty_day = measure_window(GOLD, 30, hourly, daily, now, CONFIG)

    assert two_day.basis == "full_mtf"
    assert "all three legs warmed up" in two_day.note
    # Hourly bars each look a full day forward, so they overlap; the row has to
    # say so rather than let a reader count 24 samples as 24 independent facts.
    assert "not independent observations" in two_day.note
    # The same data cannot support a 30-day multi-timeframe window.
    assert thirty_day.basis == "daily_only"


def test_a_window_that_is_only_partly_covered_by_intraday_falls_back():
    """Full alignment is required at the window's FIRST bar, not somewhere in it.

    Otherwise a window beginning before the intraday stream would replay as one
    long "not aligned" stretch labelled multi-timeframe, which reads as "the
    indicator said nothing" when the truth is "it could not be computed".
    """
    hourly = hourly_candles(ramp(240, 1000.0, 1.0))
    daily = daily_candles(ramp(40, 500.0, 1.0), start=BASE - timedelta(days=30))
    now = BASE + timedelta(days=10)

    row = measure_window(GOLD, 11, hourly, daily, now, CONFIG)

    assert row.basis == "daily_only"
    assert "intraday prices only begin" in row.note


def test_the_daily_era_hourly_buckets_never_feed_the_intraday_legs():
    """A full_mtf row must be identical with and without the daily-era buckets.

    They exist in the loaded hourly series (one per day, for years) and would
    otherwise be fed to the 1H and 4H moving averages as if they were candles.
    """
    hourly = hourly_candles(ramp(240, 1000.0, 1.0))
    daily = daily_candles(ramp(40, 500.0, 1.0), start=BASE - timedelta(days=30))
    now = BASE + timedelta(days=10)
    daily_era_buckets = [
        Candle(start=BASE - timedelta(days=i, hours=12), open=9.0, high=9.0, low=9.0, close=9.0)
        for i in range(1, 31)
    ]

    clean = measure_window(GOLD, 2, hourly, daily, now, CONFIG)
    polluted = measure_window(GOLD, 2, sorted(daily_era_buckets + hourly,
                                              key=lambda c: c.start), daily, now, CONFIG)

    assert clean.basis == polluted.basis == "full_mtf"
    assert clean.as_row() == polluted.as_row()


def test_daily_only_rows_never_call_themselves_the_alignment():
    rows = backtest(GOLD, [], turning_series(), TURNING_NOW, CONFIG)
    for row in rows:
        assert row.basis == "daily_only"
        assert "NOT the multi-timeframe alignment" in row.note


# --- the statistics ----------------------------------------------------------


def test_windows_are_returned_longest_first():
    rows = backtest(GOLD, [], turning_series(), TURNING_NOW, CONFIG)
    assert [r.window_days for r in rows] == list(WINDOW_DAYS)


def test_the_state_buckets_add_up_to_samples():
    """samples is the denominator of every rate, so it must be the same bars."""
    for row in backtest(GOLD, [], turning_series(), TURNING_NOW, CONFIG):
        assert row.bullish_bars + row.bearish_bars + row.unaligned_bars == row.samples


def test_forward_returns_and_baseline_are_the_measured_bars():
    """Every stored statistic recomputed straight from the fixture closes."""
    window_start = TURNING_NOW - timedelta(days=60)
    bars = measured_bars(
        replay(GOLD, "daily_only", [], turning_series(), window_start, TURNING_NOW, CONFIG)
    )
    row = measure_window(GOLD, 60, [], turning_series(), TURNING_NOW, CONFIG)

    bullish = [b.forward_return_pct for b in bars if b.state == "bullish"]
    bearish = [b.forward_return_pct for b in bars if b.state == "bearish"]
    every = [b.forward_return_pct for b in bars]

    assert row.samples == len(every)
    assert row.fwd_return_bullish_pct == pytest.approx(sum(bullish) / len(bullish))
    assert row.fwd_return_bearish_pct == pytest.approx(sum(bearish) / len(bearish))
    assert row.fwd_return_baseline_pct == pytest.approx(sum(every) / len(every))
    # The baseline is the point of the exercise: without it a rising market
    # reads as a working indicator.
    assert row.fwd_return_baseline_pct != row.fwd_return_bullish_pct


def test_hit_rates_score_the_direction_that_was_called():
    """Bullish hits on a rise, bearish hits on a FALL — not on a rise."""
    window_start = TURNING_NOW - timedelta(days=60)
    bars = measured_bars(
        replay(GOLD, "daily_only", [], turning_series(), window_start, TURNING_NOW, CONFIG)
    )
    row = measure_window(GOLD, 60, [], turning_series(), TURNING_NOW, CONFIG)

    bullish = [b.forward_return_pct for b in bars if b.state == "bullish"]
    bearish = [b.forward_return_pct for b in bars if b.state == "bearish"]
    assert bullish and bearish, "the fixture must exercise both directions"

    assert row.hit_rate_bullish == pytest.approx(
        sum(1 for v in bullish if v > 0) / len(bullish))
    assert row.hit_rate_bearish == pytest.approx(
        sum(1 for v in bearish if v < 0) / len(bearish))
    # A bearish stretch that fell is a hit even though the return is negative;
    # scoring it like a bullish call would report the indicator upside down.
    assert row.fwd_return_bearish_pct < 0
    assert row.hit_rate_bearish > 0.5


def test_an_exactly_flat_forward_day_is_a_miss_on_both_sides():
    """The call was for a move. There was none, so nobody was right.

    Counting a tie as a hit is the classic way a coin-flip indicator reports a
    winning record, so the rule is pinned directly rather than left to a fixture
    that happens not to produce one.
    """
    from app.models.trend_backtest import _hit_rate

    assert _hit_rate([0.0], bullish=True) == 0.0
    assert _hit_rate([0.0], bullish=False) == 0.0
    assert _hit_rate([1.0, 0.0], bullish=True) == pytest.approx(0.5)
    assert _hit_rate([-1.0, 0.0], bullish=False) == pytest.approx(0.5)
    assert _hit_rate([], bullish=True) is None


def test_a_flat_market_is_measured_as_zero_not_as_a_signal():
    flat = daily_candles([100.0] * 20)
    row = measure_window(GOLD, 30, [], flat, BASE + timedelta(days=20), CONFIG)
    # A flat series is never a strict stack, so every bar is unaligned...
    assert row.bullish_bars == row.bearish_bars == 0
    assert row.hit_rate_bullish is None and row.hit_rate_bearish is None
    # ...and the baseline over a flat market is exactly zero, which IS a
    # measurement, unlike the two Nones above.
    assert row.fwd_return_baseline_pct == pytest.approx(0.0)


def test_episodes_count_entries_not_bars():
    """Bullish, broken, bullish again is two episodes over many bars."""
    row = measure_window(GOLD, 60, [], turning_series(), TURNING_NOW, CONFIG)
    assert row.bullish_episodes == 2
    assert row.bearish_episodes == 1
    assert row.bullish_bars > row.bullish_episodes
    # A hit rate is only readable next to these: three bars in one episode is
    # noise however good the rate looks.
    assert row.samples > 0


def test_a_state_that_never_happened_is_null_never_zero():
    """"Never bearish" and "bearish and flat" are different facts."""
    rising = daily_candles(ramp(40, 100.0, 2.0))
    row = measure_window(GOLD, 60, [], rising, BASE + timedelta(days=40), CONFIG)

    assert row.bearish_bars == 0
    assert row.bearish_episodes == 0
    assert row.fwd_return_bearish_pct is None
    assert row.hit_rate_bearish is None
    # The bullish side of the same row IS measured, so the Nones above are not
    # an empty-window artefact.
    assert row.bullish_bars > 0
    assert row.fwd_return_bullish_pct is not None


def test_a_window_with_no_bars_reports_nothing_rather_than_zero():
    stale = daily_candles(ramp(5, 100.0, 1.0))
    row = measure_window(GOLD, 90, [], stale, BASE + timedelta(days=200), CONFIG)

    assert row.samples == 0
    assert row.replayed_bars == 0
    assert row.evaluated_from is None and row.evaluated_to is None
    for value in (
        row.fwd_return_bullish_pct, row.fwd_return_bearish_pct,
        row.fwd_return_baseline_pct, row.hit_rate_bullish, row.hit_rate_bearish,
    ):
        assert value is None
    assert "no track record to report" in row.note


def test_the_newest_bar_has_no_forward_day_and_is_excluded():
    row = measure_window(GOLD, 60, [], turning_series(), TURNING_NOW, CONFIG)
    assert row.replayed_bars == row.samples + 1
    assert "Excluded as too new to score: 1" in row.note


def test_a_gap_is_dropped_rather_than_stretched_over():
    """A missing day must not turn a 1-day horizon into a 2-day one."""
    values = ramp(40, 100.0, 2.0)
    full = daily_candles(values)
    holed = [c for c in full if c.start != BASE + timedelta(days=20)]
    now = BASE + timedelta(days=40)

    intact = measure_window(GOLD, 60, [], full, now, CONFIG)
    gapped = measure_window(GOLD, 60, [], holed, now, CONFIG)

    # The bar before the hole loses its forward observation, and the hole itself
    # is one fewer bar: two bars fewer, none of them stretched.
    assert gapped.samples == intact.samples - 2
    assert "Dropped on a hole in the price series: 1" in gapped.note
    bars = replay(GOLD, "daily_only", [], holed, now - timedelta(days=60), now, CONFIG)
    before_hole = [b for b in bars if b.close_time == BASE + timedelta(days=20)]
    assert before_hole and before_hole[0].forward_return_pct is None


def test_the_row_stores_counts_and_returns_and_nothing_else():
    """No Sharpe, no p-value, no significance claim — by construction.

    With these sample sizes any of them would be decoration on noise, so the
    column set is pinned here rather than left to reviewer vigilance.
    """
    row = measure_window(GOLD, 30, [], turning_series(), TURNING_NOW, CONFIG)
    assert set(row.as_row()) == ROW_COLUMNS
    report = row.as_dict()
    assert set(report) == ROW_COLUMNS | {"replayed_bars", "bars_without_forward_return"}
    assert report["bars_without_forward_return"] == row.replayed_bars - row.samples


def test_the_model_and_the_table_mirror_agree_on_the_columns():
    """A column added to one side and not the other fails the upsert in prod."""
    assert set(trend_alignment_performance.c.keys()) == ROW_COLUMNS | {
        "computed_at", "updated_at",
    }


def test_both_bases_measure_the_same_forward_horizon():
    """A full_mtf row and a daily_only row must be comparable, not two questions."""
    hourly = hourly_candles(ramp(240, 1000.0, 1.0))
    daily = daily_candles(ramp(40, 500.0, 1.0), start=BASE - timedelta(days=30))
    now = BASE + timedelta(days=10)

    mtf = measure_window(GOLD, 2, hourly, daily, now, CONFIG)
    dly = measure_window(GOLD, 30, hourly, daily, now, CONFIG)

    assert mtf.basis == "full_mtf" and dly.basis == "daily_only"
    for row in (mtf, dly):
        assert "over the next 1 day from each bar's close" in row.note


# --- persistence and the job -------------------------------------------------


def seed_daily_prices(engine, symbol: str, values, *, start: datetime = BASE) -> None:
    """One good observation per day, at midday, so each day is one candle."""
    rows = [
        {
            "symbol": symbol,
            "value": float(value),
            "currency": "IRT",
            "unit": "gram",
            "source": "test",
            "observed_at": start + timedelta(days=index, hours=12),
            "quality": "ok",
        }
        for index, value in enumerate(values)
    ]
    with engine.begin() as conn:
        conn.execute(prices.insert(), rows)


@pytest.fixture()
def trend_settings(settings: Settings) -> Settings:
    settings.trend_alignment_enabled = True
    settings.trend_alignment_fast_period = 2
    settings.trend_alignment_mid_period = 3
    settings.trend_alignment_slow_period = 4
    return settings


@pytest.fixture()
def turning_engine(engine):
    seed_daily_prices(engine, GOLD, TURNING_VALUES)
    return engine


def stored(engine, symbol: str = GOLD) -> list:
    with engine.connect() as conn:
        return conn.execute(
            select(trend_alignment_performance)
            .where(trend_alignment_performance.c.symbol == symbol)
            .order_by(trend_alignment_performance.c.window_days.desc())
        ).mappings().all()


def test_refresh_writes_one_row_per_window(turning_engine, trend_settings):
    refresh_performance(turning_engine, GOLD, trend_config(trend_settings), TURNING_NOW)

    rows = stored(turning_engine)
    assert [r["window_days"] for r in rows] == list(WINDOW_DAYS)
    for row in rows:
        assert row["basis"] == "daily_only"
        assert row["note"]
        assert row["bullish_bars"] + row["bearish_bars"] + row["unaligned_bars"] == row["samples"]


def test_a_second_refresh_restates_the_window_rather_than_appending(
    turning_engine, trend_settings
):
    """Two rows both claiming to be "the last 90 days" would be unreadable."""
    config = trend_config(trend_settings)
    refresh_performance(turning_engine, GOLD, config, TURNING_NOW)
    first = stored(turning_engine)
    refresh_performance(turning_engine, GOLD, config, TURNING_NOW)
    second = stored(turning_engine)

    assert len(first) == len(second) == len(WINDOW_DAYS)
    assert [r["samples"] for r in first] == [r["samples"] for r in second]


def test_a_state_that_never_happened_is_stored_as_null(engine, trend_settings):
    """The database must hold NULL, not 0.0, for a state with no bars."""
    seed_daily_prices(engine, GOLD, ramp(40, 100.0, 2.0))
    refresh_performance(engine, GOLD, trend_config(trend_settings), BASE + timedelta(days=40))

    row = stored(engine)[0]
    assert row["bearish_bars"] == 0
    assert row["fwd_return_bearish_pct"] is None
    assert row["hit_rate_bearish"] is None
    assert row["fwd_return_bullish_pct"] is not None
    assert row["fwd_return_baseline_pct"] is not None


def test_prices_stamped_after_now_cannot_change_a_stored_window(
    turning_engine, trend_settings
):
    """Look-ahead again, this time through the loader.

    A replay at a historical instant must read the series as it stood then. A
    provider with a skewed clock — or any re-run at an earlier `now` — would
    otherwise let tomorrow's tick rewrite yesterday's track record.
    """
    config = trend_config(trend_settings)
    refresh_performance(turning_engine, GOLD, config, TURNING_NOW)
    before = [dict(r) for r in stored(turning_engine)]

    seed_daily_prices(turning_engine, GOLD, [10_000.0, 20_000.0], start=TURNING_NOW)
    refresh_performance(turning_engine, GOLD, config, TURNING_NOW)
    after = [dict(r) for r in stored(turning_engine)]

    for old, new in zip(before, after):
        for column in ROW_COLUMNS:
            assert old[column] == new[column], column


def test_the_pass_refreshes_the_track_record_beside_the_live_state(
    turning_engine, trend_settings
):
    result = run_trend_alignment(
        turning_engine, trend_settings, symbols=[GOLD], now=TURNING_NOW
    )

    assert result["evaluated"] == 1
    assert result["performance_windows"] == len(WINDOW_DAYS)
    windows = result["symbols"][GOLD]["performance"]
    assert [w["window_days"] for w in windows] == list(WINDOW_DAYS)
    for window in windows:
        assert window["basis"] in {"full_mtf", "daily_only"}
        assert window["note"]
        assert window["replayed_bars"] >= window["samples"]
    assert len(stored(turning_engine)) == len(WINDOW_DAYS)


def test_a_backtest_failure_never_sinks_the_live_indicator(
    turning_engine, trend_settings, monkeypatch
):
    """The alignment is the obligation; its history is the weaker one.

    A raising backtest must cost the user a history section and nothing else —
    the state and the transition log are already committed by then.
    """
    from app.jobs import trend_alignment as job

    def explode(*args, **kwargs):
        raise RuntimeError("simulated backtest failure")

    monkeypatch.setattr(job, "backtest", explode)
    result = run_trend_alignment(
        turning_engine, trend_settings, symbols=[GOLD], now=TURNING_NOW
    )

    assert result["evaluated"] == 1
    assert result["failed"] == 0
    assert result["performance_windows"] == 0
    assert "simulated backtest failure" in result["symbols"][GOLD]["performance_error"]
    assert result["symbols"][GOLD]["status"] == "ok"
    assert stored(turning_engine) == []
    # The live indicator is intact, which is the whole point of the try/except.
    with turning_engine.connect() as conn:
        state = conn.execute(
            select(trend_alignment_states).where(trend_alignment_states.c.symbol == GOLD)
        ).mappings().first()
    assert state is not None
    assert state["alignment"] in {"full_bullish", "full_bearish", "not_aligned"}


def test_evaluate_endpoint_reports_per_window_outcomes(client, engine, settings):
    settings.trend_alignment_fast_period = 2
    settings.trend_alignment_mid_period = 3
    settings.trend_alignment_slow_period = 4
    seed_daily_prices(engine, GOLD, TURNING_VALUES)

    response = client.post(
        "/internal/trend-alignment/evaluate",
        headers={"X-Internal-Token": "test-internal-token"},
        json={"symbols": [GOLD]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["performance_windows"] == len(WINDOW_DAYS)
    windows = body["symbols"][GOLD]["performance"]
    assert [w["window_days"] for w in windows] == list(WINDOW_DAYS)
    for window in windows:
        # `now` is the wall clock here, so the fixture series is long stale: the
        # endpoint must still answer with a labelled, noted row rather than raise.
        assert window["basis"] in {"full_mtf", "daily_only"}
        assert isinstance(window["note"], str) and window["note"]
